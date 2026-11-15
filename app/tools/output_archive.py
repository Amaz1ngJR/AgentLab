"""Immutable, workspace-local full outputs for on-demand paged reads."""
import hashlib
import re
import time

from filelock import FileLock, Timeout

from app.config.loader import workspace_root
from app.util.redact import redact

RETENTION_SECONDS = 2 * 60 * 60
_ARCHIVE_NAME = re.compile(r"[0-9a-f]{64}\.txt")


def _cleanup_expired(directory, now):
    for entry in directory.iterdir():
        if not _ARCHIVE_NAME.fullmatch(entry.name) or entry.is_symlink():
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime <= now - RETENTION_SECONDS:
                entry.unlink()
        except OSError:
            continue  # A failed deletion must not break tool execution.


def archive_output(text: str) -> str | None:
    text = redact(text)
    root = workspace_root().resolve()
    path = root / ".agentlab/tool-output" / (hashlib.sha256(text.encode()).hexdigest() + ".txt")
    try:
        path.resolve().relative_to(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path.parent / ".retention.lock"), timeout=3):
            if path.is_symlink():
                return None
            try:
                with path.open("x", encoding="utf-8") as out:
                    out.write(text)
            except FileExistsError:
                if path.read_text(encoding="utf-8") != text:
                    return None
                path.touch()  # Reuse extends retention without changing content.
            _cleanup_expired(path.parent, time.time())
        return path.relative_to(root).as_posix()
    except (OSError, ValueError, Timeout):
        return None


def output_reference(path):
    return f"[完整输出: {path}；使用 read_file 的 offset/limit 按行读取]"
