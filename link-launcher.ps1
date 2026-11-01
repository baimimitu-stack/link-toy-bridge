# LINK launcher: start / stop everything in the background, no console windows.
#   powershell -File link-launcher.ps1 start   (stays resident as a tray icon)
#   powershell -File link-launcher.ps1 stop
# Closing the handheld window does NOT stop anything: the tray icon stays.
#   tray double-click / "open"  -> bring the handheld window back
#   tray "stop all and quit"    -> stop everything (same as Stop LINK.bat)
# Keep this file ASCII-only (Windows PowerShell 5.1 reads it with the system code page);
# Chinese UI text is written as \uXXXX escapes and converted with [regex]::Unescape.
param([Parameter(Mandatory = $true)][ValidateSet('start', 'stop')][string]$Action)

$ErrorActionPreference = 'Continue'
$Proj = Split-Path -Parent $MyInvocation.MyCommand.Path
# Paths: read from .env (LINK_PY / LINK_INTIFACE), else look on PATH / the default install location
function Env-Val($name) {
    $line = Get-Content (Join-Path $Proj '.env') -ErrorAction SilentlyContinue | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if ($line) { return ($line -split '=', 2)[1].Trim() }
    return ''
}
$Py = Env-Val 'LINK_PY'
if (-not $Py) { $Py = (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source }
if (-not $Py) { throw 'python.exe not found: install Python 3.12+ or set LINK_PY in .env' }
$Intiface = Env-Val 'LINK_INTIFACE'
if (-not $Intiface) { $Intiface = Join-Path $env:APPDATA 'IntifaceCentral\intiface_central.exe' }
$Edge = @('C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', 'C:\Program Files\Microsoft\Edge\Application\msedge.exe') | Where-Object { Test-Path $_ } | Select-Object -First 1
$Logs = Join-Path $Proj 'logs'
New-Item -ItemType Directory -Force $Logs | Out-Null
$Log = Join-Path $Logs 'launcher.log'
$QuitFlag = Join-Path $Logs 'quit.flag'

function Note($m) { Add-Content $Log ("{0} {1}" -f (Get-Date -Format 'MM-dd HH:mm:ss'), $m) -Encoding utf8 }

# python.exe / powershell.exe / msedge.exe processes whose command line matches the pattern (never this process)
function Find-Procs($name, $pattern) {
    Get-CimInstance Win32_Process -Filter "Name='$name'" |
        Where-Object { $_.CommandLine -match $pattern -and $_.ProcessId -ne $PID }
}

function Is-Up($name, $pattern) { [bool](Find-Procs $name $pattern) }

function Start-Hidden($file, $argList, $tag) {
    Start-Process $file -ArgumentList $argList -WorkingDirectory $Proj -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Logs "$tag.out.log") `
        -RedirectStandardError (Join-Path $Logs "$tag.err.log") | Out-Null
    Note "started $tag"
}

function Start-Services {
    $env:NO_PROXY = '*'; $env:no_proxy = '*'
    if (-not (Get-Process intiface_central -ErrorAction SilentlyContinue) -and (Test-Path $Intiface)) {
        Start-Process $Intiface | Out-Null
        Note 'started intiface_central (you still need to Scan for the toy)'
        Start-Sleep 3
    }
    if (-not (Is-Up 'python.exe' 'bridge\.py')) { Start-Hidden $Py 'bridge.py' 'bridge'; Start-Sleep 3 }
    if (-not (Is-Up 'python.exe' 'mediator\.py')) { Start-Hidden $Py 'mediator.py' 'mediator'; Start-Sleep 2 }
    $envText = Get-Content (Join-Path $Proj '.env') -Raw -ErrorAction SilentlyContinue
    if ($envText -match '(?m)^LINK_RELAY_TOKEN=\S' -and -not (Is-Up 'python.exe' 'linkrelay\.py')) {
        Start-Hidden $Py 'linkrelay.py' 'linkrelay'
    }
    if (-not (Is-Up 'powershell.exe' 'keepawake\.ps1')) {
        Start-Hidden 'powershell.exe' ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f (Join-Path $Proj 'keepawake.ps1')) 'keepawake'
    }
    for ($i = 0; $i -lt 15; $i++) {
        try { Invoke-WebRequest 'http://127.0.0.1:8800/health' -UseBasicParsing -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep 1 }
    }
}

# Show the handheld window: bring the existing one forward, or open a new app window
function Open-Handheld {
    if (-not ('LinkWin32' -as [type])) {
        Add-Type -Name 'Win32' -Namespace 'LinkWin' -ErrorAction SilentlyContinue -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
'@
    }
    $w = Find-Procs 'msedge.exe' 'app-profile' | ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue } |
        Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if ($w) {
        [LinkWin.Win32]::ShowWindow($w.MainWindowHandle, 9) | Out-Null   # 9 = restore (also un-minimize)
        [LinkWin.Win32]::SetForegroundWindow($w.MainWindowHandle) | Out-Null
        return
    }
    if ($Edge) {
        $Prof = Join-Path $Proj 'app-profile'
        Start-Process $Edge -ArgumentList @('--app=http://127.0.0.1:8800/', "--user-data-dir=`"$Prof`"", '--window-size=440,840', '--no-first-run', '--no-default-browser-check') | Out-Null
        Note 'opened handheld window'
    }
    else {
        Start-Process 'http://127.0.0.1:8800/'
        Note 'opened panel in default browser (Edge not found)'
    }
}

