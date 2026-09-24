"""
End-to-end test of the Qwen-Image web UI server (src/webui/server.py).

Runs real (small, fast) generations with the installed model, so it takes a few minutes.
Standard library only. Run from the project root:

    python tests/test_webui_integration.py
"""
import base64
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = PROJECT_ROOT / "src" / "webui" / "server.py"
# The server writes to a temporary folder (env QWEN_WEBUI_OUTPUT_DIR) so the user's outputs/ is never touched
OUTPUT_DIR = Path(tempfile.mkdtemp(prefix="qwen_webui_integration_"))
TEST_PORT = 7861
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"
FAST_SETTINGS = {"width": 512, "height": 512, "steps": 8, "cfg_scale": 6, "count": 1, "seed": 11}
JOB_TIMEOUT_SECONDS = 600


def http_request(path, body=None, method=None):
    """Return (status_code, parsed_json_or_bytes)."""
    request_data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(BASE_URL + path, data=request_data, method=method or ("POST" if body is not None else "GET"))
    if request_data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw_body = response.read()
            status_code = response.status
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as http_error:
        raw_body = http_error.read()
        status_code = http_error.code
        content_type = http_error.headers.get("Content-Type", "")
    if "application/json" in content_type:
        return status_code, json.loads(raw_body.decode("utf-8"))
    return status_code, raw_body


def wait_for_server(timeout_seconds=20):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            if http_request("/api/status")[0] == 200:
                return
        except OSError:
            time.sleep(0.3)
    raise AssertionError("Server did not start")


def wait_for_job_end(timeout_seconds=JOB_TIMEOUT_SECONDS):
    deadline = time.time() + timeout_seconds
    seen_phases = set()
    while time.time() < deadline:
        status = http_request("/api/status")[1]
        if status["phase"]:
            seen_phases.add(status["phase"])
        if status["state"] != "running":
            return status, seen_phases
        time.sleep(1)
    raise AssertionError("Job did not finish in time")


def wait_for_phase(target_phases, timeout_seconds=180):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = http_request("/api/status")[1]
        if status["phase"] in target_phases:
            return status
        time.sleep(0.5)
    raise AssertionError(f"Phase {target_phases} not reached")


class _ProcessEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def engine_processes():
    """Return PIDs of running sd-cli.exe processes.

    Uses a Toolhelp snapshot instead of tasklist/WMI, which can hang for minutes on a loaded machine.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32)]
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot_handle = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    engine_pids = []
    process_entry = _ProcessEntry32()
    process_entry.dwSize = ctypes.sizeof(_ProcessEntry32)
    has_entry = kernel32.Process32FirstW(snapshot_handle, ctypes.byref(process_entry))
    while has_entry:
        if process_entry.szExeFile.lower() == "sd-cli.exe":
            engine_pids.append(process_entry.th32ProcessID)
        has_entry = kernel32.Process32NextW(snapshot_handle, ctypes.byref(process_entry))
    kernel32.CloseHandle(snapshot_handle)
    return engine_pids


def wait_for_no_engine(timeout_seconds=30):
    """A killed engine can take several seconds to exit while it releases GPU memory."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not engine_processes():
            return True
        time.sleep(0.5)
    return False


def start_server():
    server_process = subprocess.Popen([sys.executable, str(SERVER_SCRIPT), "--port", str(TEST_PORT), "--no-browser"],
                                      cwd=str(PROJECT_ROOT), env=dict(os.environ, QWEN_WEBUI_OUTPUT_DIR=str(OUTPUT_DIR)))
    wait_for_server()
    return server_process


def check(condition, description):
    print(("PASS  " if condition else "FAIL  ") + description, flush=True)
    if not condition:
        raise AssertionError(description)


