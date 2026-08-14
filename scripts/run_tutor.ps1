$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot
& .\.venv\Scripts\Activate.ps1
python .\tools\piper_preflight.py
python .\app\realtime\voice_agent.py dev
