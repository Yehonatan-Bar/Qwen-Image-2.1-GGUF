"""
Qwen-Image-2.1 local web UI server.

Serves a browser UI on 127.0.0.1 and runs sd-cli.exe on demand for each job
(text-to-image or image editing). The model is loaded only while a job runs:
the engine process exits when the job ends, so the idle UI holds no model
RAM/VRAM. Standard library only - no pip installs.

Run:  python server.py [--port 7860] [--no-browser]
"""
import argparse
import base64
import binascii
import ctypes
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------------------
# Paths and model files
# ---------------------------------------------------------------------------
WEBUI_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = WEBUI_DIR.parents[1]
ENGINE_EXE = PROJECT_ROOT / "sd" / "sd-cli.exe"
MODELS_DIR = PROJECT_ROOT / "models"
DIFFUSION_MODEL = MODELS_DIR / "diffusion_models" / "qwen-image-2.1-UC-Q4_K_M.gguf"
TEXT_ENCODER = MODELS_DIR / "text_encoders" / "Qwen3VL-8B-Instruct-Q4_K_M.gguf"
VISION_ENCODER = MODELS_DIR / "text_encoders" / "mmproj-Qwen3VL-8B-Instruct-F16.gguf"
VAE_MODEL = MODELS_DIR / "vae" / "qwen_image_2.1_vae_bf16.safetensors"
# Tests point this at a temporary folder so they never touch the user's images
OUTPUT_DIR = Path(os.environ.get("QWEN_WEBUI_OUTPUT_DIR") or (PROJECT_ROOT / "outputs"))
LOG_DIR = PROJECT_ROOT / "logs"
INDEX_HTML = WEBUI_DIR / "index.html"

# Request limits
MAX_REQUEST_BYTES = 120 * 1024 * 1024
MAX_REFERENCE_IMAGES = 4
MAX_REFERENCE_IMAGE_BYTES = 30 * 1024 * 1024
MAX_PROMPT_CHARS = 4000
GALLERY_LIMIT = 300

# Windows process flags: run the engine without a console window, at lower CPU priority
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

# Optional background service on this PC that competes for the GPU (switched off while images are made).
# It is controlled only through its own script (`-File <script> Start|Stop`); its state comes from its
# health URLs. Everything about it is machine-specific, so it lives in local_settings.json (gitignored;
# see local_settings.example.json). Without a script and health URLs there, the switch is hidden.
LOCAL_SETTINGS_PATH = WEBUI_DIR / "local_settings.json"
try:
    _local_settings = json.loads(LOCAL_SETTINGS_PATH.read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    _local_settings = {}
BACKGROUND_SERVICE_SETTINGS = _local_settings.get("background_service") or {}
DEFAULT_BACKGROUND_SERVICE_LABEL = "שירות רקע"
DEFAULT_BACKGROUND_SERVICE_DESCRIPTION = ("שירות רקע שצורך מעבד וכרטיס מסך. כבו אותו כשצריך את כל כוח העיבוד "
                                          "של המחשב, והדליקו אותו בחזרה אחר כך.")
BACKGROUND_SERVICE_HEALTH_TIMEOUT_SECONDS = 1.5
BACKGROUND_SERVICE_ACTION_TIMEOUT_SECONDS = 420   # a Start may wait minutes for the service to load
BACKGROUND_SERVICE_LOG = LOG_DIR / "background_service.log"

# Official prompt wrapper for RGBA (transparent background) output
TRANSPARENT_PROMPT_TEMPLATE = (
    "This is an RGBA image with transparency. {prompt}. "
    "The image has alpha channel and the background is transparent."
)

# Output names: qwen_YYYYMMDD-HHMMSS.png, or qwen_YYYYMMDD-HHMMSS_<index>.png for batches
JOB_IMAGE_NAME_PATTERN = re.compile(r"^(qwen_\d{8}-\d{6})(?:_(\d+))?\.png$")
SAFE_FILE_NAME_PATTERN = re.compile(r"^[\w\-. ]+\.png$")

# ---------------------------------------------------------------------------
# Logging (category-filtered, see logging_config.json)
# ---------------------------------------------------------------------------
LOG_TAG = "[QWEN_WEBUI]"
LOGGING_CONFIG_PATH = WEBUI_DIR / os.environ.get("LOGGING_CONFIG_PATH", "logging_config.json")
_logging_config = None
_enabled_categories = set()


def load_logging_config():
    global _logging_config, _enabled_categories
    if os.environ.get("LOGGING_ENABLED", "true").lower() != "true":
        _logging_config, _enabled_categories = {"disabled": True}, set()
        return
    try:
        with open(LOGGING_CONFIG_PATH, "r", encoding="utf-8") as config_file:
            _logging_config = json.load(config_file)
        _enabled_categories = {
            name for name, settings in _logging_config.get("log_categories", {}).items() if settings.get("enabled")
        }
    except (FileNotFoundError, json.JSONDecodeError):
        _logging_config, _enabled_categories = {"default": True}, set()


def is_category_enabled(category):
    if _logging_config is None:
        load_logging_config()
    return not _logging_config.get("disabled") and category.strip("[]").upper().replace(" ", "_") in _enabled_categories


class CategoryFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if LOG_TAG not in message:
            return True
        match = re.search(r"\[QWEN_WEBUI\]\[([^\]]+)\]", message)
        return is_category_enabled(match.group(1)) if match else True


def get_logger():
    load_logging_config()
    new_logger = logging.getLogger("qwen_webui")
    if not new_logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        handler.addFilter(CategoryFilter())
        new_logger.addHandler(handler)
        new_logger.setLevel(logging.DEBUG)
    return new_logger


logger = get_logger()


# ---------------------------------------------------------------------------
# Windows job object: kills the engine if this server dies unexpectedly
# ---------------------------------------------------------------------------
class _IoCounters(ctypes.Structure):
    _fields_ = [(field_name, ctypes.c_ulonglong) for field_name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


def create_kill_on_close_job():
    """Create a job object whose processes are killed when this server's handle closes.

    If the UI window is closed or the server crashes, Windows closes the handle and
    terminates any running sd-cli.exe, so the model never keeps running unattended.
    Returns None when not on Windows or on failure (the server still works).
    """
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    job_handle = kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        return None
    limit_information = _JobExtendedLimitInformation()
    limit_information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
            job_handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limit_information), ctypes.sizeof(limit_information)):
        return None
    return job_handle


