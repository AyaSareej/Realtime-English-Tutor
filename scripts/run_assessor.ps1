$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
& (Join-Path $PSScriptRoot "configure_local_secrets.ps1") -ProjectRoot $ProjectRoot
& (Join-Path $ProjectRoot ".venv\Scripts\Activate.ps1")
python tools\assessment_preflight.py
python app\realtime\assessment_agent.py dev
