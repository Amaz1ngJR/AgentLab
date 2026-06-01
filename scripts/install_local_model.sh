#!/usr/bin/env bash
# 一键安装脚本(macOS / Linux):
#   - 检测 Ollama 是否安装,缺失时打印安装命令并退出
#   - 启动 Ollama 服务(如果还没在运行)
#   - 拉取指定模型(默认 qwen2.5-coder:7b-instruct)
#   - 用一次简单推理验证模型可用
#   - 提示用户改 .env 切换到 local_qwen profile
#
# 用法:
#   bash scripts/install_local_model.sh
#   bash scripts/install_local_model.sh qwen2.5-coder:14b-instruct   # 自定义模型
#
# 退出码:
#   0  全部成功
#   1  Ollama 未安装
#   2  Ollama 服务起不来
#   3  模型拉取失败
#   4  模型推理验证失败

set -e

MODEL="${1:-qwen2.5-coder:7b-instruct}"
ENDPOINT="${OLLAMA_ENDPOINT:-http://localhost:11434}"

# ── 颜色辅助(如果终端不支持就退化为空) ──────────────────────────────────────
if [ -t 1 ]; then
    BOLD="\033[1m"; DIM="\033[2;90m"; GREEN="\033[32m"; YELLOW="\033[33m"
    RED="\033[31m"; CYAN="\033[36m"; RESET="\033[0m"
else
    BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi

step()  { printf "${CYAN}▸${RESET} ${BOLD}%s${RESET}\n" "$1"; }
ok()    { printf "  ${GREEN}✓${RESET} %s\n" "$1"; }
warn()  { printf "  ${YELLOW}⚠${RESET} %s\n" "$1"; }
err()   { printf "  ${RED}✗${RESET} %s\n" "$1" >&2; }

echo
echo "===================================================="
echo "  AgentLab 本地模型一键安装"
echo "  模型: $MODEL"
echo "  端点: $ENDPOINT"
echo "===================================================="
echo

# ── 1. 检查 Ollama 是否已安装 ────────────────────────────────────────────────
step "[1/4] 检查 Ollama 是否安装"
if ! command -v ollama >/dev/null 2>&1; then
    err "未检测到 ollama 命令"
    echo
    echo "请先安装 Ollama:"
    echo
    case "$(uname -s)" in
        Darwin)
            echo "  brew install ollama"
            echo "  # 或下载 .dmg: https://ollama.com/download/mac"
            ;;
        Linux)
            echo "  curl -fsSL https://ollama.com/install.sh | sh"
            ;;
        *)
            echo "  https://ollama.com/download"
            ;;
    esac
    echo
    echo "安装完成后重新运行本脚本。"
    exit 1
fi
ok "找到 $(ollama --version 2>&1 | head -1)"

# ── 2. 检查 / 启动 Ollama 服务 ────────────────────────────────────────────────
step "[2/4] 检查 Ollama 服务"
if curl -sf -o /dev/null --max-time 3 "${ENDPOINT}/api/tags"; then
    ok "服务已运行 ($ENDPOINT)"
else
    warn "服务未响应,尝试启动..."
    ollama serve >/tmp/ollama-agentlab.log 2>&1 &
    SERVE_PID=$!
    # 最多等 10 秒
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if curl -sf -o /dev/null --max-time 1 "${ENDPOINT}/api/tags"; then
            ok "服务已启动 (PID=$SERVE_PID)"
            break
        fi
        sleep 1
    done
    if ! curl -sf -o /dev/null --max-time 3 "${ENDPOINT}/api/tags"; then
        err "无法启动 Ollama 服务"
        echo
        echo "请手动运行:"
        echo "  ollama serve"
        echo "再重试本脚本。日志: /tmp/ollama-agentlab.log"
        exit 2
    fi
fi

# ── 3. 拉取模型 ──────────────────────────────────────────────────────────────
step "[3/4] 拉取模型 $MODEL"
if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$MODEL"; then
    ok "模型已存在,跳过下载"
else
    echo "  ${DIM}首次下载约需 5-10 分钟,模型大小约 4.7 GB${RESET}"
    if ! ollama pull "$MODEL"; then
        err "模型拉取失败"
        echo "网络或磁盘空间问题?查看 https://ollama.com/library 确认模型名"
        exit 3
    fi
    ok "模型拉取完成"
fi

# ── 4. 跑一次最小推理验证 ────────────────────────────────────────────────────
step "[4/4] 验证模型可推理"
RESPONSE=$(echo "Reply with only the two characters: OK" \
    | ollama run "$MODEL" 2>&1 \
    | tr -d '\r' \
    | head -3 \
    | tr '\n' ' ')
if [ -z "$RESPONSE" ]; then
    err "模型未返回任何输出"
    exit 4
fi
ok "模型响应: ${RESPONSE:0:80}"

# ── 完成 ─────────────────────────────────────────────────────────────────────
echo
printf "${GREEN}${BOLD}✅ 安装完成${RESET}\n"
echo
echo "下一步:切换 AgentLab 到本地 profile"
echo
echo "  ${CYAN}echo 'ACTIVE_PROFILE=local_qwen' > .env${RESET}"
echo "  ${CYAN}python -m app${RESET}"
echo
echo "或单次测试:"
echo
echo "  ${CYAN}python -m app --profile local_qwen -p '用一句话介绍你自己'${RESET}"
echo
echo "更多模型与故障排查见 docs/local_model_guide.md"
echo