def assign_process_to_job(job_handle, process):
    """Put the engine process in the kill-on-close job. Returns True on success."""
    if not job_handle:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    if kernel32.AssignProcessToJobObject(job_handle, ctypes.c_void_p(int(process._handle))):
        return True
    logger.warning(f"{LOG_TAG}[JOB_ERROR] Could not attach engine to kill-on-close job "
                   f"(error {ctypes.get_last_error()}); it may outlive a crashed server")
    return False


# ---------------------------------------------------------------------------
# Engine output parsing
# ---------------------------------------------------------------------------
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Progress bars look like: "  |=====>     | 3/20 - 1.26s/it"  or  "| 23/397 - 69.50MB/s"
PROGRESS_PATTERN = re.compile(r"\|\s*(\d+)/(\d+)\s*-\s*([\d.]+)\s*(s/it|it/s|MB/s)")
IMAGE_START_PATTERN = re.compile(r"generating image:\s*(\d+)/(\d+)\s*-\s*seed\s*(-?\d+)")
DECODING_PATTERN = re.compile(r"decoding \d+ latents")
SAVED_IMAGE_PATTERN = re.compile(r"save result image (\d+) to '(.+?)' \((success|failure)\)")
ERROR_LINE_PATTERN = re.compile(r"\[ERROR\s*\][^-]*-\s*(.+)")
GPU_MEMORY_FAILURE_PATTERN = re.compile(r"cannot make enough memory available|out of memory", re.IGNORECASE)

# Retry policy when another program has filled the GPU memory
ENGINE_ATTEMPTS = 3
GPU_RETRY_DELAY_SECONDS = 8

# Adaptive GPU budget. Other programs on this PC can hold several GB of VRAM, and the engine's
# automatic placement then fails. An explicit --max-vram budget based on the free memory measured
# just before the job makes the engine stream weights in segments instead.
GPU_MEMORY_MARGIN_MB = 1024           # left free for other programs and driver overhead
MIN_GPU_BUDGET_GIB = 1.5
RETRY_BUDGET_FACTOR = 0.6             # each retry after a GPU failure uses a smaller budget
SMALL_VAE_TILES_BELOW_GIB = 3.5       # default VAE tiles need ~3.4 GB; smaller tiles need far less
CONSTRAINED_GPU_BELOW_GIB = 5.0       # below this the UI warns that the job will be slower

# User-facing messages (shown in the Hebrew UI)
MESSAGE_GPU_MEMORY_FULL = ("הזיכרון של כרטיס המסך מלא - תוכנה אחרת משתמשת בו כרגע "
                           "(למשל שרת Python אחר שרץ ברקע). ניסיתי 3 פעמים. "
                           "סגרו את התוכנה שתופסת את כרטיס המסך ונסו שוב.")
MESSAGE_ENGINE_FAILED = "המנוע נכשל: "
MESSAGE_BUSY = "יש כבר יצירה פעילה. חכו שתסתיים או בטלו אותה."
MESSAGE_SERVICE_UNAVAILABLE = "שירות הרקע לא מוגדר במחשב הזה."
MESSAGE_SERVICE_BUSY = "פעולה על שירות הרקע כבר רצה. חכו שתסתיים."
MESSAGE_SERVICE_START_FAILED = "הדלקת שירות הרקע נכשלה. הפרטים ב-logs\\background_service.log"
MESSAGE_SERVICE_STOP_FAILED = "כיבוי שירות הרקע נכשל. הפרטים ב-logs\\background_service.log"


