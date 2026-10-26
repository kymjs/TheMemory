# One-line installer (spec §16.1). Bootstraps uv, then installs this repo as a tool.
$ErrorActionPreference = "Stop"
Write-Host "TheMemory installer"
Write-Host "Note: embedding model is vendored (~95MB ONNX). With uv/Python/onnxruntime,"
Write-Host "total download/install size may be about 200-300MB. Memory stays on this machine only."
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    irm https://astral.sh/uv/install.ps1 | iex
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
$repo = Split-Path -Parent $PSScriptRoot
uv tool install --force --from $repo thememory
Write-Host "Installed. Next: thememory setup"
Write-Host "Extraction will send conversation text to the LLM endpoint you configure."
