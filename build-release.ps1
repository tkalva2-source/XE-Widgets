# Builds the files to attach to a GitHub release:
#   dist\XE-Widgets.zip          everything a user needs (run install.bat inside)
#   dist\XE-Chat.icuewidget      widget only, for manual import in iCUE
#   dist\XE-Visualizer.icuewidget
#   dist\XE-Widgets-Setup.bat    one-click installer (downloads the zip above)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $here 'dist'
Remove-Item $dist -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $dist | Out-Null

function Zip($from, $to) { [IO.Compression.ZipFile]::CreateFromDirectory($from, $to) }
Zip (Join-Path $here 'widgets\xe-chat') (Join-Path $dist 'XE-Chat.icuewidget')
Zip (Join-Path $here 'widgets\xe-visualizer') (Join-Path $dist 'XE-Visualizer.icuewidget')

$stage = Join-Path $env:TEMP ('xe-widgets-' + [guid]::NewGuid())
$pkg = Join-Path $stage 'XE-Widgets'
New-Item -ItemType Directory $pkg | Out-Null
foreach ($item in 'widgets', 'server', 'install.bat', 'install.ps1', 'uninstall.bat', 'uninstall.ps1', 'README.md', 'LICENSE') {
    Copy-Item (Join-Path $here $item) $pkg -Recurse
}
Zip $stage (Join-Path $dist 'XE-Widgets.zip')
Remove-Item $stage -Recurse -Force
Copy-Item (Join-Path $here 'XE-Widgets-Setup.bat') $dist
Get-ChildItem $dist | Format-Table Name, Length
