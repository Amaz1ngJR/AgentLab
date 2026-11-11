"""文件工具 —— 提供读文件、写文件、局部编辑、列目录四个基础工具。

使用场景:
  Agent 需要查看代码、读取配置、写入结果时调用这些工具。
  启动时通过 default_tools() 获取列表,注册到 ToolRegistry 即可。

安全说明:
  - 路径会被 expanduser + resolve 展开成绝对路径,支持 ~ 写法
  - workspace 内只读操作免审批;路径越界时必须通过独立的 outside_workspace 审批
  - read_file 按行分页(默认 200 行 / 9000 字符),大文件不会被整个读进内存
  - write_file / edit_file 标记了 requires_approval=True,执行前会弹出人工确认;
    CLI 在确认前会展示彩色 diff。改文件优先用 edit_file(局部替换)而非 shell。

数据完整性说明(重要):
  - 读文件用 newline="" 保留原始换行符(CRLF 不会被规范化成 LF),写回也用
    newline="" 原样落盘。否则只改一行就会把整个 Windows 检出文件的换行符重写。
  - edit_file 以 errors="strict" 解码:文件不是合法 UTF-8 时拒绝编辑,而不是用
    U+FFFD 替换后再写回(那会不可逆地损坏原字节)。
  - 所有写入先落到同目录临时文件再 os.replace,崩溃/被杀不会留下半截文件。
"""
from __future__ import annotations

import json
import os
import stat
import uuid
import threading
from collections import OrderedDict
from pathlib import Path

from app.config.loader import workspace_root
from app.tools.registry import Tool, ToolExecutionError, is_approval_granted

# ── 读取上限 ──────────────────────────────────────────────────────────────────
# 单页字符上限。刻意低于 executor 的 TOOL_OUTPUT_MAX_CHARS(10_000):超过它的
# 输出会被全局截成"头 + 省略标记 + 尾",模型既看不清中间也没法续读。分页让
# read_file 永远输出结构化、可续读的完整页,不触发那次截断。
MAX_READ_CHARS = 9_000
DEFAULT_READ_LINES = 200       # 默认每页最多返回多少行
MAX_READ_LINES = 2_000         # 单次请求的行数上限
MAX_LINE_CHARS = 2_000         # 单行超长(压缩文件)时截断
BINARY_PROBE_BYTES = 8_192     # 前 8KB 含 NUL 即判为二进制,拒绝读取

# 目录列表上限。超出的条目不再原样返回,避免被外层截断后"中间静默消失"。
MAX_LIST_ENTRIES = 500


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


# ── 编码 / 换行 / 原子写入 ────────────────────────────────────────────────────

def read_text_preserving(path: Path, *, strict: bool = False) -> str:
    """按原字节语义读取文本:保留 CRLF,不做通用换行转换。

    newline="" 让 Python 不去翻译 \\r\\n ↔ \\n,读到的就是磁盘上的内容。这是
    edit_file 能"只改一行"而不重写整个文件换行符的前提。

    strict=True 用于写入前的读取:非 UTF-8 直接抛错,避免用 U+FFFD 替换后再
    写回从而永久损坏原字节。strict=False 用于只读展示(预览 diff),有损但无害。
    """
    try:
        with path.open("r", encoding="utf-8", newline="",
                       errors="strict" if strict else "replace") as fh:
            return fh.read()
    except UnicodeDecodeError as exc:
        raise ToolExecutionError(
            f"refused: {path} 不是合法 UTF-8(解码失败于字节位置 {exc.start})。"
            "整文件重写会损坏原编码,已拒绝编辑;请改用能保留原字节的处理方式,"
            "或确认文件编码后再操作。"
        ) from exc


