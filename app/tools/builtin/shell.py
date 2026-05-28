"""Shell 工具 —— 跨平台执行命令。

使用场景:
  Agent 需要跑测试、查 git 状态、构建项目等"系统命令"时调用。

安全约束:
  - requires_approval=True,每次执行前用户必须显式同意(默认行为;-y 模式跳过)
  - cwd 锁定在 workspace_root(),命令的相对路径都基于 workspace
  - timeout 默认 30s,超时后子进程被杀掉
  - 输出截断到 8KB,避免巨型日志撑爆模型上下文

跨平台说明:
  - Unix(macOS / Linux): 用 ["bash", "-c", command],利用 shell 解析管道、
    重定向、引号等;模型可写自然 shell 脚本片段
  - Windows: 用 ["powershell", "-NoProfile", "-Command", command],
    -NoProfile 避免加载用户个人配置(更可重现);PowerShell 比 cmd 表达力更强

为什么不用 shell=True?
  shell=True 会把 command 字符串通过登录 shell 解析,行为依赖 SHELL 环境变量,
  Windows 下还会走 cmd.exe,跨平台不可控。明确给 ["bash"/"powershell", ...]
  能让命令解释方式可预期。
"""
from __future__ import annotations

import platform
import subprocess

from app.config.loader import workspace_root
from app.tools.registry import Tool

# 单次输出上限:8KB。超出截断并附说明,避免让模型上下文吞下兆级 log。
MAX_OUTPUT_BYTES = 8_000

# 默认超时:30 秒。模型可在 args 里指定更长(比如 pytest 大概要 60s+)。
DEFAULT_TIMEOUT = 30


def _build_argv(command: str) -> list[str]:
    """根据当前操作系统构造 subprocess 的 argv。

    返回的 argv 第一个元素是 shell 可执行,后面是参数;subprocess.run 会用
    这个 argv 直接 exec(没有 shell=True 的二次解析)。
    """
    if platform.system() == "Windows":
        # -NoProfile: 不加载用户 PowerShell profile,行为更确定
        # -Command: 后跟一段 PowerShell 脚本字符串
        return ["powershell", "-NoProfile", "-Command", command]
    # Unix(macOS / Linux): bash -c 让模型可写带管道、重定向的脚本片段
    return ["bash", "-c", command]


def _run_shell(args: dict) -> str:
    """执行命令,返回合并后的 stdout / stderr / exit code 文本。"""
    command = args.get("command", "")
    if not command:
        return "refused: empty command"

    # timeout 字段允许 int 或 str(模型有时给字符串)
    try:
        timeout = int(args.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    cwd = workspace_root()
    argv = _build_argv(command)

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
            # errors="replace" 处理非 UTF-8 输出(比如某些命令输出 GBK)
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        # 超时不抛异常,作为错误字符串返回给模型,让它自己决定是否调高 timeout
        return f"timeout: command exceeded {timeout}s and was killed"
    except FileNotFoundError as exc:
        # 没装 bash / powershell 时
        return f"shell not found: {exc}"

    # ── 拼装输出 ─────────────────────────────────────────────────────────────
    # 顺序: stdout 主体 → [stderr] 标记 + stderr 内容 → exit code
    # 模型看到这种结构能立刻判断"是否成功 + 错误原因"
    parts: list[str] = []
    if proc.stdout:
        parts.append(proc.stdout.rstrip())
    if proc.stderr:
        parts.append("[stderr]")
        parts.append(proc.stderr.rstrip())
    parts.append(f"[exit code: {proc.returncode}]")

    output = "\n".join(parts)

    # 截断超长输出。把截断标记放末尾,模型能看见"原始输出多大",决定要不要
    # 改命令(比如加 head / tail)
    if len(output) > MAX_OUTPUT_BYTES:
        truncated = output[:MAX_OUTPUT_BYTES]
        output = f"{truncated}\n\n[...truncated, total {len(output)} bytes]"
    return output


SHELL = Tool(
    name="shell",
    description=(
        "在 workspace 目录下执行 shell 命令(macOS/Linux 用 bash,Windows 用 PowerShell)。"
        "返回 stdout / stderr / exit code。超过 8KB 的输出会被截断。"
        "默认超时 30 秒,可通过 timeout 字段指定更长(单位秒)。"
        "适用:跑测试、看 git 状态、构建项目、列文件等。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的命令字符串,例如 'git status' 或 'pytest tests/'",
            },
            "timeout": {
                "type": "integer",
                "description": f"超时秒数,默认 {DEFAULT_TIMEOUT}",
                "default": DEFAULT_TIMEOUT,
            },
        },
        "required": ["command"],
    },
    executor=_run_shell,
    requires_approval=True,  # shell 命令属于高风险操作,默认强制审批
)


def default_tools() -> list[Tool]:
    """返回 shell 模块的默认工具列表。"""
    return [SHELL]
