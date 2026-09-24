"""
Checks the web UI's safety net: a child process attached to the kill-on-close job object
must die when the parent process dies abruptly (crash / window closed).

Run from the project root:  python tests/test_kill_on_close.py
"""
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Parent: create the job, start a long-sleeping child, attach it, print its PID, then die without cleanup
PARENT_CODE = r"""
import os, subprocess, sys
sys.path.insert(0, r"{webui_dir}")
import server
job_handle = server.create_kill_on_close_job()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
attached = server.assign_process_to_job(job_handle, child)
print(child.pid, bool(job_handle), attached, flush=True)
os._exit(0)
""".format(webui_dir=PROJECT_ROOT / "src" / "webui")


def process_is_alive(process_id):
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    process_handle = kernel32.OpenProcess(0x1000, False, process_id)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process_handle:
        return False
    exit_code = ctypes.c_uint32()
    kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code))
    kernel32.CloseHandle(process_handle)
    return exit_code.value == 259  # STILL_ACTIVE


def main():
    parent_output = subprocess.run([sys.executable, "-c", PARENT_CODE], capture_output=True, text=True, timeout=60)
    child_pid_text, job_created, attached = parent_output.stdout.split()
    child_pid = int(child_pid_text)
    print(f"job created: {job_created}, child attached: {attached}, child pid: {child_pid}")
    deadline = time.time() + 10
    while time.time() < deadline and process_is_alive(child_pid):
        time.sleep(0.2)
    child_alive = process_is_alive(child_pid)
    if child_alive:
        subprocess.run(["taskkill", "/PID", str(child_pid), "/F"], capture_output=True)
    print("FAIL  child survived the parent" if child_alive else "PASS  child was killed together with the parent")
    return 1 if child_alive or attached != "True" else 0


if __name__ == "__main__":
    sys.exit(main())
