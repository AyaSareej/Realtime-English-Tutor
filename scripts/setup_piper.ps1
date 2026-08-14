param(
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$VoiceName = "en_US-lessac-medium"
$VoiceDirectory = Join-Path $ProjectRoot "data\piper"
$ModelPath = Join-Path $VoiceDirectory "$VoiceName.onnx"
$ConfigPath = "$ModelPath.json"

New-Item -ItemType Directory -Force -Path $VoiceDirectory | Out-Null

if ((Test-Path $ModelPath) -and (Test-Path $ConfigPath)) {
    Write-Host "Piper voice is already installed at $ModelPath"
    exit 0
}

Write-Host "Downloading the Piper $VoiceName voice for offline guided TTS..."
python -m piper.download_voices --data-dir $VoiceDirectory $VoiceName
if ($LASTEXITCODE -ne 0) {
    throw "Piper voice download failed. Check the internet connection and rerun this script."
}
if (-not (Test-Path $ModelPath)) {
    throw "Piper model was not created at $ModelPath"
}
if (-not (Test-Path $ConfigPath)) {
    throw "Piper config was not created at $ConfigPath"
}

Write-Host "Piper voice downloaded. Guided TTS can now run without an online TTS provider."
