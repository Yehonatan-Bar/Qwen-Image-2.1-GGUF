# Generate Client (command-line, one-shot)

## Purpose

Command-line alternative to the Web UI, for scripting. One PowerShell call loads the model,
creates or edits image(s), and exits. Nothing stays running afterwards: all RAM and VRAM is
released when the command returns.

## Key file

`C:\projects\Qwen-Image-2.1-GGUF\src\generate.ps1` runs `sd\sd-cli.exe`.

## Parameters

| Param | Default | Notes |
|-------|---------|-------|
| `-Prompt` (positional, required) | - | English works best; text inside quotes is rendered on the image |
| `-NegativePrompt` | `''` | What to avoid |
| `-Width` / `-Height` | `1024` | Rounded down to a multiple of 32, range 256-2048. With `-Image` and no explicit size: first image's aspect ratio at ~1 MP |
| `-Steps` | `20` | More steps = more detail, linear time cost |
| `-CfgScale` | `6.0` | Prompt adherence |
| `-Seed` | `-1` | `-1` = random; a fixed value reproduces an image |
| `-Count` | `1` | Images per run (1-50); all share one model load |
| `-Image` | none | One or more images to edit, in order (enables edit mode, adds `--llm_vision` + `-r`) |
| `-Transparent` | off | Wraps the prompt in the official RGBA format so the output PNG has a transparent background |
| `-OutputDir` | `<project>\outputs` | Created if missing |
| `-Open` | off | Opens the saved image(s) in the default viewer |

## Flow

1. Validates the required files (sd-cli.exe, UC diffusion model, text encoder, VAE, plus the vision encoder when `-Image` is used) and the input images. Without `-Transparent`, any input with an alpha channel is flattened onto white into `%TEMP%\qwen_flat_*.png` (deleted afterwards). Otherwise the hidden colors under transparent pixels leak into the edit.
2. Writes the prompt and negative prompt to UTF-8 temp files (`%TEMP%\qwen_prompt_<ts>.txt`) and passes them with `--prompt-file` / `--negative-prompt-file`. Native command-line arguments would mangle non-English text and embedded quotes.
3. Starts `sd-cli.exe` with `Start-Process -NoNewWindow -PassThru` (progress prints in the same window) and sets `PriorityClass = BelowNormal`.
4. Waits for exit. A `finally` block kills the engine if the script is interrupted (Ctrl+C) and deletes the temp prompt files.
5. Collects `outputs\qwen_<yyyyMMdd-HHmmss>.png` (or `_0.png`, `_1.png`... when `-Count` > 1, via sd-cli's `%d` pattern). No metadata sidecar is written; the gallery shows these images without settings.

## Engine flags

`--offload-to-cpu --diffusion-fa --vae-tiling --sampling-method euler`, plus an adaptive `--max-vram <free VRAM - 1 GB>`
(minimum 1.5 GiB) read from nvidia-smi. Below 3.5 GiB it also passes `--vae-tile-size 16x16`. When other programs
hold GPU memory, this makes the engine stream weights instead of failing (see "Adaptive GPU budget" in `WEB_UI.md`).
The script prints a notice when the budget is under 5 GiB. It does not retry.

## Examples

```powershell
C:\projects\Qwen-Image-2.1-GGUF\src\generate.ps1 "a red fox reading a newspaper in a cafe, photorealistic" -Open
C:\projects\Qwen-Image-2.1-GGUF\src\generate.ps1 "movie poster with the title 'NIGHT SHIFT'" -Width 768 -Height 1152 -Steps 30 -Seed 42
C:\projects\Qwen-Image-2.1-GGUF\src\generate.ps1 "Change the text on the sign to 'Closed'" -Image C:\photos\sign.png -Open
C:\projects\Qwen-Image-2.1-GGUF\src\generate.ps1 "a red apple" -Transparent
```

## Interactions

- Independent of the Web UI (`WEB_UI.md`); both write to `outputs\`.
