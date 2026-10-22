# XE Widgets installer: sets up the companion server and copies the widgets into iCUE.
# Run via install.bat (double-click). Safe to run again to update.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$serverHome = Join-Path $env:LOCALAPPDATA 'XEWidgets\server'
$widgetRoot = Join-Path $env:APPDATA 'Corsair\CUE5\html_widgets'
$startup = [Environment]::GetFolderPath('Startup')

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }

# 1. Python 3.10+
Step 'Checking Python'
$py = $null
foreach ($c in @('py', 'python')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
        $exe = & $c -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')" 2>$null
        if ($exe -and (Test-Path $exe)) { $py = $exe.Trim(); break }
    } catch {}
}
if (-not $py) {
    Write-Host 'Python 3.10 or newer was not found.' -ForegroundColor Yellow
    $ans = Read-Host 'Install Python 3.12 now with winget? (Y/N)'
    if ($ans -notmatch '^[Yy]') { Write-Host 'Install Python from https://www.python.org (tick "Add Python to PATH"), then run this again.'; exit 1 }
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    Write-Host 'Python installed. Close this window and run install.bat again.' -ForegroundColor Green
    exit 0
}
Write-Host "Using $py"
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }

# 2. Server dependencies
Step 'Installing server dependencies (first run can take a minute)'
& $py -m pip install --quiet --upgrade -r (Join-Path $here 'server\requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip failed to install the dependencies.' }

# 3. Server files (stop a running copy first so the file isn't in use)
Step 'Installing the server'
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*NowPlayingServer.pyw*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force $serverHome | Out-Null
Copy-Item (Join-Path $here 'server\NowPlayingServer.pyw') $serverHome -Force
$server = Join-Path $serverHome 'NowPlayingServer.pyw'

# 4. Start with Windows
Step 'Starting the server with Windows'
$sh = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $startup 'XE Widgets Server.lnk'))
$sh.TargetPath = $pyw
$sh.Arguments = '"' + $server + '"'
$sh.WorkingDirectory = $serverHome
$sh.Description = 'XE Widgets companion server (now playing, Twitch chat relay, stream preview)'
$sh.Save()
Start-Process -FilePath $pyw -ArgumentList ('"' + $server + '"') -WorkingDirectory $serverHome

# 5. Widgets
Step 'Installing widgets into iCUE'
$widgets = @(
    @{ src = 'widgets\xe-chat'; dst = 'com\xewidgets\chat' },
    @{ src = 'widgets\xe-visualizer'; dst = 'com\quadrakev\xevisualizer' }
)
foreach ($w in $widgets) {
    $dst = Join-Path $widgetRoot $w.dst
    New-Item -ItemType Directory -Force $dst | Out-Null
    Copy-Item (Join-Path (Join-Path $here $w.src) '*') $dst -Recurse -Force
    Write-Host "  $($w.dst)"
}

Write-Host "`nDone. Restart iCUE, then add 'XE Chat' and 'XE Visualizer' to your Xeneon Edge." -ForegroundColor Green
Write-Host 'The server runs in the system tray and starts with Windows.'
