# Web UI

## Purpose

Browser interface (Hebrew, RTL) for everything the model does: text-to-image, image editing with
1-4 reference images, transparent-background output, gallery with reuse, edit, and delete.
The model runs only while a job is active. Each job starts `sd-cli.exe`, which exits when the job
ends, so the idle UI holds no model RAM/VRAM (the Python server itself is ~20 MB).

## Key files

| Path | Role |
|------|------|
| `C:\projects\Qwen-Image-2.1-GGUF\src\start-webui.ps1` | Launcher: finds Python, runs the server, which opens the browser |
| `C:\projects\Qwen-Image-2.1-GGUF\src\webui\server.py` | HTTP server + job runner (Python standard library only) |
| `C:\projects\Qwen-Image-2.1-GGUF\src\webui\index.html` | Single-file frontend (inline CSS/JS) |
| `C:\projects\Qwen-Image-2.1-GGUF\src\webui\logging_config.json` | Log category switches |
| `C:\projects\Qwen-Image-2.1-GGUF\src\webui\local_settings.json` | Machine-specific settings (background service switch); gitignored, template `local_settings.example.json` |
| `C:\projects\Qwen-Image-2.1-GGUF\logs\last_job.log` | Full engine command + output of the most recent job |
| `C:\projects\Qwen-Image-2.1-GGUF\tests\test_webui_integration.py` | End-to-end test (real generations) |
| `C:\projects\Qwen-Image-2.1-GGUF\tests\test_kill_on_close.py` | Verifies a child in the job object dies when the parent dies abruptly |
| `C:\projects\Qwen-Image-2.1-GGUF\tests\fixtures\gpu_memory_hog.py` | Holds N MB of VRAM for N seconds (CUDA driver API) to simulate a busy GPU |
| `C:\projects\Qwen-Image-2.1-GGUF\tests\test_background_service.py` | Background service switch against a fake script and fake health endpoints |
| `C:\projects\Qwen-Image-2.1-GGUF\tests\test_gallery_delete.py` | Single, batch, and delete-all; settings-file cleanup; unsafe names. Uses a temp output folder with fake images; seconds, no model |
| `C:\projects\Qwen-Image-2.1-GGUF\tests\test_request_guard.py` | Forged requests (foreign Host/Origin, non-JSON POST) are refused; the UI's own requests pass |
| `C:\projects\Qwen-Image-2.1-GGUF\logs\background_service.log` | Output of every background service start/stop run |

`test_webui_integration.py` and `test_gallery_delete.py` point the server at a temporary output folder through env
`QWEN_WEBUI_OUTPUT_DIR`, so they never touch the user's `outputs\`. Without that variable the server uses `<project>\outputs`.

## Usage

```powershell
C:\projects\Qwen-Image-2.1-GGUF\src\start-webui.ps1              # opens http://127.0.0.1:7860/
C:\projects\Qwen-Image-2.1-GGUF\src\start-webui.ps1 -Port 8080 -NoBrowser
```

Stop the UI by closing the window or using the "כבה את הממשק" button. A running job is killed with it.
If the port is already taken (the UI is already running), the launcher just opens the browser.

## Architecture and data flow

```
browser --POST /api/generate (JSON, images as PNG data URLs)--> server.py
  JobManager.start()  -> validate, reject if busy (409), start worker thread
  worker thread       -> temp dir: prompt.txt, negative_prompt.txt, reference_N.png
                      -> subprocess sd-cli.exe (BELOW_NORMAL priority, no window, in kill-on-close job object)
                      -> reads stdout, splits on \r and \n, parses progress
                      -> outputs\qwen_YYYYMMDD-HHMMSS[_N].png + qwen_YYYYMMDD-HHMMSS.json sidecar
                      -> deletes temp dir
browser --GET /api/status every 1 s while running--> phase, image i/n, step, s/step, elapsed
```

### Engine output parsing (`JobManager._handle_engine_segment`)

