# Qwen-Image-2.1 Local - Technical Map

## Architecture overview

Local image creation and editing with Qwen-Image-2.1 (uncensored "UC" weights) on Windows
(RTX 2080 8 GB, 64 GB RAM). The engine is the prebuilt CUDA build of stable-diffusion.cpp (`sd-cli.exe`).
The model is loaded only while a job runs: every job is a separate engine process that exits when done.

```
Browser UI  --HTTP-->  src/webui/server.py (127.0.0.1:7860, ~20 MB idle)
                          '-- per job --> sd-cli.exe (BelowNormal priority, killed if the server dies)
Command line:  src/generate.ps1 ------------> sd-cli.exe
                                                 |- Qwen3-VL-8B text encoder (GGUF Q4_K_M)
                                                 |- Qwen3-VL-8B vision projector (mmproj F16, edit only)
                                                 |- Qwen-Image-2.1 UC DiT (GGUF Q4_K_M)
                                                 '- Qwen-Image-2.1 VAE (bf16)
                                              --> outputs\qwen_<timestamp>[_N].png (+ .json settings from the UI)
```

Weights sit in system RAM (`--offload-to-cpu`) and are streamed into VRAM on demand. Before each job the free VRAM is
measured and passed as an explicit budget (`--max-vram`). Other programs on this PC use the GPU too; the budget lets
jobs still run in that case, only slower.

## Directory structure

```
Qwen-Image-2.1-GGUF/
├── src/
│   ├── start-webui.ps1        # Opens the browser interface (main entry point)
│   ├── generate.ps1           # Command-line one-shot create/edit
│   └── webui/
│       ├── server.py          # HTTP server + on-demand engine runner (stdlib only)
│       ├── index.html         # Hebrew RTL single-page UI
│       ├── logging_config.json
│       └── local_settings.example.json  # Template for local_settings.json (machine settings, not in git)
├── tests/
│   ├── test_webui_integration.py  # End-to-end UI server test (real generations)
│   ├── test_kill_on_close.py      # Engine dies if the UI server dies (job object)
│   ├── test_background_service.py # Background service switch (fake script, fake health endpoints)
│   ├── test_request_guard.py      # Forged cross-site requests are refused (seconds, no model)
│   ├── test_gallery_delete.py     # Gallery delete: single/batch/all, sidecars, unsafe names (seconds, no model)
│   └── fixtures/gpu_memory_hog.py # Holds VRAM to simulate another program using the GPU
├── sd/                        # stable-diffusion.cpp CUDA 12 binaries (sd-cli.exe, DLLs) - not in git
├── models/                    # Model weights - not in git
│   ├── diffusion_models/      # qwen-image-2.1-UC-Q4_K_M.gguf (4.6 GB)
│   ├── text_encoders/         # Qwen3VL-8B-Instruct-Q4_K_M.gguf (5.0 GB), mmproj-Qwen3VL-8B-Instruct-F16.gguf (1.2 GB)
│   └── vae/                   # qwen_image_2.1_vae_bf16.safetensors (0.7 GB)
├── outputs/                   # Generated images + per-job settings JSON - not in git
├── logs/                      # last_job.log (engine command + output of the latest UI job) - not in git
├── Kingdom_of_Claudes_Beloved_MDs/  # Detail docs
├── .gitignore                 # Keeps weights, binaries, outputs, logs and secrets out of git
└── TECHNICAL.md               # This file
```

## Component index

**[Web UI]** - Browser interface for text-to-image, editing (1-4 reference images), transparent output, a gallery, and cancel/shutdown. Starts the engine per job and releases it when the job ends. Retries when another program fills the GPU and shows free VRAM. Has an optional on/off switch for a background service configured in `local_settings.json`, to free the whole machine while images are made.
> Detail: `Kingdom_of_Claudes_Beloved_MDs/WEB_UI.md`

**[Generate Client]** - `generate.ps1` does the same create/edit from the command line in one shot (load, generate, exit) at BelowNormal priority.
> Detail: `Kingdom_of_Claudes_Beloved_MDs/GENERATE_CLIENT.md`

## Configuration & environment

| Item | Value |
|------|-------|
| UI address | `http://127.0.0.1:7860` (`-Port` on start-webui.ps1) |
| Log switches | `src/webui/logging_config.json`; env `LOG_LEVEL`, `LOGGING_ENABLED`, `LOGGING_CONFIG_PATH` |
| Background service switch | `background_service` in `src/webui/local_settings.json` (gitignored; template `local_settings.example.json`): script, health URLs, label; hidden if unset |
| Runtime build | stable-diffusion.cpp `master-908-88411ef`, `win-cuda12-x64` + cudart |
| Model source | `huggingface.co/abenzerps/Qwen-Image-2.1-GGUF` (branch `main`, UC Q4_K_M, SHA256 `e79c8a00...1b41`) |
| Text/vision encoder source | `huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF` |
| VAE source | same repo as the model, `vae/qwen_image_2.1_vae_bf16.safetensors` |

## Setup & commands

```powershell
# Browser interface (opens automatically; close the window to stop)
C:\projects\Qwen-Image-2.1-GGUF\src\start-webui.ps1

# Command line
C:\projects\Qwen-Image-2.1-GGUF\src\generate.ps1 "a cat holding a sign that says 'Hello'" -Open
C:\projects\Qwen-Image-2.1-GGUF\src\generate.ps1 "Change the text to 'Bye'" -Image .\outputs\some.png

# Test (runs real small generations, a few minutes)
python C:\projects\Qwen-Image-2.1-GGUF\tests\test_webui_integration.py
# Background service switch test (no real service is touched, about a minute)
python C:\projects\Qwen-Image-2.1-GGUF\tests\test_background_service.py
# Cross-site request guard (seconds)
python C:\projects\Qwen-Image-2.1-GGUF\tests\test_request_guard.py
# Gallery delete (seconds, uses a temp folder - never touches outputs\)
python C:\projects\Qwen-Image-2.1-GGUF\tests\test_gallery_delete.py
```

## Dependencies

| Package | Purpose |
|---------|---------|
| stable-diffusion.cpp (sd-cli.exe, ggml-cuda.dll) | Inference engine |
| CUDA 12 runtime DLLs (cublas, cudart) | GPU compute; bundled in `sd/` |
| Python 3.12 (standard library only) | Web UI server |
| NVIDIA driver >= CUDA 12 | Installed (591.86, CUDA 13.1) |
