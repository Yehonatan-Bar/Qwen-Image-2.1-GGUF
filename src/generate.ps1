<#
.SYNOPSIS
    Creates or edits images with Qwen-Image-2.1 in one shot: loads the model, generates, exits.

.DESCRIPTION
    Command-line alternative to the browser interface (start-webui.ps1).
    Runs sd-cli.exe directly (no server). When it finishes, the process exits and all
    RAM / VRAM is released, so nothing keeps running in the background.
    The engine runs at BelowNormal CPU priority so the desktop stays responsive.
    Images are saved to the outputs\ folder with a timestamp in the file name.
    With -Image the prompt describes an edit to the given image(s).

.EXAMPLE
    .\src\generate.ps1 "a red fox reading a newspaper in a cafe, photorealistic" -Open
    .\src\generate.ps1 "a poster that says 'SALE 50%'" -Width 768 -Height 1024 -Steps 30
    .\src\generate.ps1 "a watercolor lighthouse" -Count 4
    .\src\generate.ps1 "Change the text on the sign to 'Closed'" -Image C:\photos\sign.png -Open
    .\src\generate.ps1 "a red apple" -Transparent
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Prompt,
    [string]$NegativePrompt = '',
    [int]$Width = 1024,
    [int]$Height = 1024,
    [int]$Steps = 20,
    [double]$CfgScale = 6.0,
    [long]$Seed = -1,
    # Several images in one run share a single model load
    [ValidateRange(1, 50)]
    [int]$Count = 1,
    # Image(s) to edit, in order; enables edit mode
    [string[]]$Image = @(),
    # Transparent background (RGBA PNG) using the official prompt format
    [switch]$Transparent,
    [string]$OutputDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'outputs'),
    [switch]$Open
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$cliExe = Join-Path $projectRoot 'sd\sd-cli.exe'
$diffusionModelPath = Join-Path $projectRoot 'models\diffusion_models\qwen-image-2.1-UC-Q4_K_M.gguf'
$textEncoderPath = Join-Path $projectRoot 'models\text_encoders\Qwen3VL-8B-Instruct-Q4_K_M.gguf'
$visionEncoderPath = Join-Path $projectRoot 'models\text_encoders\mmproj-Qwen3VL-8B-Instruct-F16.gguf'
$vaePath = Join-Path $projectRoot 'models\vae\qwen_image_2.1_vae_bf16.safetensors'

$requiredFiles = @($cliExe, $diffusionModelPath, $textEncoderPath, $vaePath)
if ($Image.Count -gt 0) { $requiredFiles += $visionEncoderPath }
foreach ($requiredFile in $requiredFiles) {
    if (-not (Test-Path $requiredFile)) {
        Write-Error "Missing file: $requiredFile"
        exit 1
    }
}

# Validate every input first so an early exit never leaves temporary files behind
foreach ($imagePath in $Image) {
    if (-not (Test-Path $imagePath)) {
        Write-Error "Image not found: $imagePath"
        exit 1
    }
}

$referenceImagePaths = @()
$flattenedTempFiles = @()
if ($Image.Count -gt 0) { Add-Type -AssemblyName System.Drawing }
foreach ($imagePath in $Image) {
    $resolvedImagePath = (Resolve-Path $imagePath).Path
    # Transparent pixels still carry hidden colors that the model would see. Unless -Transparent
    # asks to keep the transparency, paint those areas white in a temporary copy.
    if (-not $Transparent) {
        $sourceImage = [System.Drawing.Image]::FromFile($resolvedImagePath)
        try {
            if ([System.Drawing.Image]::IsAlphaPixelFormat($sourceImage.PixelFormat)) {
                $flattenedBitmap = New-Object System.Drawing.Bitmap($sourceImage.Width, $sourceImage.Height)
                $graphics = [System.Drawing.Graphics]::FromImage($flattenedBitmap)
                $graphics.Clear([System.Drawing.Color]::White)
                $graphics.DrawImage($sourceImage, 0, 0, $sourceImage.Width, $sourceImage.Height)
                $graphics.Dispose()
                $flattenedPath = Join-Path $env:TEMP ("qwen_flat_{0}_{1}.png" -f (Get-Date -Format 'HHmmssfff'), $referenceImagePaths.Count)
                $flattenedBitmap.Save($flattenedPath, [System.Drawing.Imaging.ImageFormat]::Png)
                $flattenedBitmap.Dispose()
                $flattenedTempFiles += $flattenedPath
                $resolvedImagePath = $flattenedPath
            }
        }
        finally {
            $sourceImage.Dispose()
        }
    }
    $referenceImagePaths += $resolvedImagePath
}

# When editing without an explicit size, keep the first image's aspect ratio at about 1 megapixel
if ($referenceImagePaths.Count -gt 0 -and -not $PSBoundParameters.ContainsKey('Width') -and -not $PSBoundParameters.ContainsKey('Height')) {
    Add-Type -AssemblyName System.Drawing
    $firstImage = [System.Drawing.Image]::FromFile($referenceImagePaths[0])
    $aspectRatio = $firstImage.Width / $firstImage.Height
    $firstImage.Dispose()
    $Width = [Math]::Sqrt(1024 * 1024 * $aspectRatio)
    $Height = $Width / $aspectRatio
}