| Engine text | Effect |
|-------------|--------|
| `generating image: i/n - seed S` | image index, seed recorded, phase `sampling` |
| `\| a/b - X MB/s` | phase `loading` (weights being read) |
| `\| a/b - X s/it` or `it/s` | step `a` of `b`, seconds per step |
| `decoding N latents` | phase `decoding` (later progress bars ignored) |
| `save result image i to '...' (success)` | result file collected |
| `[ERROR ...] - msg` | kept as last error; reported only if exit code != 0 (the engine logs recoverable OOM retries as errors) |
| `cannot make enough memory available` / `out of memory` | marks a GPU memory failure (acted on only if the engine then fails) |

### Adaptive GPU budget and retry

Other programs on this PC can hold several GB of VRAM (e.g. another Python server using CUDA). With about 4 GB
taken, the engine's automatic placement stages the whole 4.3 GB text encoder or 4.4 GB DiT on the card. It then
fails with `reported free 0.00 MB` / `cannot make enough memory available`. `--max-vram -N` (reserve) and
`--backend te=cpu` do not prevent this.

Fix: before every attempt, `JobManager._gpu_budget_arguments` reads free VRAM (nvidia-smi) and passes
`--max-vram <free - 1 GB>` (minimum 1.5 GiB). The engine then splits the models into segments and streams weights.
Below 3.5 GiB it also passes `--vae-tile-size 16x16`, because default VAE tiles need about 3.4 GB.

| Condition (measured, 1024x1024) | Result |
|---------------------------------|--------|
| GPU free (6.5 GB), budget 5.4 GiB vs no budget | Same speed: 6.2 s/step, decode about 11 s |
| 4.1 GB taken by others, no budget | Fails immediately |
| 4.1 GB taken by others, budget 2 GiB + small VAE tiles | Works: about 12 s/step, decode about 29 s |

If an attempt still fails for GPU memory, the job retries up to `ENGINE_ATTEMPTS = 3` times. It waits
`GPU_RETRY_DELAY_SECONDS = 8` between tries and uses a budget 0.6x smaller each time. Status fields: `phase` =
`waiting_gpu`, `attempt`, `gpu_budget_gib`, and `gpu_constrained` (budget < 5 GiB). When constrained, the UI shows
"economy mode, slower". If every try fails, the UI shows `MESSAGE_GPU_MEMORY_FULL` (Hebrew).

The header shows free VRAM from `/api/gpu`. It turns amber below 2 GB and refreshes on load, after each job, and
every 30 s while idle.

### Background service switch

Another program on the PC may run a service that holds the GPU or CPU.
The header can switch it off while images are made (full CPU and GPU for the engine) and back on
afterwards. Everything about the service is machine-specific, so it is configured only in
`src\webui\local_settings.json` (gitignored; `local_settings.example.json` is the committed template):