def _create_temp_sibling(directory: Path, name: str, mode: int = 0o666) -> tuple[int, Path]:
    """在目标目录内独占创建一个临时文件,返回 (fd, path)。

    用 os.open(O_CREAT|O_EXCL) 而不是 tempfile.mkstemp:mkstemp 固定建 0600,
    要还原成用户预期权限就得读进程 umask,而 os.umask() 是**进程级全局状态**,
    子 agent 线程并发写文件时会被短暂改坏。新文件按 0o666 与 umask 创建;
    覆盖文件则传入已有权限,临时文件从创建开始就不比目标更宽松。
    """
    for _ in range(100):
        candidate = directory / f".{name}.{uuid.uuid4().hex[:12]}.tmp"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
            return fd, candidate
        except FileExistsError:
            continue
    raise OSError(f"could not create a temporary file next to {name}")


def atomic_write_text(path: Path, text: str, *, permissions: int | None = None) -> None:
    """原子写入:同目录临时文件 → fsync → os.replace。

    直接 write_text 写到一半崩溃会留下截断的半成品文件;os.replace 在同一文件
    系统上是原子的,读者要么看到旧内容要么看到新内容。覆盖已有文件时同时沿用
    原文件权限位。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    if permissions is not None:
        mode = permissions
    fd, tmp = _create_temp_sibling(path.parent, path.name, mode if mode is not None else 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            if mode is not None:
                # Restore bits masked by umask before writing any private content.
                # Failure must leave the original file intact, not widen its permissions.
                os.chmod(tmp, mode)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _coerce_int(value, default: int, *, minimum: int, maximum: int) -> int:
    """模型偶尔把数字写成字符串;非法值退回默认值而不是报错。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


# Only seek offsets are cached; never cache file contents. Replacement invalidates offsets.
_READ_OFFSETS = OrderedDict()
_READ_OFFSETS_LOCK = threading.Lock()


def _read_start(path, offset):
    info = path.stat()
    version = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    key = (str(path), version)
    with _READ_OFFSETS_LOCK:
        points = {1: 0, **_READ_OFFSETS.get(key, {})}
    line = max((line for line in points if line <= offset), default=1)
    return key, points, line, points.get(line, 0)


def _read_page(args: dict, *, max_chars: int = MAX_READ_CHARS):
    """按行分页读取文本文件。

    返回单页内容(最多 DEFAULT_READ_LINES 行 / MAX_READ_CHARS 字符)。大文件不会
    被整体读进内存,模型用 offset 续读下一页。
    """
    try:
        path = _resolve_within_workspace(
            args["path"],
            "read_file_outside_workspace",
        )
    except WorkspacePathError as exc:
        raise ToolExecutionError(f"refused: {exc}") from exc
    if not path.exists():
        raise ToolExecutionError(f"file not found: {path}")
    if not path.is_file():
        raise ToolExecutionError(f"not a file: {path}")

    offset = _coerce_int(args.get("offset"), 1, minimum=1, maximum=10**9)
    limit = _coerce_int(
        args.get("limit"), DEFAULT_READ_LINES, minimum=1, maximum=MAX_READ_LINES,
    )

    # 二进制探测:只读前 8KB。NUL 字节在文本文件里几乎不出现,出现即拒绝,
    # 避免把二进制内容灌进模型上下文。
    try:
        with path.open("rb") as fh:
            head = fh.read(BINARY_PROBE_BYTES)
    except OSError as exc:
        raise ToolExecutionError(f"read failed: {exc}") from exc
    if b"\x00" in head:
        raise ToolExecutionError(
            f"refused: {path} 看起来是二进制文件;read_file 只读取文本。"
        )

    lines_out: list[str] = []
    chars = 0
    last_line = offset - 1
    has_more = False
    key, points, start_line, cookie = _read_start(path, offset)
    try:
        # newline="" 保留原始换行符；readline 允许保存 TextIO seek cookie。
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            opened = os.fstat(fh.fileno())
            version = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            if version != key[1]:
                key, points, start_line, cookie = (str(path), version), {1: 0}, 1, 0
            fh.seek(cookie)
            for lineno, line in enumerate(iter(fh.readline, ""), start=start_line):
                if lineno % 256 == 0 or lineno == offset - 1:
                    points[lineno + 1] = fh.tell()
                if lineno < offset:
                    continue
                if len(lines_out) >= limit:
                    has_more = True
                    break
                if len(line) > MAX_LINE_CHARS:
                    line = (
                        f"{line[:MAX_LINE_CHARS]}"
                        f"…[本行已截断,共 {len(line)} 字符]\n"
                    )
                if chars + len(line) > max_chars:
                    has_more = True
                    break
                lines_out.append(line)
                chars += len(line)
                last_line = lineno
                points[lineno + 1] = fh.tell()
    except OSError as exc:
        raise ToolExecutionError(f"read failed: {exc}") from exc

    # Check the version again: offsets from a concurrently rewritten file are discarded.
    if _read_start(path, 1)[0] == key:
        with _READ_OFFSETS_LOCK:
            # Limit both the number of files and checkpoints per file.
            merged = {**_READ_OFFSETS.get(key, {}), **points}
            _READ_OFFSETS[key] = dict(sorted(merged.items())[-2048:])
            _READ_OFFSETS.move_to_end(key)
            while len(_READ_OFFSETS) > 128:
                _READ_OFFSETS.popitem(last=False)

    return path, {"content": "".join(lines_out), "start_line": offset, "end_line": last_line,
                  "next_offset": last_line + 1 if has_more else None, "truncated": has_more}