# The model works on 32-pixel blocks, so round the size down to a multiple of 32
$Width = [Math]::Min(2048, [Math]::Max(256, [Math]::Floor($Width / 32) * 32))
$Height = [Math]::Min(2048, [Math]::Max(256, [Math]::Floor($Height / 32) * 32))

if ($Transparent) {
    $Prompt = "This is an RGBA image with transparency. $($Prompt.Trim().TrimEnd('.')). The image has alpha channel and the background is transparent."
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
# With several images sd-cli fills in %d with the image index
$outputPattern = if ($Count -gt 1) { "qwen_${timestamp}_%d.png" } else { "qwen_${timestamp}.png" }
$outputPath = Join-Path $OutputDir $outputPattern

# Pass prompts through UTF-8 files: native command-line arguments would mangle
# non-English text and embedded quotes
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$promptFile = Join-Path $env:TEMP "qwen_prompt_$timestamp.txt"
$negativePromptFile = Join-Path $env:TEMP "qwen_negative_$timestamp.txt"
[System.IO.File]::WriteAllText($promptFile, $Prompt, $utf8NoBom)
[System.IO.File]::WriteAllText($negativePromptFile, $NegativePrompt, $utf8NoBom)

# Start-Process joins arguments with spaces, so paths are quoted explicitly
$cliArguments = @(
    '--diffusion-model', "`"$diffusionModelPath`"",
    '--llm', "`"$textEncoderPath`"",
    '--vae', "`"$vaePath`"",
    '--offload-to-cpu',
    '--diffusion-fa',
    '--vae-tiling',
    '--sampling-method', 'euler',
    '--cfg-scale', $CfgScale.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    '--steps', $Steps,
    '-W', $Width,
    '-H', $Height,
    '-s', $Seed,
    '-b', $Count,
    '--prompt-file', "`"$promptFile`"",
    '--negative-prompt-file', "`"$negativePromptFile`"",
    '-o', "`"$outputPath`""
)
# Other programs on this PC can hold several GB of VRAM, which breaks the engine's automatic
# placement. Give it an explicit budget of (free VRAM - 1 GB) so it streams weights instead of failing.
try {
    $gpuFreeMegabytes = [int](nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | Select-Object -First 1)
    $gpuBudgetGib = [Math]::Round([Math]::Max(1.5, ($gpuFreeMegabytes - 1024) / 1024), 1)
    $cliArguments += @('--max-vram', $gpuBudgetGib.ToString([System.Globalization.CultureInfo]::InvariantCulture))
    # Default VAE tiles need ~3.4 GB; smaller tiles fit a tight budget
    if ($gpuBudgetGib -lt 3.5) { $cliArguments += @('--vae-tile-size', '16x16') }
    if ($gpuBudgetGib -lt 5) { Write-Host "GPU is busy with other programs ($gpuFreeMegabytes MB free) - running in economy mode, slower." }
}
catch {
    Write-Host 'Could not read GPU memory (nvidia-smi); using automatic placement.'
}

# Edit mode: vision weights plus each image as a reference (-r), in the given order
if ($referenceImagePaths.Count -gt 0) {
    $cliArguments += @('--llm_vision', "`"$visionEncoderPath`"")
    foreach ($referenceImagePath in $referenceImagePaths) {
        $cliArguments += @('-r', "`"$referenceImagePath`"")
    }
}

$modeDescription = if ($referenceImagePaths.Count -gt 0) { "Editing $($referenceImagePaths.Count) image(s) ->" } else { 'Generating' }
Write-Host "$modeDescription $Count image(s) at ${Width}x${Height}, $Steps steps..."
Write-Host 'Loading takes ~1 min, then ~3 min per 1024x1024 image. Everything is released when it finishes.'
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$engineProcess = $null
try {
    $engineProcess = Start-Process -FilePath $cliExe -ArgumentList $cliArguments -NoNewWindow -PassThru
    # Caching the handle is required for ExitCode to be readable after the process ends
    $null = $engineProcess.Handle
    # Lower CPU priority so the desktop and other apps stay responsive during generation
    try { $engineProcess.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::BelowNormal } catch { }
    $engineProcess.WaitForExit()
    $engineExitCode = $engineProcess.ExitCode
}
finally {
    # If the user presses Ctrl+C, make sure the engine does not keep running in the background
    if ($engineProcess -and -not $engineProcess.HasExited) {
        Stop-Process -Id $engineProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $promptFile, $negativePromptFile -Force -ErrorAction SilentlyContinue
    foreach ($flattenedTempFile in $flattenedTempFiles) {
        Remove-Item -LiteralPath $flattenedTempFile -Force -ErrorAction SilentlyContinue
    }
}

$savedImages = @(Get-ChildItem -Path $OutputDir -Filter "qwen_${timestamp}*.png" -ErrorAction SilentlyContinue)
if ($engineExitCode -ne 0 -or $savedImages.Count -eq 0) {
    Write-Error "Generation failed (exit code $engineExitCode). See the messages above."
    exit 1
}

Write-Host ("Done in {0:N0}s. Saved:" -f $stopwatch.Elapsed.TotalSeconds)
$savedImages | ForEach-Object { Write-Host "  $($_.FullName)" }
if ($Open) {
    $savedImages | ForEach-Object { Invoke-Item $_.FullName }
}