def main():
    assert not engine_processes(), "An sd-cli.exe is already running; stop it before testing"
    created_images = []
    server_process = start_server()
    try:
        # --- Static and read-only endpoints
        status_code, page_bytes = http_request("/")
        check(status_code == 200 and "סטודיו מקומי".encode("utf-8") in page_bytes, "GET / serves the Hebrew UI page")
        status_code, config = http_request("/api/config")
        check(status_code == 200 and config["model"].startswith("qwen-image-2.1-UC"), "config reports the UC model")
        check(config["edit_available"] is True, "config reports editing available")
        check(http_request("/api/status")[1]["state"] == "idle", "initial state is idle")
        check(http_request("/api/gallery")[0] == 200, "gallery endpoint responds")
        check(http_request("/outputs/..%5Cserver.py")[0] == 404, "path traversal in /outputs is rejected")

        # --- Validation
        check(http_request("/api/generate", {"mode": "txt2img", "prompt": "  "})[0] == 400, "empty prompt is rejected")
        check(http_request("/api/generate", {"mode": "edit", "prompt": "x", "images": []})[0] == 400, "edit without image is rejected")

        # --- Text to image
        started_at = time.time()
        status_code, job = http_request("/api/generate", dict(FAST_SETTINGS, mode="txt2img",
                                        prompt="a small wooden sign that says 'Test' on a white wall"))
        check(status_code == 202, "txt2img job accepted")
        check(http_request("/api/generate", dict(FAST_SETTINGS, mode="txt2img", prompt="second"))[0] == 409, "second job while busy gets 409")
        final_status, seen_phases = wait_for_job_end()
        check(final_status["state"] == "done" and len(final_status["results"]) == 1, "txt2img job finished with 1 image")
        check("sampling" in seen_phases, f"progress phases were reported ({sorted(seen_phases)})")
        created_images += final_status["results"]
        result_name = final_status["results"][0]
        check((OUTPUT_DIR / result_name).exists(), "txt2img image file exists")
        metadata = json.loads((OUTPUT_DIR / f"{job['job_id']}.json").read_text(encoding="utf-8"))
        check(metadata["seeds"] == [11] and metadata["mode"] == "txt2img", "metadata sidecar has seed and mode")
        check(not engine_processes(), "engine exited after the job (memory released)")
        print(f"      txt2img took {time.time() - started_at:.0f}s", flush=True)

        # --- Image edit, using the image just created as the reference
        reference_data_url = "data:image/png;base64," + base64.b64encode((OUTPUT_DIR / result_name).read_bytes()).decode("ascii")
        started_at = time.time()
        status_code, _ = http_request("/api/generate", dict(FAST_SETTINGS, mode="edit", images=[reference_data_url],
                                      prompt="Change the text on the sign to 'Done'. Keep everything else the same."))
        check(status_code == 202, "edit job accepted")
        final_status, seen_phases = wait_for_job_end()
        check(final_status["state"] == "done" and len(final_status["results"]) == 1, "edit job finished with 1 image")
        created_images += final_status["results"]
        gallery_items = http_request("/api/gallery")[1]["items"]
        edited_item = next(item for item in gallery_items if item["name"] == final_status["results"][0])
        check(edited_item["meta"]["mode"] == "edit" and edited_item["meta"]["reference_count"] == 1, "gallery shows edit metadata")
        check(not engine_processes(), "engine exited after the edit job")
        print(f"      edit took {time.time() - started_at:.0f}s", flush=True)

        # --- Cancel during a job
        http_request("/api/generate", dict(FAST_SETTINGS, mode="txt2img", prompt="a cat"))
        wait_for_phase({"loading", "sampling"})
        check(http_request("/api/cancel", {})[1]["cancelled"] is True, "cancel kills the running engine")
        final_status, _ = wait_for_job_end(60)
        check(final_status["state"] == "cancelled", "state becomes cancelled")
        check(wait_for_no_engine(), "no engine left after cancel")

        # --- Delete through the API: one image, then everything that is left
        first_image, remaining_images = created_images[0], created_images[1:]
        check(http_request("/api/delete", {"name": first_image})[1]["deleted"] == [first_image], f"deleted {first_image}")
        check(not (OUTPUT_DIR / f"{job['job_id']}.json").exists(), "metadata sidecar removed with its last image")
        delete_all_response = http_request("/api/delete", {"all": True})[1]
        check(sorted(delete_all_response["deleted"]) == sorted(remaining_images), "delete all removed the remaining images")
        check(http_request("/api/gallery")[1]["total"] == 0, "gallery empty after delete all")

        # --- Kill-on-close: if the server dies mid-job, the engine must die too
        http_request("/api/generate", dict(FAST_SETTINGS, mode="txt2img", prompt="a dog"))
        wait_for_phase({"loading", "sampling"})
        check(len(engine_processes()) == 1, "engine running before killing the server")
        server_process.kill()
        server_process.wait(10)
        check(wait_for_no_engine(), "engine was killed together with the server")

        # --- Shutdown button
        server_process = start_server()
        check(http_request("/api/shutdown", {})[1]["shutting_down"] is True, "shutdown endpoint responds")
        server_process.wait(15)
        check(server_process.returncode == 0, "server exited cleanly after shutdown")
    finally:
        if server_process.poll() is None:
            server_process.kill()
        # Leave nothing behind, also from an interrupted run
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    print("\nAll web UI checks passed.")


if __name__ == "__main__":
    main()
