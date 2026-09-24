"""
Checks that the web UI refuses requests other websites could forge (wrong Host, foreign Origin,
non-JSON POST) and still accepts its own. No model or real service is touched; takes seconds.

Run from the project root:  python tests/test_request_guard.py
"""
import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src" / "webui"))
import server  # noqa: E402


def send(port, method, path, headers=None, body=None):
    """Send a raw request with full control over Host/Origin/Content-Type. Returns the status code."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    request_headers = {"Host": f"127.0.0.1:{port}"}
    request_headers.update(headers or {})
    body_bytes = body.encode("utf-8") if body is not None else b""
    if body is not None:
        request_headers["Content-Length"] = str(len(body_bytes))
    for header_name, header_value in request_headers.items():
        if header_value is not None:
            connection.putheader(header_name, header_value)
    connection.endheaders(body_bytes if body is not None else None)
    status_code = connection.getresponse().status
    connection.close()
    return status_code


def main():
    failures = []

    def check(condition, description):
        print(("PASS  " if condition else "FAIL  ") + description)
        if not condition:
            failures.append(description)

    # Real job manager (idle), background-service control not configured so nothing external runs
    server.job_manager = server.JobManager()
    server.background_service_control = server.BackgroundServiceControl(settings={})
    ui_server = ThreadingHTTPServer(("127.0.0.1", 0), server.WebUiRequestHandler)
    server.http_server = ui_server
    threading.Thread(target=ui_server.serve_forever, daemon=True).start()
    port = ui_server.server_address[1]
    own_origin = f"http://127.0.0.1:{port}"
    json_type = {"Content-Type": "application/json"}

    # Legitimate traffic
    check(send(port, "GET", "/api/status") == 200, "GET from a non-browser client (no Origin) is allowed")
    check(send(port, "GET", "/api/status", {"Host": f"localhost:{port}"}) == 200, "Host localhost:<port> is allowed")
    check(send(port, "POST", "/api/cancel", dict(json_type, Origin=own_origin), "{}") == 200, "POST JSON from the UI's own origin is allowed")
    check(send(port, "POST", "/api/cancel", {"Content-Type": "application/json; charset=utf-8"}, "{}") == 200,
          "POST JSON with charset from PowerShell (no Origin) is allowed")

    # Forged traffic
    check(send(port, "POST", "/api/cancel", {"Content-Type": "text/plain"}, "{}") == 403, "POST text/plain (form-style CSRF) is refused")
    check(send(port, "POST", "/api/cancel", {"Content-Type": None}, "{}") == 403, "POST without Content-Type is refused")
    check(send(port, "POST", "/api/cancel", dict(json_type, Origin="http://evil.example"), "{}") == 403, "POST from a foreign Origin is refused")
    check(send(port, "POST", "/api/cancel", dict(json_type, Origin="null"), "{}") == 403, "POST from a sandboxed/file page (Origin null) is refused")
    check(send(port, "GET", "/api/gallery", {"Host": f"evil.example:{port}"}) == 403, "GET with a foreign Host (DNS rebinding) is refused")
    check(send(port, "GET", "/", {"Host": "127.0.0.1:1"}) == 403, "GET with a different port in Host is refused")
    check(send(port, "POST", "/api/background-service", {"Content-Type": "text/plain"}, '{"action": "stop"}') == 403,
          "forged background-service switch is refused")
    check(send(port, "POST", "/api/shutdown", {"Content-Type": "text/plain"}, "{}") == 403, "forged shutdown is refused")
    check(send(port, "GET", "/api/status") == 200, "server still running after the forged shutdown")

    ui_server.shutdown()
    print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
