#!/usr/bin/env bash
# 端到端验证本地模型链路:
#   1. profile 配置加载
#   2. Ollama 端点连通
#   3. 简单对话(无工具)
#   4. 工具调用闭环(读文件)
#   5. todo_write 任务面板
#
# 用法:
#   bash scripts/verify_local_model.sh
#   bash scripts/verify_local_model.sh local_qwen14b   # 验证其他 profile

set -e

PROFILE="${1:-local_qwen}"

if [ -t 1 ]; then
    BOLD="\033[1m"; GREEN="\033[32m"; RED="\033[31m"; CYAN="\033[36m"; RESET="\033[0m"
else
    BOLD=""; GREEN=""; RED=""; CYAN=""; RESET=""
fi

step() { printf "${CYAN}▸${RESET} ${BOLD}%s${RESET}\n" "$1"; }
ok()   { printf "  ${GREEN}✓${RESET} %s\n" "$1"; }
fail() { printf "  ${RED}✗${RESET} %s\n" "$1" >&2; exit 1; }

echo
echo "===================================================="
echo "  端到端验证 profile: $PROFILE"
echo "===================================================="
echo

# 必须在项目根目录运行
if [ ! -f "config/models.yaml" ]; then
    fail "请在 AgentLab 项目根目录运行此脚本"
fi

# ── 1. profile 配置可加载 ───────────────────────────────────────────────────
step "[1/5] 加载 profile 配置"
python -c "
from app.config.loader import load_config
cfg = load_config(profile_name='$PROFILE')
print(f'  provider: {cfg.provider}')
print(f'  model:    {cfg.model}')
print(f'  base_url: {cfg.base_url}')
" || fail "profile 配置加载失败"
ok "配置 OK"
echo

# ── 2. Ollama 端点存活 + 模型已下载 ────────────────��────────────────────────
step "[2/5] 检查 Ollama 与模型"
MODEL=$(python -c "
from app.config.loader import load_config
print(load_config(profile_name='$PROFILE').model)
")
if ! curl -sf -o /dev/null --max-time 3 http://localhost:11434/api/tags; then
    fail "Ollama 服务无响应。先运行: bash scripts/install_local_model.sh"
fi
if ! ollama list | awk 'NR>1 {print $1}' | grep -qx "$MODEL"; then
    fail "模型 $MODEL 未下载。运行: ollama pull $MODEL"
fi
ok "Ollama 服务正常,模型 $MODEL 已就绪"
echo

# ── 3. 简单对话 ─────────────────────────────────────��───────────────────────
step "[3/5] 简单对话(无工具)"
OUT=$(python -m app --profile "$PROFILE" -p "请只回复 OK 两个字符,不要别的" -y 2>&1)
if echo "$OUT" | grep -q "OK"; then
    ok "模型响应正常"
else
    fail "模型响应异常: $(echo "$OUT" | tail -3)"
fi
echo

# ── 4. 工具调用闭环 ─────────────────────────────────────────────────────────
step "[4/5] 工具调用闭环(list_dir)"
OUT=$(python -m app --profile "$PROFILE" \
    -p "用 list_dir 列出当前目录,然后只用一句话总结有什么" -y 2>&1)
if echo "$OUT" | grep -q "list_dir"; then
    ok "模型成功调用了 list_dir 工具"
else
    fail "模型没有调用工具,可能 profile 缺 capabilities: tools 或模型不支持工具调用"
fi
echo

# ── 5. todo_write 任务面板 ──────────────────────────────────────────────────
step "[5/5] todo_write 任务面板"
OUT=$(python -m app --profile "$PROFILE" \
    -p "用 todo_write 列出 2 个虚构任务后回复完成" -y 2>&1)
if echo "$OUT" | grep -qE "todo_write|tasks \("; then
    ok "todo_write 工具调用成功"
else
    fail "模型没有调用 todo_write,看下完整输出:$OUT"
fi
echo

# ── 完成 ─────────────────────────────────────────────────────────────────────
echo
printf "${GREEN}${BOLD}✅ 全部通过${RESET}\n"
echo
echo "本地链路工作正常。可以正式使用了:"
echo
echo "  ${CYAN}python -m app --profile $PROFILE${RESET}"
echo