def _read_file(args: dict) -> str:
    path, page = _read_page(args)
    offset = page["start_line"]
    last_line = page["end_line"]
    has_more = page["truncated"]
    limit = _coerce_int(args.get("limit"), DEFAULT_READ_LINES, minimum=1, maximum=MAX_READ_LINES)
    if not page["content"]:
        if offset > 1:
            return (f"{path}: offset {offset} 超出文件末尾"
                    f"(已读到最后一行,无更多内容)。")
        return f"{path}: (empty)"

    body = page["content"]
    # 只有真的分页了才加页眉页脚,普通小文件的返回值保持原样(向后兼容)。
    if offset > 1 or has_more:
        header = f"{path} (lines {offset}-{last_line})\n"
        footer = (
            f"\n[...单页上限 {limit} 行 / {MAX_READ_CHARS} 字符;"
            f"继续请传 offset={last_line + 1}]"
            if has_more else ""
        )
        return header + body + footer
    return body


def _write_file(args: dict) -> str:
    """把内容写入文件。父目录不存在时自动创建。已有文件会被覆盖。"""
    try:
        path = _resolve_within_workspace(
            args["path"],
            "write_file_outside_workspace",
        )
    except WorkspacePathError as exc:
        raise ToolExecutionError(f"refused: {exc}") from exc
    content = args.get("content", "")
    # 原子写 + newline="":内容原样落盘,不会把模型给的 \r\n 改写成 \n。
    atomic_write_text(path, content)
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
      - 文件用 CRLF 而模型只给了 LF 时,自动按文件换行风格匹配,并在结果里说明。
    """
    try:
        path = _resolve_within_workspace(
            args["path"],
            "edit_file_outside_workspace",
        )
    except WorkspacePathError as exc:
        raise ToolExecutionError(f"refused: {exc}") from exc
    if not path.exists():
        raise ToolExecutionError(f"file not found: {path}(新建文件请用 write_file)")
    if not path.is_file():
        raise ToolExecutionError(f"not a file: {path}")

    old_str = args["old_str"]
    new_str = args["new_str"]
    # strict 解码 + newline="":非 UTF-8 直接拒绝,CRLF 原样保留。
    text = read_text_preserving(path, strict=True)

    converted = False
    if old_str == "":
        # 空 old_str = 末尾追加
        updated = text + new_str
    else:
        count = text.count(old_str)
        if count == 0 and "\r\n" in text and "\r\n" not in old_str:
            # 模型给的是 LF 版本,而文件是 CRLF:按文件风格改写 old/new 再匹配,
            # 否则模型会陷入"read 看到 LF、edit 永远找不到"的重试循环。
            crlf_old = old_str.replace("\r\n", "\n").replace("\n", "\r\n")
            if text.count(crlf_old) == 1:
                old_str = crlf_old
                new_str = new_str.replace("\r\n", "\n").replace("\n", "\r\n")
                count = 1
                converted = True
        if count == 0:
            raise ToolExecutionError(
                "old_str 未在文件中找到。请先 read_file 确认原文(注意空格/缩进/"
                "换行完全一致),再重试。")
        if count > 1:
            raise ToolExecutionError(
                f"old_str 在文件中出现 {count} 次,不唯一。请在 old_str 里带上更多"
                "上下文(前后多几行)使其唯一,再重试。")
        updated = text.replace(old_str, new_str, 1)

    if updated == text:
        return "no change: old_str 与 new_str 相同,文件未改动。"
    atomic_write_text(path, updated)
    delta = len(updated) - len(text)
    sign = "+" if delta >= 0 else ""
    note = "（按文件 CRLF 换行匹配）" if converted else ""
    return f"edited {path}({sign}{delta} chars){note}"


def _list_dir(args: dict) -> str:
    """列出目录下的条目。目录名末尾带 / 以便区分文件和目录。

    超过 MAX_LIST_ENTRIES 时只返回前 N 条并说明剩余数量,而不是原样吐几千行
    再被外层整体截断(那样中间条目会静默消失,模型会误判目录规模)。
    """
    try:
        path = _resolve_within_workspace(
            args.get("path", "."),
            "list_dir_outside_workspace",
        )
    except WorkspacePathError as exc:
        raise ToolExecutionError(f"refused: {exc}") from exc
    if not path.exists():
        raise ToolExecutionError(f"directory not found: {path}")
    if not path.is_dir():
        raise ToolExecutionError(f"not a directory: {path}")
    children = sorted(path.iterdir())
    entries = [
        f"{child.name}{'/' if child.is_dir() else ''}"
        for child in children[:MAX_LIST_ENTRIES]
    ]
    if not entries:
        return f"(empty) {path}"
    output = f"{path}:\n" + "\n".join(entries)
    remaining = len(children) - MAX_LIST_ENTRIES
    if remaining > 0:
        output += (
            f"\n[...还有 {remaining} 个条目未显示;"
            "用 code_search mode=file 或 shell 精确定位具体文件]"
        )
    return output


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
    description="按行分页读取本地文本文件。默认返回第 1 页(最多 200 行 / 9000 字符),"
                "更大的文件用 offset 续读下一页。二进制文件会被拒绝。"
                "workspace 内免审批,读取外部路径前必须获得用户审批。",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径,支持 ~ 表示家目录"},
            "offset": {"type": "integer", "minimum": 1,
                       "description": "起始行号(1-based),默认 1。续读上一页时传页脚提示的 offset"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_LINES,
                      "description": f"本页最多返回多少行,默认 {DEFAULT_READ_LINES}"},
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
    parallel_safe=True,
)

WRITE_FILE = Tool(
    name="write_file",
    description="把内容写入本地文件。会覆盖已有文件,会自动创建父目录。"
                "内容按原样落盘(换行符不被改写),写入是原子的。"
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
    description="列出目录下的条目,目录名带 / 后缀。不传 path 时列当前目录。"
                f"超过 {MAX_LIST_ENTRIES} 个条目时只返回前 {MAX_LIST_ENTRIES} 个并说明剩余数量。"
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
    parallel_safe=True,
)

EDIT_FILE = Tool(
    name="edit_file",
    description="局部编辑已有文件:把 old_str 替换成 new_str。改配置、删/改几行时"
                "优先用它(而不是 write_file 覆盖整文件,更不要用 shell 改文件)。"
                "规则:old_str 必须在文件里唯一出现;old_str 传空串表示在末尾追加"
                "new_str;new_str 传空串表示删除 old_str 这段。文件不存在时请改用"
                "write_file。保留原换行符;非 UTF-8 文件会被拒绝编辑。"
                "编辑始终审批;外部路径使用独立的越界审批。",
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
