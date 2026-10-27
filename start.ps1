# 一键启动脚本（后端 + 前端）
#
# 用法：
#   1. 打开一个 PowerShell 窗口
#   2. cd 到项目根目录
#   3. .\start.ps1
#
# 然后浏览器打开 http://127.0.0.1:5173
#
# 停止：在本窗口按 Ctrl+C，或在任务管理器结束 python / node 进程

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot

Write-Host ''
Write-Host '========================================' -ForegroundColor Green
Write-Host '  经营诊断 · 本地启动' -ForegroundColor Green
Write-Host '========================================' -ForegroundColor Green
Write-Host ''

# ---------- 定位 Python（当前 conda 环境 → PATH → 常见安装位置，逐级回退）----------
$PythonCandidates = @()
if ($env:CONDA_PREFIX) { $PythonCandidates += (Join-Path $env:CONDA_PREFIX 'python.exe') }
# 本机私有配置（start.local.ps1 已加入 .gitignore，不入库）：
#   $LocalPythonPath = 'D:\你的路径\envs\data_agent\python.exe'
$LocalConfig = Join-Path $Root 'start.local.ps1'
if (Test-Path $LocalConfig) { . $LocalConfig }
if ($LocalPythonPath) { $PythonCandidates += $LocalPythonPath }
$CmdPython = Get-Command python -ErrorAction SilentlyContinue
# 排除 Windows 商店的 python 占位 stub（存在但不可用）
if ($CmdPython -and $CmdPython.Source -notmatch 'WindowsApps') {
    $PythonCandidates += $CmdPython.Source
}
$PythonCandidates += @(
    "$env:USERPROFILE\.conda\envs\data_agent\python.exe",
    "$env:USERPROFILE\anaconda3\envs\data_agent\python.exe",
    "$env:USERPROFILE\miniconda3\envs\data_agent\python.exe"
)
$Python = $PythonCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $Python) {
    Write-Host '[错误] 找不到可用的 Python 解释器。' -ForegroundColor Red
    Write-Host '       方式一：激活已有 conda 环境（conda activate data_agent）后重试'
    Write-Host '       方式二：创建新环境：conda create -n data_agent python=3.12 -y'
    Write-Host '               然后安装依赖：pip install -r requirements.txt'
    Write-Host '       方式三：任意 Python 3.12+ 加入 PATH 后重试'
    exit 1
}
Write-Host "Python : $Python" -ForegroundColor Cyan

# ---------- 检查 .env ----------
if (-not (Test-Path (Join-Path $Root '.env'))) {
    Write-Host '[错误] 缺少 .env 文件。' -ForegroundColor Red
    Write-Host '       请执行：Copy-Item .env.template .env  然后填写配置'
    exit 1
}
$envContent = Get-Content (Join-Path $Root '.env') -Raw
if ($envContent -match '(?m)^APP_SECRET_KEY=\s*$') {
    Write-Host '[错误] .env 中 APP_SECRET_KEY 为空。' -ForegroundColor Red
    $key = & $Python -c "import secrets; print(secrets.token_hex(32))"
    Write-Host "       请写入：APP_SECRET_KEY=$key"
    exit 1
}

# ---------- 检查前端依赖 ----------
$Web = Join-Path $Root 'web'
if (-not (Test-Path (Join-Path $Web 'node_modules'))) {
    Write-Host '[提示] 前端依赖未安装，正在安装…' -ForegroundColor Yellow
    Push-Location $Web
    & npm install --no-audit --no-fund
    Pop-Location
}

# ---------- 端口占用检测（docs/16 P2-22）----------
# 原先不查端口：二次启动时 uvicorn 绑不上 8000 直接退出，健康检查
# 探测到的是**旧进程**（旧代码）—— 显示"启动成功"实则新代码没在跑。
function Get-PortOwner($port) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($c) { return ($c | Select-Object -First 1).OwningProcess }
    return $null
}

