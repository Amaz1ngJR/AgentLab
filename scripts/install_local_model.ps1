# 一键安装脚本 (Windows PowerShell):
#   - 检测 Ollama 是否安装,缺失时打印安装链接并退出
#   - 检查 Ollama 服务是否运行
#   - 拉取指定模型(默认 qwen2.5-coder:7b-instruct)
#   - 用一次简单推理验证模型可用
#   - 提示用户改 .env 切换到 local_qwen profile
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\install_local_model.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_local_model.ps1 -Model qwen2.5-coder:14b-instruct
#
# 注意:5060Ti 主机推荐用 14b-instruct;Mac M3 16GB 用 7b-instruct

param(
    [string]$Model = "qwen2.5-coder:7b-instruct",
    [string]$Endpoint = "http://localhost:11434"
)

$ErrorActionPreference = "Stop"

function Write-Step($msg)  { Write-Host "▸ $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "  √ $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "  ! $msg" -ForegroundColor Yellow }
function Write-Err($msg)   { Write-Host "  X $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "===================================================="
Write-Host "  AgentLab 本地模型一键安装"
Write-Host "  模型: $Model"
Write-Host "  端点: $Endpoint"
Write-Host "===================================================="
Write-Host ""

# ── 1. 检查 Ollama 是否安装 ────────────────────────────────────────────────
Write-Step "[1/4] 检查 Ollama 是否安装"
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    Write-Err "未检测到 ollama 命令"
    Write-Host ""
    Write-Host "请先下载安装 Ollama for Windows:"
    Write-Host ""
    Write-Host "  https://ollama.com/download/windows"
    Write-Host ""
    Write-Host "安装完成后重新运行本脚本。"
    exit 1
}
$version = (& ollama --version 2>&1 | Select-Object -First 1)
Write-Ok "找到 $version"

# ── 2. 检查 Ollama 服务 ─────────────────────────────────────────────────────
Write-Step "[2/4] 检查 Ollama 服务"
function Test-OllamaRunning {
    try {
        $null = Invoke-WebRequest -Uri "$Endpoint/api/tags" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

if (Test-OllamaRunning) {
    Write-Ok "服务已运行 ($Endpoint)"
} else {
    Write-Warn "服务未响应,尝试后台启动..."
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden -PassThru | Out-Null
    # 最多等 10 秒
    $started = $false
    for ($i = 1; $i -le 10; $i++) {
        Start-Sleep -Seconds 1
        if (Test-OllamaRunning) {
            $started = $true
            break
        }
    }
    if (-not $started) {
        Write-Err "无法启动 Ollama 服务"
        Write-Host ""
        Write-Host "请手动运行: ollama serve"
        Write-Host "再重试本脚本。"
        exit 2
    }
    Write-Ok "服务已启动"
}

# ── 3. 拉取模型 ──────────────────────────────────────────────────────────────
Write-Step "[3/4] 拉取模型 $Model"
$existing = (& ollama list 2>$null) | Select-String -Pattern "^$([regex]::Escape($Model))\s"
if ($existing) {
    Write-Ok "模型已存在,跳过下载"
} else {
    Write-Host "  首次下载约需 5-10 分钟,模型大小约 4.7 GB" -ForegroundColor DarkGray
    & ollama pull $Model
    if ($LASTEXITCODE -ne 0) {
        Write-Err "模型拉取失败"
        Write-Host "网络或磁盘空间问题?查看 https://ollama.com/library 确认模型名"
        exit 3
    }
    Write-Ok "模型拉取完成"
}

# ── 4. 验证模型可推理 ────────────────────────────────────────────────────────
Write-Step "[4/4] 验证模型可推理"
$response = "Reply with only the two characters: OK" | & ollama run $Model 2>&1 | Out-String
$response = $response.Trim()
if ([string]::IsNullOrWhiteSpace($response)) {
    Write-Err "模型未返回任何输出"
    exit 4
}
$preview = $response.Substring(0, [Math]::Min(80, $response.Length))
Write-Ok "模型响应: $preview"

# ── 完成 ─────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "✅ 安装完成" -ForegroundColor Green
Write-Host ""
Write-Host "下一步:切换 AgentLab 到本地 profile"
Write-Host ""
Write-Host "  Set-Content .env 'ACTIVE_PROFILE=local_qwen'" -ForegroundColor Cyan
Write-Host "  python -m app" -ForegroundColor Cyan
Write-Host ""
Write-Host "或单次测试:"
Write-Host ""
Write-Host "  python -m app --profile local_qwen -p '用一句话介绍你自己'" -ForegroundColor Cyan
Write-Host ""
Write-Host "更多模型与故障排查见 docs/local_model_guide.md"
Write-Host ""