function Stop-All {
    foreach ($spec in @(@('msedge.exe', 'app-profile'), @('python.exe', 'linkrelay\.py'), @('python.exe', 'roomclient\.py'), @('python.exe', 'mediator\.py'), @('python.exe', 'bridge\.py'), @('powershell.exe', 'keepawake\.ps1'))) {
        # the Windows python stub and its child both match, so both get killed
        Find-Procs $spec[0] $spec[1] | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    Get-Process intiface_central -ErrorAction SilentlyContinue | ForEach-Object { [void]$_.CloseMainWindow() }
    Note 'stopped all'
}

if ($Action -eq 'stop') {
    Stop-All
    # tell the resident tray process (if any) to quit too, so its icon disappears
    New-Item -ItemType File -Force $QuitFlag | Out-Null
    return
}

# ---- start ----
Remove-Item $QuitFlag -Force -ErrorAction SilentlyContinue
$resident = Find-Procs 'powershell.exe' 'link-launcher\.ps1"?\s+start'
Start-Services
Open-Handheld
if ($resident) { Note 'already resident; just brought the window forward'; return }

# ---- stay resident as a tray icon ----
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$txtOpen = [regex]::Unescape('\u6253\u5f00\u638c\u673a')                       # open handheld
$txtQuit = [regex]::Unescape('\u5168\u90e8\u5173\u95ed\u5e76\u9000\u51fa')       # stop all and quit
$txtTip = 'LINK ' + [regex]::Unescape('\u638c\u673a')

$bmp = New-Object System.Drawing.Bitmap 16, 16
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.Clear([System.Drawing.Color]::FromArgb(220, 187, 28))
$g.FillRectangle((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(36, 29, 6))), 2, 2, 5, 5)
$g.FillRectangle((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(36, 29, 6))), 9, 9, 5, 5)
$g.Dispose()
$icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())

$ni = New-Object System.Windows.Forms.NotifyIcon
$ni.Icon = $icon
$ni.Text = $txtTip
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$miOpen = $menu.Items.Add($txtOpen)
$miQuit = $menu.Items.Add($txtQuit)
$ni.ContextMenuStrip = $menu
$ni.Visible = $true

$ctx = New-Object System.Windows.Forms.ApplicationContext
$quit = {
    $timer.Stop()
    $ni.Visible = $false
    $ni.Dispose()
    $ctx.ExitThread()
}
$ni.add_DoubleClick({ Open-Handheld })
$ni.add_MouseClick({ param($s, $e) if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) { Open-Handheld } })
$miOpen.add_Click({ Open-Handheld })
$miQuit.add_Click({ Stop-All; & $quit })

# Stop LINK.bat drops a flag file; notice it and leave
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1500
$timer.add_Tick({
    if (Test-Path $QuitFlag) {
        Remove-Item $QuitFlag -Force -ErrorAction SilentlyContinue
        & $quit
    }
})
$timer.Start()
Note 'tray icon resident'
[System.Windows.Forms.Application]::Run($ctx)
Note 'tray process exited'