foreach ($pair in @(@('8000', '后端'), @('5173', '前端(Vite)'))) {
    $owner = Get-PortOwner $pair[0]
    if ($owner) {
        $procName = ''
        try { $procName = (Get-Process -Id $owner -ErrorAction Stop).ProcessName } catch {}
        Write-Host "[错误] 端口 $($pair[0])（$($pair[1])）已被 PID $owner ($procName) 占用。" -ForegroundColor Red
        Write-Host '       可能是上一次启动的服务还在运行 —— 先停止它再启动新代码：' -ForegroundColor Red
        Write-Host "       Stop-Process -Id $owner -Force" -ForegroundColor White
        Write-Host '       （确认不是别的应用在用这个端口再停止）'
        exit 1
    }
}

# ---------- 日志目录（docs/16 P2-22：后端输出不再随隐藏窗口丢失）----------
$LogDir = Join-Path $Root 'logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# ---------- 启动后端 ----------
Write-Host ''
Write-Host '启动后端 http://127.0.0.1:8000 …' -ForegroundColor Cyan
$BackendLog = Join-Path $LogDir 'backend.log'
$Backend = Start-Process -FilePath $Python `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000' `
    -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $BackendLog -RedirectStandardError (Join-Path $LogDir 'backend.err.log')

# 轮询等待就绪（固定 sleep 对冷启动不够：探测那一刻服务可能还没监听）
$h = $null
foreach ($i in 1..30) {
    Start-Sleep -Seconds 1
    try { $h = Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/health' -TimeoutSec 2; break } catch {}
}
if ($h) {
    # 公开 health 已脱敏（P2-13），只有 status/version
    Write-Host "  后端就绪 (status=$($h.status), version=$($h.version))" -ForegroundColor Green
} else {
    Write-Host '  后端启动失败，请检查 .env 与依赖；日志：' -ForegroundColor Red
    Write-Host "    $BackendLog" -ForegroundColor White
    Write-Host "    $(Join-Path $LogDir 'backend.err.log')" -ForegroundColor White
    Stop-Process -Id $Backend.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

# ---------- 启动前端 ----------
Write-Host '启动前端 http://127.0.0.1:5173 …' -ForegroundColor Cyan
$FrontendLog = Join-Path $LogDir 'frontend.log'
# ★ cmd /c npm 是包装进程：真正监听 5173 的是其子进程 node。
#   记录两个 PID，结束时都要停（docs/16 P2-22）。
$Frontend = Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/c', 'npm', 'run', 'dev' `
    -WorkingDirectory $Web -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $FrontendLog -RedirectStandardError (Join-Path $LogDir 'frontend.err.log')

$r = $null
foreach ($i in 1..40) {
    Start-Sleep -Seconds 1
    try { $r = Invoke-WebRequest 'http://127.0.0.1:5173/' -TimeoutSec 2 -UseBasicParsing; break } catch {}
}
if ($r) {
    Write-Host "  前端就绪 (HTTP $($r.StatusCode))" -ForegroundColor Green
} else {
    Write-Host '  前端启动失败；日志：' -ForegroundColor Red
    Write-Host "    $FrontendLog" -ForegroundColor White
}

$FrontendReal = Get-PortOwner 5173        # 真正监听的 node 进程

Write-Host ''
Write-Host '========================================' -ForegroundColor Green
Write-Host '  启动完成' -ForegroundColor Green
Write-Host '========================================' -ForegroundColor Green
Write-Host ''
Write-Host '  打开：http://127.0.0.1:5173' -ForegroundColor Yellow
Write-Host '  API 文档：http://127.0.0.1:8000/docs' -ForegroundColor Yellow
Write-Host "  日志：$LogDir\backend.log / frontend.log" -ForegroundColor Yellow
Write-Host ''
Write-Host '  后端 PID：' -NoNewline; Write-Host $Backend.Id -ForegroundColor Cyan
Write-Host '  前端 PID：' -NoNewline
Write-Host "$FrontendReal" -ForegroundColor Cyan
Write-Host "  （前端包装进程 PID：$($Frontend.Id)，停止时一并结束）"
Write-Host ''
Write-Host '  停止服务：' -NoNewline
$stopIds = @($Backend.Id, $Frontend.Id) + @($FrontendReal) | Where-Object { $_ } | Select-Object -Unique
Write-Host ("Stop-Process -Id " + ($stopIds -join ',') + " -Force") -ForegroundColor White
Write-Host ''

Start-Process 'http://127.0.0.1:5173'