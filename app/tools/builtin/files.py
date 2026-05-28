"""文件工具 —— 提供读文件、写文件、列目录三个基础工具。

使用场景:
  Agent 需要查看代码、读取配置、写入结果时调用这些工具。
  启动时通过 default_tools() 获取列表,注册到 ToolRegistry 即可。

安全说明:
  - 路径会被 expanduser + resolve 展开成绝对路径,支持 ~ 写法
  - 所有操作都被限制在 workspace_root() 返回的目录内,越界请求会被拒绝
  - read_file 单次最多读 200KB,防止把超大文件全塞进模型上下文
  - write_file 标记了 requires_approval=True,执行前会弹出人工确认
"""
from __future__ import annotations

from pathlib import Path

from app.config.loader import workspace_root
from app.tools.registry import Tool

# 单次读取上限:200KB。超出部分截断并附提示,避免撑爆模型上下文窗口。
MAX_READ_BYTES = 200_000


class WorkspacePathError(Exception):
    """路径越出 workspace 范围。被工具捕获,转成给模型的错误字符串。"""


def _resolve_within_workspace(path_str: str) -> Path:
    """把路径展开成绝对路径,并校验是否在 workspace 之下。

    越界时抛 WorkspacePathError;调用方捕获后返回错误字符串给模型。
    """
    target = Path(path_str).expanduser().resolve()
    root = workspace_root()
    try:
        target.relative_to(root)
    except ValueError:
        raise WorkspacePathError(
            f"path '{target}' is outside workspace '{root}'. "
            f"set WORKSPACE_ROOT in .env if you need to access this path."
        )
    return target


def _read_file(args: dict) -> str:
    """读取文本文件内容。超过 200KB 时截断并附说明。"""
    try:
        path = _resolve_within_workspace(args["path"])
    except WorkspacePathError as exc:
        return f"refused: {exc}"
    if not path.exists():
        return f"file not found: {path}"
    if not path.is_file():
        return f"not a file: {path}"
    data = path.read_bytes()
    truncated = len(data) > MAX_READ_BYTES
    # errors="replace" 遇到非 UTF-8 字节时用 ? 替代,不报错
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    suffix = f"\n\n[...truncated, total {len(data)} bytes]" if truncated else ""
    return text + suffix


def _write_file(args: dict) -> str:
    """把内容写入文件。父目录不存在时自动创建。已有文件会被覆盖。"""
    try:
        path = _resolve_within_workspace(args["path"])
    except WorkspacePathError as exc:
        return f"refused: {exc}"
    content = args.get("content", "")
    # parents=True 递归创建多级目录;exist_ok=True 目录已存在时不报错
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


def _list_dir(args: dict) -> str:
    """列出目录下的所有条目。目录名末尾带 / 以便区分文件和目录。"""
    try:
        path = _resolve_within_workspace(args.get("path", "."))
    except WorkspacePathError as exc:
        return f"refused: {exc}"
    if not path.exists():
        return f"directory not found: {path}"
    if not path.is_dir():
        return f"not a directory: {path}"
    entries = []
    for child in sorted(path.iterdir()):
        marker = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{marker}")
    if not entries:
        return f"(empty) {path}"
    return f"{path}:\n" + "\n".join(entries)


# ── 工具实例 ──────────────────────────────────────────────────────────────────
# input_schema 是 JSON Schema 格式,模型根据它知道该传哪些参数。
# description 是给模型看的自然语言说明,写清楚功能和限制。

READ_FILE = Tool(
    name="read_file",
    description="读取本地文本文件的内容。返回 UTF-8 文本,超过 200KB 会截断。"
                "路径必须在 workspace 内,否则会被拒绝。",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径,支持 ~ 表示家目录"},
        },
        "required": ["path"],
    },
    executor=_read_file,
    requires_approval=False,  # 只读,不需要确认
)

WRITE_FILE = Tool(
    name="write_file",
    description="把内容写入本地文件。会覆盖已有文件,会自动创建父目录。"
                "路径必须在 workspace 内,否则会被拒绝。",
    input_schema={
        "type": "object",
        "properties": {
            "path":    {"type": "string", "description": "目标文件路径"},
            "content": {"type": "string", "description": "要写入的文本内容"},
        },
        "required": ["path", "content"],
    },
    executor=_write_file,
    requires_approval=True,   # 写操作,执行前弹出 y/a/n 确认
)

LIST_DIR = Tool(
    name="list_dir",
    description="列出目录下的所有条目,目录名带 / 后缀。不传 path 时列当前目录。"
                "路径必须在 workspace 内,否则会被拒绝。",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径,默认为当前目录 '.'"},
        },
        "required": [],  # path 是可选参数
    },
    executor=_list_dir,
    requires_approval=False,  # 只读,不需要确认
)


def default_tools() -> list[Tool]:
    """返回默认工具列表。启动时注册到 ToolRegistry 使用。"""
    return [READ_FILE, WRITE_FILE, LIST_DIR]
