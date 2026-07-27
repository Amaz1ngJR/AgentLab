"""文件工具 —— 提供读文件、写文件、局部编辑、列目录四个基础工具。

使用场景:
  Agent 需要查看代码、读取配置、写入结果时调用这些工具。
  启动时通过 default_tools() 获取列表,注册到 ToolRegistry 即可。

安全说明:
  - 路径会被 expanduser + resolve 展开成绝对路径,支持 ~ 写法
  - workspace 内只读操作免审批;路径越界时必须通过独立的 outside_workspace 审批
  - read_file 单次最多读 200KB,防止把超大文件全塞进模型上下文
  - write_file / edit_file 标记了 requires_approval=True,执行前会弹出人工确认;
    CLI 在确认前会展示彩色 diff。改文件优先用 edit_file(局部替换)而非 shell。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.config.loader import workspace_root
from app.tools.registry import Tool, is_approval_granted

# 单次读取上限:200KB。超出部分截断并附提示,避免撑爆模型上下文窗口。
MAX_READ_BYTES = 200_000


class WorkspacePathError(Exception):
    """路径越出 workspace 且没有对应审批。"""


def _resolve_path(path_str: str) -> Path:
    """相对路径基于 workspace 展开，绝对路径保持其原始目标。"""
    root = workspace_root()
    raw = Path(path_str).expanduser()
    return (raw if raw.is_absolute() else root / raw).resolve()


def _is_outside_workspace(target: Path) -> bool:
    root = workspace_root()
    try:
        target.relative_to(root)
        return False
    except ValueError:
        return True


def _outside_workspace_approval(
    tool_name: str,
    args: dict,
    *,
    path_key: str = "path",
    default: str = ".",
) -> str | None:
    """路径越界时返回独立审批动作名，供 Tool 动态判定。"""
    raw = args.get(path_key, default)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        target = _resolve_path(raw)
    except (OSError, RuntimeError):
        return None
    if _is_outside_workspace(target):
        return f"{tool_name}_outside_workspace"
    return None


def _resolve_within_workspace(
    path_str: str,
    outside_approval: str | None = None,
) -> Path:
    """解析路径；越界时要求 ToolRegistry 授予对应审批动作。

    保留原函数名以兼容 CLI diff 和既有调用。未传 outside_approval 的调用仍是
    严格 workspace 解析，适合审批前预览等不应读取外部文件的场景。
    """
    root = workspace_root()
    target = _resolve_path(path_str)
    if _is_outside_workspace(target):
        if outside_approval and is_approval_granted(outside_approval):
            return target
        raise WorkspacePathError(
            f"path '{target}' is outside workspace '{root}'. "
            f"approval '{outside_approval or 'outside_workspace'}' is required."
        )
    return target


def _read_file(args: dict) -> str:
    """读取文本文件内容。超过 200KB 时截断并附说明。"""
    try:
        path = _resolve_within_workspace(
            args["path"],
            "read_file_outside_workspace",
        )
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
        path = _resolve_within_workspace(
            args["path"],
            "write_file_outside_workspace",
        )
    except WorkspacePathError as exc:
        return f"refused: {exc}"
    content = args.get("content", "")
    # parents=True 递归创建多级目录;exist_ok=True 目录已存在时不报错
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


def _edit_file(args: dict) -> str:
    """把文件里的 old_str 替换成 new_str(局部编辑,不覆盖整文件)。

    设计意图:
      模型改配置 / 删几行时,不该用 shell 重定向或 python -c 偷偷改文件(那样
      审批前看不到 diff、还可能空跑)。edit_file 明确给出 old_str/new_str,CLI
      在审批前就能算出并展示 diff,和 write_file 一致。

    规则(对齐 Claude Code 的 str_replace 语义):
      - old_str 必须在文件中唯一出现,否则报错(避免改错地方)。多处命中时
        让模型带上更多上下文重试。
      - old_str 为空串表示"在文件末尾追加 new_str"(常见的加一行需求)。
      - new_str 为空串表示"删除 old_str 这段"。
      - 文件不存在时报错(新建请用 write_file)。
    """
    try:
        path = _resolve_within_workspace(
            args["path"],
            "edit_file_outside_workspace",
        )
    except WorkspacePathError as exc:
        return f"refused: {exc}"
    if not path.exists():
        return f"file not found: {path}(新建文件请用 write_file)"
    if not path.is_file():
        return f"not a file: {path}"

    old_str = args.get("old_str", "")
    new_str = args.get("new_str", "")
    text = path.read_text(encoding="utf-8", errors="replace")

    if old_str == "":
        # 空 old_str = 末尾追加
        updated = text + new_str
    else:
        count = text.count(old_str)
        if count == 0:
            return ("old_str 未在文件中找到。请先 read_file 确认原文(注意空格/缩进/"
                    "换行完全一致),再重试。")
        if count > 1:
            return (f"old_str 在文件中出现 {count} 次,不唯一。请在 old_str 里带上更多"
                    f"上下文(前后多几行)使其唯一,再重试。")
        updated = text.replace(old_str, new_str, 1)

    if updated == text:
        return "no change: old_str 与 new_str 相同,文件未改动。"
    path.write_text(updated, encoding="utf-8")
    delta = len(updated) - len(text)
    sign = "+" if delta >= 0 else ""
    return f"edited {path}({sign}{delta} chars)"


def _list_dir(args: dict) -> str:
    """列出目录下的所有条目。目录名末尾带 / 以便区分文件和目录。"""
    try:
        path = _resolve_within_workspace(
            args.get("path", "."),
            "list_dir_outside_workspace",
        )
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


def _read_audit_summary(args: dict, result: str) -> tuple[str, str]:
    return (
        json.dumps({"path": args.get("path", "")}, ensure_ascii=False),
        f"returned_chars={len(result)}",
    )


def _write_audit_summary(args: dict, result: str) -> tuple[str, str]:
    summary = {"path": args.get("path", "")}
    for key in ("content", "old_str", "new_str"):
        if key in args:
            summary[f"{key}_chars"] = len(str(args.get(key, "")))
    return json.dumps(summary, ensure_ascii=False), result


READ_FILE = Tool(
    name="read_file",
    description="读取本地文本文件的内容。返回 UTF-8 文本,超过 200KB 会截断。"
                "workspace 内免审批,读取外部路径前必须获得用户审批。",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径,支持 ~ 表示家目录"},
        },
        "required": ["path"],
    },
    executor=_read_file,
    risk="read",
    target_type="filesystem",
    scope="workspace_or_approved_external",
    origin="builtin",
    audit_redactor=_read_audit_summary,
    requires_approval=False,  # 只读,不需要确认
    approval_resolver=lambda args: _outside_workspace_approval("read_file", args),
)

WRITE_FILE = Tool(
    name="write_file",
    description="把内容写入本地文件。会覆盖已有文件,会自动创建父目录。"
                "写入始终审批;外部路径使用独立的越界审批。",
    input_schema={
        "type": "object",
        "properties": {
            "path":    {"type": "string", "description": "目标文件路径"},
            "content": {"type": "string", "description": "要写入的文本内容"},
        },
        "required": ["path", "content"],
    },
    executor=_write_file,
    risk="write",
    target_type="filesystem",
    scope="workspace_or_approved_external",
    origin="builtin",
    audit_redactor=_write_audit_summary,
    requires_approval=True,   # 写操作,执行前弹出 y/a/n 确认
    approval_resolver=lambda args: _outside_workspace_approval("write_file", args),
)

LIST_DIR = Tool(
    name="list_dir",
    description="列出目录下的所有条目,目录名带 / 后缀。不传 path 时列当前目录。"
                "workspace 内免审批,列出外部目录前必须获得用户审批。",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径,默认为当前目录 '.'"},
        },
        "required": [],  # path 是可选参数
    },
    executor=_list_dir,
    risk="read",
    target_type="filesystem",
    scope="workspace_or_approved_external",
    origin="builtin",
    audit_redactor=_read_audit_summary,
    requires_approval=False,  # 只读,不需要确认
    approval_resolver=lambda args: _outside_workspace_approval(
        "list_dir",
        args,
    ),
)

EDIT_FILE = Tool(
    name="edit_file",
    description="局部编辑已有文件:把 old_str 替换成 new_str。改配置、删/改几行时"
                "优先用它(而不是 write_file 覆盖整文件,更不要用 shell 改文件)。"
                "规则:old_str 必须在文件里唯一出现;old_str 传空串表示在末尾追加"
                "new_str;new_str 传空串表示删除 old_str 这段。文件不存在时请改用"
                "write_file。编辑始终审批;外部路径使用独立的越界审批。",
    input_schema={
        "type": "object",
        "properties": {
            "path":    {"type": "string", "description": "要编辑的文件路径"},
            "old_str": {"type": "string",
                        "description": "被替换的原文(需与文件内容逐字符一致,含空格/换行);"
                                       "传空串表示在文件末尾追加"},
            "new_str": {"type": "string",
                        "description": "替换成的新内容;传空串表示删除 old_str 这段"},
        },
        "required": ["path", "old_str", "new_str"],
    },
    executor=_edit_file,
    risk="write",
    target_type="filesystem",
    scope="workspace_or_approved_external",
    origin="builtin",
    audit_redactor=_write_audit_summary,
    requires_approval=True,   # 写操作,执行前弹确认(审批前会显示 diff)
    approval_resolver=lambda args: _outside_workspace_approval("edit_file", args),
)


def default_tools() -> list[Tool]:
    """返回默认工具列表。启动时注册到 ToolRegistry 使用。"""
    return [READ_FILE, WRITE_FILE, EDIT_FILE, LIST_DIR]
