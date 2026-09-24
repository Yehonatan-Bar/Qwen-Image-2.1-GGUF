"""
Tests gallery deletion in the web UI server: single, batch, and delete-all, sidecar cleanup,
unsafe names, and that nothing outside the output folder is touched.

Runs the server against a temporary output folder (env QWEN_WEBUI_OUTPUT_DIR) filled with
small fake images, so the user's real outputs/ folder is never touched. No model is loaded.

Run from the project root:  python tests/test_gallery_delete.py
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = PROJECT_ROOT / "src" / "webui" / "server.py"
REAL_OUTPUT_DIR = PROJECT_ROOT / "outputs"


def find_free_port():
    """Ask the OS for a free port. Several Claude sessions test this project at once,
    so a fixed port number can collide with another session's own test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        return probe_socket.getsockname()[1]


TEST_PORT = find_free_port()
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"
# Smallest valid PNG (1x1 transparent pixel)
TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def http_request(path, body=None):
    request_data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(BASE_URL + path, data=request_data, method="POST" if body is not None else "GET")
    if request_data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as http_error:
        return http_error.code, json.loads(http_error.read().decode("utf-8") or "{}")


def check(condition, description):
    print(("PASS  " if condition else "FAIL  ") + description, flush=True)
    if not condition:
        raise AssertionError(description)


def create_fake_outputs(output_dir):
    for image_name in ("qwen_20260101-000001.png", "qwen_20260101-000002_0.png",
                       "qwen_20260101-000002_1.png", "legacy image.png"):
        (output_dir / image_name).write_bytes(TINY_PNG_BYTES)
    for job_id in ("qwen_20260101-000001", "qwen_20260101-000002", "qwen_20260101-000009"):
        (output_dir / f"{job_id}.json").write_text(json.dumps({"job_id": job_id, "prompt": "x"}), encoding="utf-8")
    (output_dir / "notes.txt").write_text("not an image", encoding="utf-8")


def real_outputs_snapshot():
    if not REAL_OUTPUT_DIR.exists():
        return set()
    return {path.name for path in REAL_OUTPUT_DIR.iterdir()}


def main():
    real_outputs_before = real_outputs_snapshot()
    temp_root = Path(tempfile.mkdtemp(prefix="qwen_gallery_test_"))
    output_dir = temp_root / "outputs"
    output_dir.mkdir()
    outside_file = temp_root / "outside.png"          # must never be deleted
    outside_file.write_bytes(TINY_PNG_BYTES)
    create_fake_outputs(output_dir)

    server_environment = dict(os.environ, QWEN_WEBUI_OUTPUT_DIR=str(output_dir))
    server_process = subprocess.Popen([sys.executable, str(SERVER_SCRIPT), "--port", str(TEST_PORT), "--no-browser"],
                                      cwd=str(PROJECT_ROOT), env=server_environment)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if http_request("/api/status")[0] == 200:
                    break
            except OSError:
                time.sleep(0.3)

        gallery = http_request("/api/gallery")[1]
        check(gallery["total"] == 4 and len(gallery["items"]) == 4, "gallery lists the 4 fake images with total")

        # Single delete ({name}) also removes the job's settings file
        status_code, response = http_request("/api/delete", {"name": "qwen_20260101-000001.png"})
        check(status_code == 200 and response["deleted"] == ["qwen_20260101-000001.png"], "single delete reports the file")
        check(not (output_dir / "qwen_20260101-000001.png").exists(), "single image removed from disk")
        check(not (output_dir / "qwen_20260101-000001.json").exists(), "its settings file removed with it")

        # Batch: the settings file stays while another image of the same job remains
        response = http_request("/api/delete", {"names": ["qwen_20260101-000002_0.png"]})[1]
        check(response["deleted"] == ["qwen_20260101-000002_0.png"], "batch delete of one image")
        check((output_dir / "qwen_20260101-000002.json").exists(), "settings file kept while a sibling image remains")

        # Unsafe and missing names are skipped, nothing outside the folder is touched
        response = http_request("/api/delete", {"names": ["..\\outside.png", "../outside.png", "missing.png",
                                                          "qwen_20260101-000002_1.png", "qwen_20260101-000002_1.png"]})[1]
        check(response["deleted"] == ["qwen_20260101-000002_1.png"], "only the valid image is deleted (duplicates ignored)")
        check(sorted(response["skipped"]) == sorted(["..\\outside.png", "../outside.png", "missing.png"]),
              "unsafe and missing names are reported as skipped")
        check(outside_file.exists(), "file outside the output folder was not touched")
        check(not (output_dir / "qwen_20260101-000002.json").exists(), "settings file removed with the last sibling")

        # Delete all: every image and orphan settings file goes, other files stay
        (output_dir / "qwen_20260101-000003.png").write_bytes(TINY_PNG_BYTES)
        response = http_request("/api/delete", {"all": True})[1]
        check(sorted(response["deleted"]) == sorted(["legacy image.png", "qwen_20260101-000003.png"]), "delete all removes every image")
        check(not list(output_dir.glob("*.png")), "no images left on disk")
        check(not (output_dir / "qwen_20260101-000009.json").exists(), "orphan settings file removed by delete all")
        check((output_dir / "notes.txt").exists(), "non-image files are left alone")
        gallery = http_request("/api/gallery")[1]
        check(gallery["total"] == 0 and gallery["items"] == [], "gallery is empty afterwards")

        check(http_request("/api/delete", {"names": []})[1]["deleted"] == [], "empty request deletes nothing")
    finally:
        http_request("/api/shutdown", {}) if server_process.poll() is None else None
        try:
            server_process.wait(10)
        except subprocess.TimeoutExpired:
            server_process.kill()
        shutil.rmtree(temp_root, ignore_errors=True)

    check(real_outputs_snapshot() == real_outputs_before, "the real outputs folder was not changed")
    print("\nAll gallery delete checks passed.")


if __name__ == "__main__":
    main()
