<#
.SYNOPSIS
    Opens the Qwen-Image-2.1 browser interface (create + edit images).

.DESCRIPTION
    Starts a small local web server (Python standard library, ~20 MB RAM when idle)
    and opens http://127.0.0.1:<Port>/ in the browser.
    The model itself is loaded only while an image is being created and is unloaded
    as soon as the job ends. Close this window, or use the power button in the UI,
    to stop the interface; any running job is stopped with it.

.EXAMPLE
    C:\projects\Qwen-Image-2.1-GGUF\src\start-webui.ps1
    C:\projects\Qwen-Image-2.1-GGUF\src\start-webui.ps1 -Port 8080 -NoBrowser
#>
param(
    [int]$Port = 7860,
    [switch]$NoBrowser
)

$serverScript = Join-Path $PSScriptRoot 'webui\server.py'

# Prefer python.exe on PATH, fall back to the py launcher
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$pythonArguments = @()
if (-not $pythonCommand) {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
    $pythonArguments = @('-3')
}
if (-not $pythonCommand) {
    Write-Error 'Python 3 was not found. Install it from https://www.python.org/ and try again.'
    exit 1
}

$pythonArguments += @($serverScript, '--port', $Port)
if ($NoBrowser) { $pythonArguments += '--no-browser' }

& $pythonCommand.Source @pythonArguments
