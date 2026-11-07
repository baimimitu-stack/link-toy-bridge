# LINK 客户电脑安装脚本：检查 Python -> 建独立环境装依赖 -> 生成 .env -> 建桌面快捷方式
# 用法：双击 "Install LINK.bat"，按提示填「服务器地址」和「暗号」（卖家会发给你）。
# 本文件带 BOM 的 UTF-8，Windows PowerShell 5.1 才能正确显示中文，别用记事本另存。
param(
    [string]$Servers = '',
    [string]$RelayToken = '',
    [switch]$SkipShortcuts
)

$ErrorActionPreference = 'Stop'
$Proj = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Proj

function Say($m) { Write-Host "[LINK] $m" }
function Fail($m) { Write-Host "[LINK][失败] $m" -ForegroundColor Red; exit 1 }

# ---- 1. Python 3.12+ ----
$py = (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $py) { Fail '没找到 Python。请先到 python.org 安装 Python 3.12 或更新版本，安装时勾选 "Add python.exe to PATH"，然后重新双击安装。' }
$okVer = & $py -c "import sys; print(1 if sys.version_info >= (3, 12) else 0)" 2>$null
if (-not $okVer) { Fail "找到的 python.exe 不能用（可能是微软商店的占位程序）。请到 python.org 安装 Python 3.12 或更新版本。" }
if ($okVer -ne '1') { Fail "Python 版本太旧（$py）。需要 3.12 或更新版本。" }
Say "Python 可用：$py"

# ---- 2. 独立环境 + 依赖 ----
$venvPy = Join-Path $Proj '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Say '正在创建独立环境……'
    & $py -m venv (Join-Path $Proj '.venv')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Fail '创建环境失败。' }
}
Say '正在安装依赖（要联网，可能要一两分钟）……'
& $venvPy -m pip install -q -r (Join-Path $Proj 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail '依赖安装失败。检查网络后重新双击安装即可，不会重复破坏已有设置。' }
Say '依赖安装完成。'

# ---- 3. .env ----
$envPath = Join-Path $Proj '.env'
if (Test-Path $envPath) {
    Say '已有 .env，保持不动（里面是你的密钥）。如需重来，先把 .env 改名再重新安装。'
}
else {
    if (-not $Servers) { $Servers = (Read-Host '请粘贴「服务器地址」（卖家给的，形如 wss://xxx/link）').Trim() }
    if (-not $RelayToken) { $RelayToken = (Read-Host '请粘贴「暗号」（卖家给的那一长串）').Trim() }
    if (-not $Servers -or -not $RelayToken) { Fail '服务器地址和暗号都不能为空。' }

    # 本机桥的钥匙：在这台电脑上随机生成，不用问卖家，也不会发给任何人
    $bytes = New-Object byte[] 24
    (New-Object System.Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
    $bridgeToken = ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''

    $lines = @(
        '# 由 install.ps1 生成。里面有密钥，不要发给任何人，也不要提交到 git。',
        'INTIFACE_URL=ws://127.0.0.1:12345',
        'BRIDGE_HOST=127.0.0.1',
        'BRIDGE_PORT=8791',
        "BRIDGE_TOKEN=$bridgeToken",
        'MAX_STRENGTH=100',
        'MEDIATOR_PORT=8800',
        'MEDIATOR_TOKEN=',
        'BRIDGE_MCP=http://127.0.0.1:8791/mcp',
        "LINK_SERVERS=$Servers",
        "LINK_RELAY_TOKEN=$RelayToken",
        "LINK_PY=$venvPy"
    )
    # 必须用不带 BOM 的 UTF-8 写，否则第一个变量名会被污染
    [System.IO.File]::WriteAllText($envPath, (($lines -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
    Say '.env 已生成。'
}

# ---- 4. Intiface Central ----
$intiface = Join-Path $env:APPDATA 'IntifaceCentral\intiface_central.exe'
if (-not (Test-Path $intiface)) {
    Say '提示：没找到 Intiface Central。请到 intiface.com 下载安装；安装位置不是默认的话，在 .env 里加一行 LINK_INTIFACE=它的完整路径。'
}

# ---- 5. 桌面快捷方式 ----
if (-not $SkipShortcuts) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $sh = New-Object -ComObject WScript.Shell
    foreach ($pair in @(@('Start LINK', 'Start LINK.bat'), @('Stop LINK', 'Stop LINK.bat'))) {
        $lnk = $sh.CreateShortcut((Join-Path $desktop ($pair[0] + '.lnk')))
        $lnk.TargetPath = Join-Path $Proj $pair[1]
        $lnk.WorkingDirectory = $Proj
        $lnk.Save()
    }
    Say '桌面上已建好 Start LINK / Stop LINK 两个快捷方式。'
}

Write-Host ''
Say '安装完成。使用方法：'
Write-Host '  1. 打开 Intiface Central，点 Start Server，再点 Start Scanning，让它搜到你的设备。'
Write-Host '  2. 双击桌面的 Start LINK。'
Write-Host '  3. 不用时双击 Stop LINK。电脑关机或断网，AI 就连不上。'