| Key under `background_service` | Meaning |
|--------------------------------|---------|
| `script` | PowerShell script called as `-File <script> Start` / `Stop`; exit code 0 = success |
| `working_dir` | Working directory for the script (default: the script's folder) |
| `health_urls` | `{name: url}`; each URL answers 200 while that part of the service is up |
| `label` | Short Hebrew name for the pill and button (default `שירות רקע`) |
| `description` | Tooltip of the pill (default: a generic Hebrew explanation) |

- Without a `script` file and at least one health URL, the switch is hidden.
- `BackgroundServiceControl` (server.py) never kills processes itself. It runs the service's own
  script in a background thread with no console and its own process group, so the services it starts
  outlive this UI. Output is appended to `logs\background_service.log`. A run may take up to 7 minutes.
- State comes from the health URLs on every status request (1.5 s timeout each), so it is right even
  when the service was switched outside the UI: `on` (all 200), `partial` (some), `off` (none),
  `starting` / `stopping` while a run is in progress, `unavailable` when not configured.
  A second request during a run gets 409.
- A run that exits non-zero sets `error` (Hebrew, points to the log) until the next run.
- The UI never restarts the service by itself: off stays off until it is switched on again.
- Frontend: a pill `<label>: <state>` (amber dot = on and using the machine, green = off, pulsing blue =
  switching, red border = the last run failed) and a button (`כבה <label>` asks for confirmation /
  `הדלק <label>`). Refreshed every 30 s, every 2 s while switching; the GPU pill refreshes when a switch finishes.

### Safety of the "model is off when idle" guarantee

- Each job is a separate `sd-cli.exe` process, which exits at the end.
- The server creates a Windows job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and assigns every engine process to it. If the server is killed or its window closed, Windows kills the engine.
- Cancel, shutdown, and Ctrl+C all kill the engine explicitly.
- If the server is killed mid-job, its `%TEMP%\qwen_webui_*` folder remains. `remove_stale_job_temp_dirs()` deletes such folders older than 6 hours at the next server start.

### Request guard (cross-site protection)

Any website open in the same browser can send requests to `127.0.0.1`. Without a guard, a malicious page could
start jobs, delete images, shut the UI down or switch the background service. `WebUiRequestHandler._request_is_from_this_ui`
runs first in `do_GET` and `do_POST` and answers **403** (logged under `API_SECURITY`) unless:

| Check | Applies to | Why |
|-------|------------|-----|
| `Host` is `127.0.0.1:<port>` or `localhost:<port>` | GET and POST | Blocks DNS-rebinding pages that resolve their own domain to 127.0.0.1 |
| `Origin`, if present, is `http://127.0.0.1:<port>` or `http://localhost:<port>` | GET and POST | Browsers send Origin on cross-site requests; `null` (sandboxed/file pages) is refused |
| `Content-Type` starts with `application/json` | POST | Cross-site pages can send JSON only after a CORS preflight, which is never approved. HTML forms cannot send JSON |

Non-browser clients (PowerShell, Python tests) send no `Origin` and pass, as long as POST bodies are sent as JSON
(`-ContentType 'application/json; charset=utf-8'`). The frontend's `apiRequest` always sends JSON for POST.

## API endpoints

| Method | Path | Body / params | Response |
|--------|------|---------------|----------|
| GET | `/` | - | UI page |
| GET | `/api/config` | - | `model`, `model_installed`, `edit_available`, `output_dir` |
| GET | `/api/status` | - | `state` (idle/running/done/error/cancelled), `phase` (starting/loading/sampling/decoding/waiting_gpu), `mode`, `job_id`, `image_index`, `image_count`, `step`, `total_steps`, `seconds_per_step`, `elapsed_seconds`, `results[]`, `seeds[]`, `error` (Hebrew), `attempt` |
| GET | `/api/gpu` | - | `gpu`: `free_mb`, `total_mb`, `utilization_percent` (null if nvidia-smi fails) |
| GET | `/api/background-service` | - | `available`, `state` (on/off/partial/starting/stopping/unavailable), `services` {name: healthy}, `error` (Hebrew, last failed run), `label`, `description` (the last two only when available) |
| POST | `/api/background-service` | `{action: "start" \| "stop"}` | 202 + the status above; 400 unknown action or not configured; 409 a run is in progress |
| POST | `/api/generate` | `mode` (`txt2img`/`edit`), `prompt`, `negative_prompt`, `width`, `height` (256-2048, rounded down to /32), `steps` (1-100), `cfg_scale` (1-20), `seed` (-1 random), `count` (1-8), `transparent`, `images[]` (data URLs, edit only, max 4) | 202 `{job_id}`; 400 validation error (Hebrew); 409 busy |
| POST | `/api/cancel` | `{}` | `{cancelled}` |
| GET | `/api/gallery` | - | `items[]` (newest first, max 300): `name`, `url`, `modified`, `meta` (sidecar JSON + per-image `seed`); `total` = all PNGs on disk |
| GET | `/outputs/<name>.png` | - | image (name validated, must be inside `outputs/`) |
| POST | `/api/delete` | `{name}`, `{names: [...]}` or `{all: true}` | Permanently deletes the PNGs from `outputs\` (not to the Recycle Bin). Removes a job's `.json` sidecar when none of its images remain; `all` also removes orphan sidecars and leaves non-PNG files alone. Returns `{deleted: [...], skipped: [...]}`. Skipped means unsafe name, missing, or part of the job still running. |
| POST | `/api/open-folder` | `{}` | opens `outputs\` in Explorer |
| POST | `/api/shutdown` | `{}` | cancels any job and stops the server |
| GET | `/logging-config` | - | enabled log categories |
| POST | `/reload-logging-config` | `{}` | re-reads `logging_config.json` |

Example from PowerShell (server running):

```powershell
$body = @{ mode = 'txt2img'; prompt = 'a lighthouse in a storm'; width = 1024; height = 1024 } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:7860/api/generate -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod http://127.0.0.1:7860/api/status   # poll until state is done; results[] are under /outputs/
```

## Frontend behavior (index.html)

- Tabs: text-to-image and editing (disabled if the vision encoder file is missing). `http://127.0.0.1:7860/#edit` opens in edit mode.
- Bidi: sizes, ratios, times, and paths are wrapped in Unicode LTR isolates (`ltrIsolate`, U+2066/U+2069), and English examples in `<bdi dir="ltr">`, so they are not scrambled inside Hebrew text.
- Edit images: file picker, drag and drop, or paste (Ctrl+V). Browser converts to PNG (keeps alpha) and downscales to max 2048 px on the long side.
- Size presets for text-to-image: 1024x1024, 864x1152, 1152x864, 768x1344, 1344x768, 512x512, and custom. Edit adds "by first image" at ~1 MP and ~0.4 MP (aspect preserved, /32).
- Time estimate: 45 s load (60 s for edit) + count x (steps x 6.0 s x MP x edit factor + 8 s). Edit factor = 1 + 0.9 x number of reference images (measured ~2x with one reference).
- Progress card: phase, image i/n, step, s/step, elapsed, remaining; cancel button.
- Result actions: download, "edit this image" (adds it as a reference), details, delete. Gallery lightbox: download, edit, "load its settings", delete (also the Delete key).
- Deleting images (always permanent, after a `confirm()` that says the files cannot be recovered):
  - Each gallery thumbnail has its own "מחק" button, always visible.
  - The gallery header shows the image count and has "בחירה ומחיקה" (selection mode), "מחק הכל" and "רענן".
  - In selection mode, clicks toggle a check mark instead of opening the lightbox. The header then offers "בחר הכל"/"נקה בחירה", "מחק N נבחרות" and "סיום". Esc leaves selection mode.
  - `deleteImages(names, deleteEverything)` sends one `/api/delete` request. Deleted images are removed from the gallery, the latest-results panel, and an open lightbox. Skipped images are reported in the error box.
- Transparent option wraps the prompt: `This is an RGBA image with transparency. <prompt>. The image has alpha channel and the background is transparent.` Images are shown on a checkerboard.
- Transparent inputs: transparent pixels still hold hidden colors (often noise), and the model sees them. An edit of a transparent image without the option came out with a solid blue background. So when an uploaded reference has any pixel with alpha < 250, the UI checks the transparent option and shows an info note. If the user unchecks it, `flattenOnWhite` paints the transparent areas white before sending.

## Logging

Pattern `logger.info(f"{LOG_TAG}[CATEGORY] ...")` with `LOG_TAG = "[QWEN_WEBUI]"`. Categories in
`logging_config.json`: `SERVER_LIFECYCLE`, `JOB_LIFECYCLE`, `JOB_ERROR`, `FILE_OPS`, `BACKGROUND_SERVICE`, `API_SECURITY` (on);
`API_REQUEST`, `JOB_PROGRESS`, `ENGINE_OUTPUT` (off, verbose). Env vars: `LOG_LEVEL`,
`LOGGING_ENABLED`, `LOGGING_CONFIG_PATH`.

## Known limitations

- One job at a time (no queue); a second request gets 409.
- When other programs hold VRAM, jobs run in economy mode (about 2x slower per step at 1024). If they hold nearly all of it, jobs fail after 3 tries, and the only fix is to close that program.
- Every job reloads the model (~40-60 s); use the count setting for several images from one load.
- Editing is ~2x slower per step than text-to-image with one reference image, and slower still with more.
- The GPU has no priority control, so the desktop can feel less smooth while a job runs.
- Bound to 127.0.0.1 only (not reachable from other devices).

## Interactions

- Uses the same model files and engine flags as the command-line generator (`GENERATE_CLIENT.md`).
- Switches an optional background service only through the script named in `local_settings.json`.