class JobManager:
    """Runs one engine job at a time and exposes its live state to the HTTP handlers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._engine_process = None
        self._cancel_requested = False
        self._kill_on_close_job = create_kill_on_close_job()
        self._state = self._idle_state()

    @staticmethod
    def _idle_state():
        return {
            "state": "idle",          # idle | running | done | error | cancelled
            "phase": "",              # starting | loading | sampling | decoding
            "mode": "",
            "job_id": "",
            "image_index": 0,
            "image_count": 0,
            "step": 0,
            "total_steps": 0,
            "seconds_per_step": 0.0,
            "started_at": 0.0,
            "finished_at": 0.0,
            "results": [],
            "seeds": [],
            "error": "",
            "attempt": 1,
            "gpu_budget_gib": 0.0,
            "gpu_constrained": False,
        }

    def snapshot(self):
        with self._lock:
            state_copy = dict(self._state)
            state_copy["results"] = list(self._state["results"])
            state_copy["seeds"] = list(self._state["seeds"])
        now = time.time()
        if state_copy["started_at"]:
            end_time = state_copy["finished_at"] or now
            state_copy["elapsed_seconds"] = round(end_time - state_copy["started_at"], 1)
        else:
            state_copy["elapsed_seconds"] = 0
        return state_copy

    def is_running(self):
        with self._lock:
            return self._state["state"] == "running"

    def _update(self, **changes):
        with self._lock:
            self._state.update(changes)

    def start(self, job_request):
        """Validate the request, prepare inputs and start the engine thread. Returns the job id."""
        with self._lock:
            if self._state["state"] == "running":
                raise RuntimeError(MESSAGE_BUSY)
            job_id = "qwen_" + datetime.now().strftime("%Y%m%d-%H%M%S")
            self._cancel_requested = False
            self._state = self._idle_state()
            self._state.update({
                "state": "running",
                "phase": "starting",
                "mode": job_request["mode"],
                "job_id": job_id,
                "image_count": job_request["count"],
                "total_steps": job_request["steps"],
                "started_at": time.time(),
            })
        worker = threading.Thread(target=self._run_job, args=(job_id, job_request), daemon=True)
        worker.start()
        logger.info(f"{LOG_TAG}[JOB_LIFECYCLE] Job {job_id} accepted: mode={job_request['mode']} "
                    f"{job_request['width']}x{job_request['height']} steps={job_request['steps']} "
                    f"count={job_request['count']} refs={len(job_request['reference_images'])}")
        return job_id

    def cancel(self):
        with self._lock:
            self._cancel_requested = True
            engine_process = self._engine_process
        if engine_process and engine_process.poll() is None:
            engine_process.kill()
            logger.info(f"{LOG_TAG}[JOB_LIFECYCLE] Cancel requested; engine process killed")
            return True
        return False

    def _build_engine_arguments(self, job_request, job_temp_dir, output_pattern):
        prompt_text = job_request["prompt"]
        if job_request["transparent"]:
            prompt_text = TRANSPARENT_PROMPT_TEMPLATE.format(prompt=prompt_text.strip().rstrip("."))

        # Prompts go through UTF-8 files so non-English text and quotes survive intact
        prompt_file = job_temp_dir / "prompt.txt"
        negative_prompt_file = job_temp_dir / "negative_prompt.txt"
        prompt_file.write_text(prompt_text, encoding="utf-8")
        negative_prompt_file.write_text(job_request["negative_prompt"], encoding="utf-8")

        engine_arguments = [
            str(ENGINE_EXE),
            "--diffusion-model", str(DIFFUSION_MODEL),
            "--llm", str(TEXT_ENCODER),
            "--vae", str(VAE_MODEL),
            "--offload-to-cpu",
            "--diffusion-fa",
            "--vae-tiling",
            "--sampling-method", "euler",
            "--cfg-scale", f"{job_request['cfg_scale']:g}",
            "--steps", str(job_request["steps"]),
            "-W", str(job_request["width"]),
            "-H", str(job_request["height"]),
            "-s", str(job_request["seed"]),
            "-b", str(job_request["count"]),
            "--prompt-file", str(prompt_file),
            "--negative-prompt-file", str(negative_prompt_file),
            "-o", str(output_pattern),
        ]

        # Image editing: vision weights + each uploaded image as a reference (-r), in order
        if job_request["mode"] == "edit":
            engine_arguments += ["--llm_vision", str(VISION_ENCODER)]
            for reference_index, (image_bytes, extension) in enumerate(job_request["reference_images"]):
                reference_path = job_temp_dir / f"reference_{reference_index}.{extension}"
                reference_path.write_bytes(image_bytes)
                engine_arguments += ["-r", str(reference_path)]
        return engine_arguments

    def _handle_engine_segment(self, segment, parse_state):
        """Update job state from one line/segment of engine output (progress bars use \\r)."""
        image_start = IMAGE_START_PATTERN.search(segment)
        if image_start:
            with self._lock:
                self._state["image_index"] = int(image_start.group(1))
                self._state["image_count"] = int(image_start.group(2))
                self._state["seeds"].append(int(image_start.group(3)))
                self._state["phase"] = "sampling"
                self._state["step"] = 0
            return
        if DECODING_PATTERN.search(segment):
            parse_state["decoding"] = True
            self._update(phase="decoding")
            return
        saved_image = SAVED_IMAGE_PATTERN.search(segment)
        if saved_image:
            if saved_image.group(3) == "success":
                parse_state["saved_paths"].append(saved_image.group(2))
            return
        if GPU_MEMORY_FAILURE_PATTERN.search(segment):
            # Only acted on if the engine then exits with an error (it retries some OOMs itself)
            parse_state["gpu_memory_failure"] = True
        error_line = ERROR_LINE_PATTERN.search(segment)
        if error_line:
            # Engine logs recoverable errors too (e.g. OOM before retrying with tiling);
            # keep the last one to report only if the engine exits with a failure
            parse_state["last_error"] = error_line.group(1).strip()
            return
        progress = PROGRESS_PATTERN.search(segment)
        if not progress or parse_state["decoding"]:
            return
        current, total, rate, unit = int(progress.group(1)), int(progress.group(2)), float(progress.group(3)), progress.group(4)
        if unit == "MB/s":
            self._update(phase="loading")
            return
        seconds_per_step = rate if unit == "s/it" else (1.0 / rate if rate > 0 else 0.0)
        self._update(phase="sampling", step=current, total_steps=total, seconds_per_step=round(seconds_per_step, 2))
        logger.debug(f"{LOG_TAG}[JOB_PROGRESS] step {current}/{total} at {seconds_per_step:.2f}s/step")

    def _gpu_budget_arguments(self, previous_budget_gib):
        """Measure free VRAM now and return (extra engine arguments, budget in GiB or None)."""
        gpu = query_gpu_memory()
        if gpu is None:
            return [], None
        budget_gib = (gpu["free_mb"] - GPU_MEMORY_MARGIN_MB) / 1024
        if previous_budget_gib:
            budget_gib = min(budget_gib, previous_budget_gib * RETRY_BUDGET_FACTOR)
        budget_gib = round(max(MIN_GPU_BUDGET_GIB, budget_gib), 1)
        budget_arguments = ["--max-vram", f"{budget_gib:g}"]
        if budget_gib < SMALL_VAE_TILES_BELOW_GIB:
            budget_arguments += ["--vae-tile-size", "16x16"]
        self._update(gpu_budget_gib=budget_gib, gpu_constrained=budget_gib < CONSTRAINED_GPU_BELOW_GIB)
        logger.info(f"{LOG_TAG}[JOB_LIFECYCLE] GPU free {gpu['free_mb']} MB -> engine budget {budget_gib} GiB")
        return budget_arguments, budget_gib

    def _run_engine_once(self, job_id, engine_arguments, engine_log, parse_state):
        """Start sd-cli, stream and parse its output until it exits. Returns the exit code."""
        engine_log.write(("COMMAND: " + subprocess.list2cmdline(engine_arguments) + "\n\n").encode("utf-8"))
        engine_process = subprocess.Popen(
            engine_arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(PROJECT_ROOT),
            creationflags=(BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW) if os.name == "nt" else 0,
        )
        assign_process_to_job(self._kill_on_close_job, engine_process)
        with self._lock:
            self._engine_process = engine_process
            cancel_already_requested = self._cancel_requested
        if cancel_already_requested:
            engine_process.kill()
        logger.info(f"{LOG_TAG}[JOB_LIFECYCLE] Job {job_id} engine started (pid {engine_process.pid})")

        # Read raw output; progress bars rewrite the line with \r, so split on both \r and \n
        pending_text = ""
        while True:
            output_chunk = engine_process.stdout.read1(8192)
            if not output_chunk:
                break
            engine_log.write(output_chunk)
            pending_text += output_chunk.decode("utf-8", errors="replace")
            segments = re.split(r"[\r\n]", pending_text)
            pending_text = segments.pop()
            for raw_segment in segments:
                segment = ANSI_ESCAPE_PATTERN.sub("", raw_segment).strip()
                if segment:
                    logger.debug(f"{LOG_TAG}[ENGINE_OUTPUT] {segment}")
                    self._handle_engine_segment(segment, parse_state)
        if pending_text.strip():
            self._handle_engine_segment(ANSI_ESCAPE_PATTERN.sub("", pending_text).strip(), parse_state)
        return engine_process.wait()

    def _run_job(self, job_id, job_request):
        job_temp_dir = Path(tempfile.mkdtemp(prefix="qwen_webui_"))
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        output_name_pattern = f"{job_id}_%d.png" if job_request["count"] > 1 else f"{job_id}.png"
        output_pattern = OUTPUT_DIR / output_name_pattern
        engine_log_path = LOG_DIR / "last_job.log"

        try:
            engine_arguments = self._build_engine_arguments(job_request, job_temp_dir, output_pattern)
            with open(engine_log_path, "wb") as engine_log:
                # Other programs can briefly take all GPU memory; if the engine fails for that
                # reason, wait for it to free up and try again (the model load is repeated)
                gpu_budget_gib = None
                for attempt_number in range(1, ENGINE_ATTEMPTS + 1):
                    parse_state = {"decoding": False, "saved_paths": [], "last_error": "", "gpu_memory_failure": False}
                    budget_arguments, gpu_budget_gib = self._gpu_budget_arguments(gpu_budget_gib)
                    exit_code = self._run_engine_once(job_id, engine_arguments + budget_arguments, engine_log, parse_state)
                    with self._lock:
                        was_cancelled = self._cancel_requested
                    retry_worthwhile = (exit_code != 0 and parse_state["gpu_memory_failure"]
                                        and not parse_state["saved_paths"] and not was_cancelled)
                    if not retry_worthwhile or attempt_number == ENGINE_ATTEMPTS:
                        break
                    logger.warning(f"{LOG_TAG}[JOB_ERROR] Job {job_id} attempt {attempt_number} failed: GPU memory "
                                   f"taken by another program; retrying in {GPU_RETRY_DELAY_SECONDS}s")
                    self._update(phase="waiting_gpu", step=0, seeds=[], attempt=attempt_number + 1)
                    engine_log.write(f"\n\n===== RETRY {attempt_number + 1} (GPU memory was full) =====\n\n".encode("utf-8"))
                    time.sleep(GPU_RETRY_DELAY_SECONDS)
                    with self._lock:
                        if self._cancel_requested:
                            break
                    self._update(phase="starting")

            result_names = [Path(saved_path).name for saved_path in parse_state["saved_paths"]
                            if Path(saved_path).exists()]
            with self._lock:
                was_cancelled = self._cancel_requested
                seeds = list(self._state["seeds"])
            elapsed_seconds = round(time.time() - self._state["started_at"], 1)

            if was_cancelled:
                self._update(state="cancelled", phase="", finished_at=time.time(), results=result_names)
                logger.info(f"{LOG_TAG}[JOB_LIFECYCLE] Job {job_id} cancelled after {elapsed_seconds}s")
            elif exit_code == 0 and result_names:
                self._write_job_metadata(job_id, job_request, result_names, seeds, elapsed_seconds)
                self._update(state="done", phase="", finished_at=time.time(), results=result_names)
                logger.info(f"{LOG_TAG}[JOB_LIFECYCLE] Job {job_id} done in {elapsed_seconds}s: {result_names}")
            else:
                if parse_state["gpu_memory_failure"]:
                    user_message = MESSAGE_GPU_MEMORY_FULL
                else:
                    user_message = MESSAGE_ENGINE_FAILED + (parse_state["last_error"] or f"exit code {exit_code}")
                self._update(state="error", phase="", finished_at=time.time(), results=result_names,
                             error=user_message)
                logger.error(f"{LOG_TAG}[JOB_ERROR] Job {job_id} failed (exit {exit_code}): "
                             f"{parse_state['last_error'] or 'no error line'}")
        except Exception as unexpected_error:
            self._update(state="error", phase="", finished_at=time.time(),
                         error=MESSAGE_ENGINE_FAILED + str(unexpected_error))
            logger.exception(f"{LOG_TAG}[JOB_ERROR] Job {job_id} crashed: {unexpected_error}")
        finally:
            with self._lock:
                engine_process = self._engine_process
                self._engine_process = None
            if engine_process and engine_process.poll() is None:
                engine_process.kill()
            shutil.rmtree(job_temp_dir, ignore_errors=True)
            logger.info(f"{LOG_TAG}[FILE_OPS] Job {job_id} temp files removed")

    @staticmethod
    def _write_job_metadata(job_id, job_request, result_names, seeds, elapsed_seconds):
        """Save a <job_id>.json sidecar so the gallery can show and reuse the settings."""
        metadata = {
            "job_id": job_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "mode": job_request["mode"],
            "prompt": job_request["prompt"],
            "negative_prompt": job_request["negative_prompt"],
            "width": job_request["width"],
            "height": job_request["height"],
            "steps": job_request["steps"],
            "cfg_scale": job_request["cfg_scale"],
            "seed_requested": job_request["seed"],
            "seeds": seeds,
            "count": job_request["count"],
            "transparent": job_request["transparent"],
            "reference_count": len(job_request["reference_images"]),
            "duration_seconds": elapsed_seconds,
            "model": DIFFUSION_MODEL.name,
            "images": result_names,
        }
        metadata_path = OUTPUT_DIR / f"{job_id}.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Request validation and gallery helpers
# ---------------------------------------------------------------------------
DATA_URL_PATTERN = re.compile(r"^data:image/(png|jpeg|jpg|webp|bmp);base64,(.+)$", re.DOTALL)


def clamp_integer(value, minimum, maximum, default):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def parse_job_request(request_body):
    """Validate and normalize a /api/generate body. Raises ValueError with a user-facing message."""
    mode = request_body.get("mode", "txt2img")
    if mode not in ("txt2img", "edit"):
        raise ValueError("סוג פעולה לא מוכר")
    prompt = str(request_body.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("כתבו תיאור קודם")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"התיאור ארוך מ-{MAX_PROMPT_CHARS} תווים")

    # The model works on 32-pixel blocks: round down to a multiple of 32
    width = clamp_integer(request_body.get("width"), 256, 2048, 1024) // 32 * 32
    height = clamp_integer(request_body.get("height"), 256, 2048, 1024) // 32 * 32
    try:
        cfg_scale = max(1.0, min(20.0, float(request_body.get("cfg_scale", 6.0))))
    except (TypeError, ValueError):
        cfg_scale = 6.0

    reference_images = []
    if mode == "edit":
        if not VISION_ENCODER.exists():
            raise ValueError("רכיב העריכה לא מותקן (קובץ ה-mmproj חסר)")
        for data_url in (request_body.get("images") or [])[:MAX_REFERENCE_IMAGES]:
            data_url_match = DATA_URL_PATTERN.match(str(data_url))
            if not data_url_match:
                raise ValueError("אחת התמונות בפורמט שלא נתמך")
            try:
                image_bytes = base64.b64decode(data_url_match.group(2), validate=False)
            except (binascii.Error, ValueError):
                raise ValueError("לא הצלחתי לקרוא את אחת התמונות")
            if len(image_bytes) > MAX_REFERENCE_IMAGE_BYTES:
                raise ValueError("אחת התמונות גדולה מדי")
            extension = {"jpeg": "jpg"}.get(data_url_match.group(1), data_url_match.group(1))
            reference_images.append((image_bytes, extension))
        if not reference_images:
            raise ValueError("הוסיפו לפחות תמונה אחת לעריכה")

    return {
        "mode": mode,
        "prompt": prompt,
        "negative_prompt": str(request_body.get("negative_prompt", "")).strip()[:MAX_PROMPT_CHARS],
        "width": width,
        "height": height,
        "steps": clamp_integer(request_body.get("steps"), 1, 100, 20),
        "cfg_scale": cfg_scale,
        "seed": clamp_integer(request_body.get("seed"), -1, 2**31 - 1, -1),
        "count": clamp_integer(request_body.get("count"), 1, 8, 1),
        "transparent": bool(request_body.get("transparent", False)),
        "reference_images": reference_images,
    }


def query_gpu_memory():
    """Return free/total GPU memory in MB and utilization %, or None if nvidia-smi is unavailable."""
    try:
        smi_output = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        ).stdout.strip().splitlines()[0]
        free_megabytes, total_megabytes, utilization_percent = (int(value.strip()) for value in smi_output.split(","))
        return {"free_mb": free_megabytes, "total_mb": total_megabytes, "utilization_percent": utilization_percent}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


class BackgroundServiceControl:
    """Switches a configured background service off and on through its own management script.

    State comes from the service's health URLs, so it is right even when the service was started
    or stopped outside this UI. A start or stop runs in a background thread (a start can take
    minutes); while it runs, the state is starting/stopping and a second request is refused.
    """

    def __init__(self, settings=None, log_path=BACKGROUND_SERVICE_LOG):
        settings = BACKGROUND_SERVICE_SETTINGS if settings is None else settings
        self._script_path = Path(settings.get("script") or "")
        self._working_dir = Path(settings.get("working_dir") or self._script_path.parent)
        self._health_urls = dict(settings.get("health_urls") or {})
        self._label = settings.get("label") or DEFAULT_BACKGROUND_SERVICE_LABEL
        self._description = settings.get("description") or DEFAULT_BACKGROUND_SERVICE_DESCRIPTION
        self._log_path = Path(log_path)
        self._lock = threading.Lock()
        self._operation = ""          # "" | starting | stopping
        self._last_error = ""

    def available(self):
        return os.name == "nt" and bool(self._health_urls) and self._script_path.is_file()

    @staticmethod
    def _is_healthy(url):
        try:
            with urllib.request.urlopen(url, timeout=BACKGROUND_SERVICE_HEALTH_TIMEOUT_SECONDS) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def snapshot(self):
        """`state`: on | off | partial | starting | stopping | unavailable, plus per-service health."""
        if not self.available():
            return {"available": False, "state": "unavailable", "services": {}, "error": ""}
        with self._lock:
            operation, last_error = self._operation, self._last_error
        services = {name: self._is_healthy(url) for name, url in self._health_urls.items()}
        if operation:
            state = operation
        elif all(services.values()):
            state = "on"
        elif any(services.values()):
            state = "partial"
        else:
            state = "off"
        return {"available": True, "state": state, "services": services, "error": last_error,
                "label": self._label, "description": self._description}

    def request(self, action):
        """Start a background `start` or `stop`. Raises ValueError (bad request) or RuntimeError (busy)."""
        if action not in ("start", "stop"):
            raise ValueError("פעולה לא מוכרת")
        if not self.available():
            raise ValueError(MESSAGE_SERVICE_UNAVAILABLE)
        with self._lock:
            if self._operation:
                raise RuntimeError(MESSAGE_SERVICE_BUSY)
            self._operation = "starting" if action == "start" else "stopping"
            self._last_error = ""
        threading.Thread(target=self._run_script, args=(action,), daemon=True).start()
        logger.info(f"{LOG_TAG}[BACKGROUND_SERVICE] {action} requested")

    def _run_script(self, action):
        script_action = "Start" if action == "start" else "Stop"
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(self._script_path), script_action]
        error_message = ""
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "ab") as control_log:
                control_log.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} {script_action} =====\n".encode("utf-8"))
                control_log.flush()
                # Its own process group and no console: the services it starts must outlive this UI
                completed = subprocess.run(
                    command, stdout=control_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    cwd=str(self._working_dir), timeout=BACKGROUND_SERVICE_ACTION_TIMEOUT_SECONDS,
                    creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
                )
            if completed.returncode != 0:
                error_message = MESSAGE_SERVICE_START_FAILED if action == "start" else MESSAGE_SERVICE_STOP_FAILED
        except (OSError, subprocess.SubprocessError) as run_error:
            error_message = MESSAGE_SERVICE_START_FAILED if action == "start" else MESSAGE_SERVICE_STOP_FAILED
            logger.error(f"{LOG_TAG}[BACKGROUND_SERVICE] {script_action} could not run: {run_error}")
        finally:
            with self._lock:
                self._operation = ""
                self._last_error = error_message
        if error_message:
            logger.error(f"{LOG_TAG}[BACKGROUND_SERVICE] {script_action} failed; see {self._log_path}")
        else:
            logger.info(f"{LOG_TAG}[BACKGROUND_SERVICE] {script_action} finished")


def resolve_output_file(file_name):
    """Return the path of an image inside outputs/, or None if the name is unsafe or missing."""
    if not SAFE_FILE_NAME_PATTERN.match(file_name):
        return None
    candidate_path = (OUTPUT_DIR / file_name).resolve()
    if candidate_path.parent != OUTPUT_DIR.resolve() or not candidate_path.is_file():
        return None
    return candidate_path


def list_gallery_items():
    if not OUTPUT_DIR.exists():
        return []
    image_paths = sorted(OUTPUT_DIR.glob("*.png"), key=lambda path: path.stat().st_mtime, reverse=True)
    metadata_cache = {}
    gallery_items = []
    for image_path in image_paths[:GALLERY_LIMIT]:
        metadata = None
        name_match = JOB_IMAGE_NAME_PATTERN.match(image_path.name)
        if name_match:
            job_id = name_match.group(1)
            if job_id not in metadata_cache:
                metadata_path = OUTPUT_DIR / f"{job_id}.json"
                try:
                    metadata_cache[job_id] = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    metadata_cache[job_id] = None
            metadata = metadata_cache[job_id]
            # Report the seed of this specific image within a batch
            if metadata and metadata.get("seeds"):
                image_index = int(name_match.group(2) or 0)
                if image_index < len(metadata["seeds"]):
                    metadata = dict(metadata, seed=metadata["seeds"][image_index])
        gallery_items.append({
            "name": image_path.name,
            "url": f"/outputs/{image_path.name}",
            "modified": image_path.stat().st_mtime,
            "meta": metadata,
        })
    return gallery_items


def delete_gallery_image(file_name):
    """Permanently delete one image from outputs/ (not to the Recycle Bin). Returns True if deleted."""
    image_path = resolve_output_file(file_name)
    if not image_path:
        return False
    try:
        image_path.unlink()
    except OSError as delete_error:
        logger.warning(f"{LOG_TAG}[FILE_OPS] Could not delete {file_name}: {delete_error}")
        return False
    name_match = JOB_IMAGE_NAME_PATTERN.match(file_name)
    if name_match:
        # Remove the job's metadata sidecar once none of its images remain
        job_id = name_match.group(1)
        if not list(OUTPUT_DIR.glob(f"{job_id}.png")) and not list(OUTPUT_DIR.glob(f"{job_id}_*.png")):
            (OUTPUT_DIR / f"{job_id}.json").unlink(missing_ok=True)
    logger.info(f"{LOG_TAG}[FILE_OPS] Deleted {file_name}")
    return True


def delete_gallery_images(file_names, protected_job_id=""):
    """Delete several images. Images of the job that is still running are skipped.

    Returns (deleted_names, skipped_names).
    """
    deleted_names, skipped_names = [], []
    for file_name in dict.fromkeys(file_names):   # drop duplicates, keep order
        if protected_job_id and file_name.startswith(protected_job_id):
            skipped_names.append(file_name)
        elif delete_gallery_image(file_name):
            deleted_names.append(file_name)
        else:
            skipped_names.append(file_name)
    return deleted_names, skipped_names


def remove_orphan_sidecars():
    """Delete job settings files (<job_id>.json) whose images are all gone."""
    if not OUTPUT_DIR.exists():
        return
    for sidecar_path in OUTPUT_DIR.glob("qwen_*-*.json"):
        job_id = sidecar_path.stem
        if not list(OUTPUT_DIR.glob(f"{job_id}.png")) and not list(OUTPUT_DIR.glob(f"{job_id}_*.png")):
            sidecar_path.unlink(missing_ok=True)
            logger.info(f"{LOG_TAG}[FILE_OPS] Removed orphan settings file {sidecar_path.name}")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
job_manager = None
background_service_control = None
http_server = None


class WebUiRequestHandler(BaseHTTPRequestHandler):
    server_version = "QwenWebUI/1.0"

    def log_message(self, format_string, *format_args):
        logger.debug(f"{LOG_TAG}[API_REQUEST] {self.command} {self.path} - " + (format_string % format_args))

    def _send_json(self, payload, status=HTTPStatus.OK):
        response_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(response_bytes)

    def _send_file(self, file_path, content_type, cache_seconds=0):
        file_bytes = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(file_bytes)))
        self.send_header("Cache-Control", f"max-age={cache_seconds}" if cache_seconds else "no-store")
        self.end_headers()
        self.wfile.write(file_bytes)

    def _request_is_from_this_ui(self, require_json_body):
        """Refuse requests that other websites open in the same browser could forge.

        Any page can send requests to 127.0.0.1, so without these checks a malicious site could
        delete images, start jobs, shut the UI down or switch the background service:
        - Host must be this server: blocks DNS-rebinding pages that resolve to 127.0.0.1.
        - A browser Origin header, when sent, must be this server's own origin.
        - POST bodies must be declared JSON: a cross-site page can only send that after a CORS
          preflight, and this server never approves one. HTML forms cannot send JSON.
        Clients without a browser (PowerShell, tests) send no Origin and pass.
        """
        server_port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{server_port}", f"localhost:{server_port}"}
        request_host = self.headers.get("Host", "")
        request_origin = self.headers.get("Origin")
        content_type = self.headers.get("Content-Type", "").lower()
        refusal_reason = ""
        if request_host not in allowed_hosts:
            refusal_reason = f"Host {request_host!r}"
        elif request_origin and request_origin not in {f"http://{host}" for host in allowed_hosts}:
            refusal_reason = f"Origin {request_origin!r}"
        elif require_json_body and not content_type.startswith("application/json"):
            refusal_reason = f"Content-Type {content_type!r}"
        if refusal_reason:
            logger.warning(f"{LOG_TAG}[API_SECURITY] Refused {self.command} {self.path}: {refusal_reason}")
            self._send_json({"error": "Forbidden"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length > MAX_REQUEST_BYTES:
            raise ValueError("הבקשה גדולה מדי")
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        return json.loads(raw_body.decode("utf-8") or "{}")

    def do_GET(self):
        if not self._request_is_from_this_ui(require_json_body=False):
            return
        request_path = urlparse(self.path).path
        if request_path in ("/", "/index.html"):
            self._send_file(INDEX_HTML, "text/html; charset=utf-8")
        elif request_path == "/api/status":
            self._send_json(job_manager.snapshot())
        elif request_path == "/api/config":
            self._send_json({
                "model": DIFFUSION_MODEL.name,
                "model_installed": DIFFUSION_MODEL.exists(),
                "edit_available": VISION_ENCODER.exists(),
                "output_dir": str(OUTPUT_DIR),
            })
        elif request_path == "/api/gallery":
            total_images = len(list(OUTPUT_DIR.glob("*.png"))) if OUTPUT_DIR.exists() else 0
            self._send_json({"items": list_gallery_items(), "total": total_images})
        elif request_path == "/api/gpu":
            self._send_json({"gpu": query_gpu_memory()})
        elif request_path == "/api/background-service":
            self._send_json(background_service_control.snapshot())
        elif request_path.startswith("/outputs/"):
            image_path = resolve_output_file(unquote(request_path[len("/outputs/"):]))
            if image_path:
                self._send_file(image_path, "image/png", cache_seconds=3600)
            else:
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        elif request_path == "/logging-config":
            self._send_json({"enabled_categories": sorted(_enabled_categories),
                             "log_level": os.environ.get("LOG_LEVEL", "INFO")})
        elif request_path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        else:
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if not self._request_is_from_this_ui(require_json_body=True):
            return
        request_path = urlparse(self.path).path
        try:
            request_body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as body_error:
            self._send_json({"error": str(body_error)}, HTTPStatus.BAD_REQUEST)
            return

        if request_path == "/api/generate":
            try:
                job_request = parse_job_request(request_body)
                job_id = job_manager.start(job_request)
            except ValueError as validation_error:
                logger.warning(f"{LOG_TAG}[JOB_ERROR] Rejected request: {validation_error}")
                self._send_json({"error": str(validation_error)}, HTTPStatus.BAD_REQUEST)
                return
            except RuntimeError as busy_error:
                self._send_json({"error": str(busy_error)}, HTTPStatus.CONFLICT)
                return
            self._send_json({"job_id": job_id}, HTTPStatus.ACCEPTED)
        elif request_path == "/api/cancel":
            self._send_json({"cancelled": job_manager.cancel()})
        elif request_path == "/api/background-service":
            try:
                background_service_control.request(str(request_body.get("action", "")))
            except ValueError as request_error:
                self._send_json({"error": str(request_error)}, HTTPStatus.BAD_REQUEST)
                return
            except RuntimeError as busy_error:
                self._send_json({"error": str(busy_error)}, HTTPStatus.CONFLICT)
                return
            self._send_json(background_service_control.snapshot(), HTTPStatus.ACCEPTED)
        elif request_path == "/api/delete":
            # Body: {"name": "x.png"} | {"names": ["a.png", ...]} | {"all": true}
            delete_everything = bool(request_body.get("all"))
            if delete_everything:
                requested_names = [path.name for path in OUTPUT_DIR.glob("*.png")] if OUTPUT_DIR.exists() else []
            else:
                requested_names = request_body.get("names") or ([request_body["name"]] if request_body.get("name") else [])
            requested_names = [str(name) for name in requested_names if isinstance(name, str)]
            running_job_id = job_manager.snapshot()["job_id"] if job_manager.is_running() else ""
            deleted_names, skipped_names = delete_gallery_images(requested_names, running_job_id)
            if delete_everything:
                remove_orphan_sidecars()
            logger.info(f"{LOG_TAG}[FILE_OPS] Delete request: {len(deleted_names)} deleted, {len(skipped_names)} skipped")
            self._send_json({"deleted": deleted_names, "skipped": skipped_names})
        elif request_path == "/api/open-folder":
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(OUTPUT_DIR))
            logger.info(f"{LOG_TAG}[FILE_OPS] Opened output folder in Explorer")
            self._send_json({"opened": True})
        elif request_path == "/api/shutdown":
            job_manager.cancel()
            self._send_json({"shutting_down": True})
            logger.info(f"{LOG_TAG}[SERVER_LIFECYCLE] Shutdown requested from the UI")
            threading.Thread(target=http_server.shutdown, daemon=True).start()
        elif request_path == "/reload-logging-config":
            global _logging_config
            _logging_config = None
            load_logging_config()
            self._send_json({"status": "reloaded", "enabled_categories": sorted(_enabled_categories)})
        else:
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)


STALE_TEMP_DIR_SECONDS = 6 * 3600


def remove_stale_job_temp_dirs():
    """Delete temp job folders left behind when a previous server was killed mid-job."""
    temp_root = Path(tempfile.gettempdir())
    for stale_dir in temp_root.glob("qwen_webui_*"):
        try:
            if stale_dir.is_dir() and time.time() - stale_dir.stat().st_mtime > STALE_TEMP_DIR_SECONDS:
                shutil.rmtree(stale_dir, ignore_errors=True)
                logger.info(f"{LOG_TAG}[FILE_OPS] Removed stale temp folder {stale_dir.name}")
        except OSError:
            continue


def main():
    global job_manager, background_service_control, http_server
    argument_parser = argparse.ArgumentParser(description="Qwen-Image-2.1 local web UI")
    argument_parser.add_argument("--port", type=int, default=7860)
    argument_parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    arguments = argument_parser.parse_args()

    missing_files = [str(path) for path in (ENGINE_EXE, DIFFUSION_MODEL, TEXT_ENCODER, VAE_MODEL, INDEX_HTML)
                     if not path.exists()]
    if missing_files:
        logger.error(f"{LOG_TAG}[SERVER_LIFECYCLE] Missing files: {missing_files}")
        return 1

    ui_url = f"http://127.0.0.1:{arguments.port}/"
    try:
        http_server = ThreadingHTTPServer(("127.0.0.1", arguments.port), WebUiRequestHandler)
    except OSError:
        # Port taken: most likely the UI is already running, so just open it
        logger.info(f"{LOG_TAG}[SERVER_LIFECYCLE] Port {arguments.port} is in use; opening the existing UI")
        if not arguments.no_browser:
            webbrowser.open(ui_url)
        return 0

    remove_stale_job_temp_dirs()
    job_manager = JobManager()
    background_service_control = BackgroundServiceControl()
    print(f"\n  Qwen Image web UI: {ui_url}\n  Close this window (or use the power button in the UI) to stop.\n", flush=True)
    logger.info(f"{LOG_TAG}[SERVER_LIFECYCLE] Listening on {ui_url}")
    if not arguments.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(ui_url,)).start()
    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        job_manager.cancel()
        http_server.server_close()
        logger.info(f"{LOG_TAG}[SERVER_LIFECYCLE] Server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
