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

# ---------- 启动后端 ----------
Write-Host ''
Write-Host '启动后端 http://127.0.0.1:8000 …' -ForegroundColor Cyan
$Backend = Start-Process -FilePath $Python `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000' `
    -WorkingDirectory $Root -PassThru -WindowStyle Hidden

Start-Sleep -Seconds 6

try {
    $h = Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/health' -TimeoutSec 10
    Write-Host "  后端就绪 (status=$($h.status), 表=$($h.database.tables))" -ForegroundColor Green
} catch {
    Write-Host '  后端启动失败，请检查 .env 与依赖' -ForegroundColor Red
    Stop-Process -Id $Backend.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

# ---------- 启动前端 ----------
Write-Host '启动前端 http://127.0.0.1:5173 …' -ForegroundColor Cyan
$Frontend = Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/c', 'npm', 'run', 'dev' `
    -WorkingDirectory $Web -PassThru -WindowStyle Hidden

Start-Sleep -Seconds 8

try {
    $r = Invoke-WebRequest 'http://127.0.0.1:5173/' -TimeoutSec 10 -UseBasicParsing
    Write-Host "  前端就绪 (HTTP $($r.StatusCode))" -ForegroundColor Green
} catch {
    Write-Host '  前端启动失败' -ForegroundColor Red
}

Write-Host ''
Write-Host '========================================' -ForegroundColor Green
Write-Host '  启动完成' -ForegroundColor Green
Write-Host '========================================' -ForegroundColor Green
Write-Host ''
Write-Host '  打开：http://127.0.0.1:5173' -ForegroundColor Yellow
Write-Host '  API 文档：http://127.0.0.1:8000/docs' -ForegroundColor Yellow
Write-Host ''
Write-Host '  后端 PID：' -NoNewline; Write-Host $Backend.Id -ForegroundColor Cyan
Write-Host '  前端 PID：' -NoNewline; Write-Host $Frontend.Id -ForegroundColor Cyan
Write-Host ''
Write-Host '  停止服务：' -NoNewline
Write-Host "Stop-Process -Id $($Backend.Id),$($Frontend.Id) -Force" -ForegroundColor White
Write-Host ''

Start-Process 'http://127.0.0.1:5173'