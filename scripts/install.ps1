# One-line installer (spec §16.1). Bootstraps uv, then installs this repo as a tool.
$ErrorActionPreference = "Stop"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    irm https://astral.sh/uv/install.ps1 | iex
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
$repo = Split-Path -Parent $PSScriptRoot
uv tool install --force --from $repo thememory
Write-Host "Installed. Next: thememory setup"
