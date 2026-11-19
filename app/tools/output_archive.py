"""Workspace-isolated, bounded output recovery outside the source tree."""
import hashlib
import os
from pathlib import Path
import re
import sys
import time

from filelock import FileLock, Timeout

from app.config.loader import workspace_root
from app.util.redact import redact

# 这是"读回被截断/压缩掉的完整输出"的临时缓存，不是产物留存：模型通常在同一次
# 任务里几次调用之内就会取完。读取和重复归档都会 touch 续期，所以正在用的条目
# 不会因为这条时限消失；留长只会在固定的 256 MiB 上限下提前触发 LRU，把本轮真正
# 需要的输出挤掉。30 分钟既覆盖一次长构建/长测试的翻看需求，又不至于积压。
RETENTION_SECONDS = 30 * 60
MAX_CACHE_BYTES = 256 * 1024 * 1024  # Per workspace.
_ARCHIVE_NAME = re.compile(r"[0-9a-f]{64}\.txt")
_OUTPUT_ID = re.compile(r"out_[0-9a-f]{64}")


def _archive_dir() -> Path:
    override = os.environ.get("AGENTLAB_OUTPUT_CACHE_DIR")
    if override:
        base = Path(override).expanduser()
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Caches/AgentLab/tool-output"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "AgentLab/Cache/tool-output"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "agentlab/tool-output"
    base = base.resolve()
    namespace = hashlib.sha256(str(workspace_root().resolve()).encode()).hexdigest()
    directory = base / namespace
    if directory.is_symlink():
        raise ValueError("output cache namespace must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _cleanup_expired(directory, now, *, keep=None):
    entries = []
    for entry in directory.iterdir():
        if not _ARCHIVE_NAME.fullmatch(entry.name) or entry.is_symlink():
            continue
        try:
            info = entry.stat()
            if not entry.is_file():
                continue
            if entry != keep and info.st_mtime <= now - RETENTION_SECONDS:
                entry.unlink()
            else:
                entries.append((info.st_mtime, entry.name, info.st_size, entry))
        except OSError:
            continue  # A failed deletion must not break tool execution.
    total = sum(item[2] for item in entries)
    for _, _, size, entry in sorted(entries):
        if total <= MAX_CACHE_BYTES:
            break
        if entry == keep:
            continue
        try:
            entry.unlink()
            total -= size
        except OSError:
            continue


def archive_output(text: str) -> str | None:
    text = redact(text)
    if len(text.encode("utf-8")) > MAX_CACHE_BYTES:
        return None
    digest = hashlib.sha256(text.encode()).hexdigest()
    try:
        path = _archive_dir() / (digest + ".txt")
        with FileLock(str(path.parent / ".retention.lock"), timeout=3):
            if path.is_symlink():
                return None
            if path.exists():
                with path.open(encoding="utf-8", newline="") as source:
                    if source.read() != text:
                        return None
                path.touch()
            else:
                from app.tools.builtin.files import atomic_write_text
                atomic_write_text(path, text, permissions=0o600)
            _cleanup_expired(path.parent, time.time(), keep=path)
        return "out_" + digest
    except (OSError, ValueError, Timeout):
        return None


def read_output(args: dict) -> str:
    from app.tools.builtin.files import _read_page
    from app.tools.registry import ToolExecutionError

    output_id = args.get("output_id", "")
    if not isinstance(output_id, str) or not _OUTPUT_ID.fullmatch(output_id):
        raise ToolExecutionError("invalid output_id: use the out_ ID from the tool result")
    try:
        path = _archive_dir() / (output_id[4:] + ".txt")
        with FileLock(str(path.parent / ".retention.lock"), timeout=3):
            _cleanup_expired(path.parent, time.time())
            if path.is_symlink() or not path.is_file():
                raise ToolExecutionError("output unavailable: expired, evicted, or belongs to another workspace")
            _, page = _read_page(args, _trusted_path=path)
            path.touch()
        result = f"[{output_id}, lines {page['start_line']}-{page['end_line']}]\n" + page["content"]
        if page["truncated"]:
            result += (f"\n[继续使用 read_tool_output: output_id={output_id}"
                       f" offset={page['next_offset']}"
                       + (f" column_offset={page['next_column']}" if page['next_column'] is not None else "") + "]")
        return result
    except (OSError, ValueError, Timeout) as exc:
        raise ToolExecutionError("output cache unavailable") from exc


def output_reference(output_id):
    return f"[完整输出: {output_id}；使用 read_tool_output 的 output_id/offset/limit 按行读取]"
