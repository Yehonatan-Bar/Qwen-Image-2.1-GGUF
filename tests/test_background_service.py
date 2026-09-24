"""
Checks the background-service switch (BackgroundServiceControl + /api/background-service) against a
fake management script and fake health endpoints, so no real service is ever touched:
state from health, start/stop running the script in the background, busy and failure handling.

Run from the project root:  python tests/test_background_service.py
"""
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src" / "webui"))
import server  # noqa: E402

# The fake script records the action it was called with, then exits with the code in exit_code.txt
FAKE_SCRIPT = r"""
param([string]$Action)
$here = Split-Path -Parent $PSCommandPath
Start-Sleep -Milliseconds 700
Add-Content -LiteralPath (Join-Path $here 'calls.txt') -Value $Action
exit [int](Get-Content -LiteralPath (Join-Path $here 'exit_code.txt'))
"""


class FakeHealth(BaseHTTPRequestHandler):
    healthy = True

    def do_GET(self):
        self.send_response(200 if type(self).healthy else 503)
        self.end_headers()

    def log_message(self, *arguments):
        pass


def start_health_server(handler_class):
    health_server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=health_server.serve_forever, daemon=True).start()
    return health_server, f"http://127.0.0.1:{health_server.server_address[1]}/health"


def wait_until_idle(control, timeout_seconds=120):   # PowerShell alone can take 10 s to start on a busy PC
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if control.snapshot()["state"] not in ("starting", "stopping"):
            return True
        time.sleep(0.2)
    return False


def main():
    failures = []

    def check(condition, description):
        print(("PASS  " if condition else "FAIL  ") + description)
        if not condition:
            failures.append(description)

    work_dir = Path(tempfile.mkdtemp(prefix="background_service_test_"))
    script_path = work_dir / "service-control.ps1"
    script_path.write_text(FAKE_SCRIPT, encoding="utf-8")
    exit_code_path = work_dir / "exit_code.txt"
    exit_code_path.write_text("0", encoding="utf-8")
    calls_path = work_dir / "calls.txt"

    class FirstHealth(FakeHealth):
        healthy = True

    class SecondHealth(FakeHealth):
        healthy = True

    first_server, first_url = start_health_server(FirstHealth)
    second_server, second_url = start_health_server(SecondHealth)
    log_path = work_dir / "control.log"

    def make_control(**settings):
        return server.BackgroundServiceControl(settings=settings, log_path=log_path)

    # Not configured: the control is not offered at all
    unavailable = {"available": False, "state": "unavailable", "services": {}, "error": ""}
    check(make_control().snapshot() == unavailable, "no settings: unavailable")
    check(make_control(script=str(work_dir / "absent.ps1"), health_urls={"first": first_url}).snapshot() == unavailable,
          "missing script: unavailable")
    check(make_control(script=str(script_path)).snapshot() == unavailable, "no health URLs: unavailable")

    control = make_control(label="שירות בדיקה", script=str(script_path),
                           health_urls={"first": first_url, "second": second_url})
    status = control.snapshot()
    check(status["state"] == "on", "both services healthy: on")
    check(status["label"] == "שירות בדיקה" and status["description"] == server.DEFAULT_BACKGROUND_SERVICE_DESCRIPTION,
          "label from settings, default description")
    SecondHealth.healthy = False
    check(control.snapshot()["state"] == "partial", "one service healthy: partial")
    FirstHealth.healthy = False
    check(control.snapshot()["state"] == "off", "no service healthy: off")
    unreachable = make_control(script=str(script_path), health_urls={"first": "http://127.0.0.1:9/health"})
    check(unreachable.snapshot()["state"] == "off", "nothing listening: off")
    check(unreachable.snapshot()["label"] == server.DEFAULT_BACKGROUND_SERVICE_LABEL, "default label when none is set")

    # Stop: runs the script in the background, busy meanwhile, a second request refused
    FirstHealth.healthy = SecondHealth.healthy = True
    control.request("stop")
    check(control.snapshot()["state"] == "stopping", "stop in progress: stopping")
    try:
        control.request("start")
        check(False, "a second request while busy is refused")
    except RuntimeError:
        check(True, "a second request while busy is refused")
    check(wait_until_idle(control), "stop finishes")
    check(calls_path.read_text(encoding="utf-8").split() == ["Stop"], "the script was called with Stop")
    check(control.snapshot()["error"] == "", "a successful stop leaves no error")

    # A failing script is reported with a message, and cleared by the next successful run
    exit_code_path.write_text("1", encoding="utf-8")
    control.request("start")
    check(wait_until_idle(control), "failing start finishes")
    check(control.snapshot()["error"] == server.MESSAGE_SERVICE_START_FAILED, "a failing start reports its error")
    exit_code_path.write_text("0", encoding="utf-8")

    try:
        control.request("reboot")
        check(False, "an unknown action is refused")
    except ValueError:
        check(True, "an unknown action is refused")

    # The HTTP endpoints of the UI server
    server.background_service_control = control
    ui_server = ThreadingHTTPServer(("127.0.0.1", 0), server.WebUiRequestHandler)
    threading.Thread(target=ui_server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{ui_server.server_address[1]}"
    with urllib.request.urlopen(base_url + "/api/background-service", timeout=10) as response:
        status = json.loads(response.read().decode("utf-8"))
    check(status["available"] and status["state"] == "on", "GET /api/background-service reports the state")
    request = urllib.request.Request(base_url + "/api/background-service", data=json.dumps({"action": "start"}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        accepted = json.loads(response.read().decode("utf-8"))
        check(response.status == 202 and accepted["state"] == "starting", "POST start: 202 and starting")
    check(wait_until_idle(control), "start finishes")
    check(calls_path.read_text(encoding="utf-8").split() == ["Stop", "Start", "Start"], "the script was called with Start")
    bad = urllib.request.Request(base_url + "/api/background-service", data=b'{"action": "x"}',
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(bad, timeout=10)
        check(False, "POST with an unknown action: 400")
    except urllib.error.HTTPError as http_error:
        check(http_error.code == 400, "POST with an unknown action: 400")

    for running_server in (ui_server, first_server, second_server):
        running_server.shutdown()
    print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
