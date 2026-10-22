# Builds the files to attach to a GitHub release:
#   dist\XE-Widgets-Server.exe     companion server, installs itself when run (no Python needed)
#   dist\XE-Chat.icuewidget        double-click to add to iCUE
#   dist\XE-Visualizer.icuewidget
# Needs Python 3.10+ (used only to build the exe).
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $here 'dist'
$work = Join-Path $env:TEMP 'xe-widgets-build'
Remove-Item $dist, $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $dist | Out-Null

[IO.Compression.ZipFile]::CreateFromDirectory((Join-Path $here 'widgets\xe-chat'), (Join-Path $dist 'XE-Chat.icuewidget'))
[IO.Compression.ZipFile]::CreateFromDirectory((Join-Path $here 'widgets\xe-visualizer'), (Join-Path $dist 'XE-Visualizer.icuewidget'))

py -m pip install --quiet --upgrade pyinstaller -r (Join-Path $here 'server\requirements.txt')
py -m PyInstaller --noconfirm --onefile --windowed --name XE-Widgets-Server `
    --icon (Join-Path $here 'server\icon.ico') `
    --hidden-import pystray._win32 --collect-submodules winrt --collect-submodules pyaudiowpatch `
    --distpath $dist --workpath $work --specpath $work `
    (Join-Path $here 'server\NowPlayingServer.pyw')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $dist | ForEach-Object { '{0,-28} {1,8:N1} MB   SHA256 {2}' -f $_.Name, ($_.Length / 1MB), (Get-FileHash $_.FullName).Hash }
