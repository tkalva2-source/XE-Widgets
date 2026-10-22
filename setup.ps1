# One-step installer: downloads the latest XE Widgets release and runs its installer.
#   irm https://raw.githubusercontent.com/tkalva2-source/XE-Widgets/main/setup.ps1 | iex
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$zipUrl = 'https://github.com/tkalva2-source/XE-Widgets/releases/latest/download/XE-Widgets.zip'
$work = Join-Path $env:TEMP ('xe-widgets-setup-' + [guid]::NewGuid())
New-Item -ItemType Directory $work | Out-Null
try {
    Write-Host 'Downloading XE Widgets...' -ForegroundColor Cyan
    $zip = Join-Path $work 'XE-Widgets.zip'
    Invoke-WebRequest $zipUrl -OutFile $zip -UseBasicParsing
    Expand-Archive $zip $work -Force
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $work 'XE-Widgets\install.ps1')
} finally {
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
