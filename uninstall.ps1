# Removes the XE Widgets server, its startup entry and the widgets. Python and its packages are left alone.
$serverHome = Join-Path $env:LOCALAPPDATA 'XEWidgets'
$widgetRoot = Join-Path $env:APPDATA 'Corsair\CUE5\html_widgets'

Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*NowPlayingServer.pyw*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'XE Widgets Server.lnk') -ErrorAction SilentlyContinue
Remove-Item $serverHome -Recurse -Force -ErrorAction SilentlyContinue
foreach ($d in @('com\xewidgets\chat', 'com\quadrakev\xevisualizer')) {
    Remove-Item (Join-Path $widgetRoot $d) -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host 'XE Widgets removed. Restart iCUE to finish.' -ForegroundColor Green
