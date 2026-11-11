"""Multi-file context patches with whole-patch preflight and bounded matching."""
from __future__ import annotations

from dataclasses import dataclass
import difflib
from pathlib import Path
import stat
import threading
import time

from app.config.loader import workspace_root
from app.tools.registry import Tool, ToolExecutionError
from app.tools.builtin.files import atomic_write_text, read_text_preserving

_LOCK = threading.RLock()


@dataclass
class Operation:
    kind: str
    path: str
    body: list[str]
    move: str | None = None


@dataclass
class Change:
    path: Path
    before: str | None
    after: str | None
    mode: int | None


def parse_patch(patch: str) -> list[Operation]:
    if len(patch) > 2_000_000:
        raise ToolExecutionError("patch exceeds 2MB; split into smaller patches")
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ToolExecutionError("patch must start with *** Begin Patch and end with *** End Patch")
    operations = []
    index = 1
    while index < len(lines) - 1:
        header = lines[index]
        kind = next((kind for kind in ("Add", "Update", "Delete") if header.startswith(f"*** {kind} File: ")), None)
        if kind is None:
            raise ToolExecutionError(f"invalid patch header at line {index + 1}: {header[:120]}")
        path = header.split(": ", 1)[1]
        index += 1
        move = None
        if index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
            if kind != "Update":
                raise ToolExecutionError("Move to is only valid after Update File")
            move = lines[index][13:]
            index += 1
        body = []
        while index < len(lines) - 1 and not lines[index].startswith(("*** Add File:", "*** Update File:", "*** Delete File:")):
            body.append(lines[index])
            index += 1
        if kind == "Delete" and body:
            raise ToolExecutionError(f"Delete File must not contain hunks: {path}")
        operations.append(Operation(kind, path, body, move))
        if len(operations) > 100:
            raise ToolExecutionError("at most 100 files per patch")
    if not operations:
        raise ToolExecutionError("empty patch")
    return operations


def _path(raw: str) -> Path:
    root = workspace_root().resolve()
    value = Path(raw).expanduser()
    if not raw.strip() or "\x00" in raw or ".." in value.parts:
        raise ToolExecutionError(f"invalid patch path: {raw!r}")
    path = value if value.is_absolute() else root / value
    if not path.resolve().is_relative_to(root):
        raise ToolExecutionError(f"patch path outside workspace: {raw}")
    relative = path.relative_to(root)
    if not relative.parts or any(part.casefold() == ".git" for part in relative.parts):
        raise ToolExecutionError("patch cannot modify Git metadata or workspace root")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ToolExecutionError(f"patch refuses symlink path: {raw}")
    return path


def patch_paths(args: dict) -> list[Path]:
    return [path for op in parse_patch(args["patch"]) for path in
            ([_path(op.path), _path(op.move)] if op.move else [_path(op.path)])]


def _seek(lines, pattern, start, eof, path):
    if not pattern:
        return len(lines)
    candidates = [len(lines) - len(pattern)] if eof else range(start, len(lines) - len(pattern) + 1)
    for normalize in (lambda s: s, str.rstrip, str.strip):
        matches = [i for i in candidates if i >= start and
                   [normalize(s) for s in lines[i:i + len(pattern)]] == [normalize(s) for s in pattern]]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ToolExecutionError(f"{path}: context is ambiguous at lines {[i + 1 for i in matches[:8]]}; add context or @@ anchor")
    snippet = "\\n".join(pattern[:3])[:160]
    raise ToolExecutionError(f"{path}: context not found after line {start + 1}: {snippet!r}; read_file and refresh patch")


def _update(text: str, body: list[str], path: str) -> str:
    raw = text.split("\n")
    raw = [line + "\n" for line in raw[:-1]] + ([raw[-1]] if raw[-1] else [])
    lines = [line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line for line in raw]
    newline = "\r\n" if "\r\n" in text else "\n"
    terminal = text.endswith(("\n", "\r"))
    cursor = 0
    index = 0
    while index < len(body):
        anchor = ""
        if body[index].startswith("@@"):
            if body[index] != "@@" and not body[index].startswith("@@ "):
                raise ToolExecutionError(f"{path}: invalid @@ anchor")
            anchor = body[index][3:]
            index += 1
        if anchor:
            cursor = _seek(lines, [anchor], cursor, False, path) + 1
        chunk = []
        eof = False
        while index < len(body) and not body[index].startswith("@@"):
            line = body[index]
            index += 1
            if line == "*** End of File":
                eof = True
                if index != len(body):
                    raise ToolExecutionError(f"{path}: End of File must be last")
                break
            if not line or line[0] not in " +-":
                raise ToolExecutionError(f"{path}: invalid hunk line {line[:100]!r}; prefix context with a space")
            chunk.append(line)
        if not chunk:
            raise ToolExecutionError(f"{path}: empty update hunk")
        old = [line[1:] for line in chunk if line[0] in " -"]
        start = _seek(lines, old, cursor, eof, path) if old else (cursor if anchor else len(lines))
        replacement = []
        replacement_raw = []
        offset = 0
        for line in chunk:
            if line[0] == " ":
                replacement.append(lines[start + offset])  # Preserve original whitespace in context.
                replacement_raw.append(raw[start + offset])
            elif line[0] == "+":
                replacement.append(line[1:])
                replacement_raw.append(line[1:] + newline)
            if line[0] in " -":
                offset += 1
        if start and replacement_raw and not raw[start - 1].endswith("\n"):
            raw[start - 1] += newline
        raw[start:start + len(old)] = replacement_raw
        lines[start:start + len(old)] = replacement
        cursor = start + len(replacement)
    if not body:
        raise ToolExecutionError(f"{path}: missing update hunks")
    result = "".join(raw)
    if not terminal and result.endswith(newline):
        result = result[:-len(newline)]
    return result


def prepare_patch(args: dict) -> list[Change]:
    changes = []
    used = set()
    for op in parse_patch(args["patch"]):
        source = _path(op.path)
        target = _path(op.move) if op.move else source
        for path in {source, target}:
            if path in used:
                raise ToolExecutionError(f"duplicate patch path: {path}; combine its hunks")
            used.add(path)
        if source.exists() and not source.is_file():
            raise ToolExecutionError(f"not a file: {source}")
        before = read_text_preserving(source, strict=True) if source.exists() else None
        if before is not None and "\x00" in before:
            raise ToolExecutionError(f"binary file refused: {source}")
        mode = stat.S_IMODE(source.stat().st_mode) if source.exists() else None
        if op.kind == "Add":
            if before is not None or any(not line.startswith("+") for line in op.body):
                raise ToolExecutionError(f"Add File requires a new path and + lines: {source}")
            after = "".join(line[1:] + "\n" for line in op.body)
        else:
            if before is None:
                raise ToolExecutionError(f"file not found: {source}")
            after = None if op.kind == "Delete" else _update(before, op.body, op.path)
        if target != source:
            if target.exists():
                raise ToolExecutionError(f"move destination exists: {target}")
            changes.extend([Change(target, None, after, mode), Change(source, before, None, mode)])
        else:
            changes.append(Change(source, before, after, mode))
    return changes


def preview_patch(args: dict) -> str:
    return "\n".join("".join(difflib.unified_diff(
        (change.before or "").splitlines(True), (change.after or "").splitlines(True),
        fromfile=str(change.path) if change.before is not None else "/dev/null",
        tofile=str(change.path) if change.after is not None else "/dev/null"))
        for change in prepare_patch(args))


def _current(change: Change) -> str | None:
    _path(str(change.path))
    return read_text_preserving(change.path, strict=True) if change.path.exists() else None


def apply_patch(args: dict) -> str:
    started = time.perf_counter()
    with _LOCK:
        changes = prepare_patch(args)
        prepared = time.perf_counter()
        applied = []
        try:
            for change in changes:
                if _current(change) != change.before:
                    raise ToolExecutionError(f"file changed during patch: {change.path}")
            for change in changes:
                if _current(change) != change.before:
                    raise ToolExecutionError(f"file changed during patch: {change.path}")
                if change.after is None:
                    change.path.unlink()
                else:
                    atomic_write_text(change.path, change.after, permissions=change.mode)
                applied.append(change)
        except Exception as exc:
            failed = []
            for change in reversed(applied):
                try:
                    if _current(change) != change.after:
                        raise ToolExecutionError("concurrent change")
                    if change.before is None:
                        change.path.unlink()
                    else:
                        atomic_write_text(change.path, change.before, permissions=change.mode)
                except Exception:
                    failed.append(str(change.path))
            raise ToolExecutionError(f"patch failed: {exc}; rollback incomplete: {failed}" if failed
                                     else f"patch failed; completed writes rolled back: {exc}") from exc
    ended = time.perf_counter()
    summary = [f"Applied patch: {len(changes)} file operations; preflight {(prepared-started)*1000:.1f}ms; write {(ended-prepared)*1000:.1f}ms"]
    summary += [("D" if c.after is None else "A" if c.before is None else "M") + " " + str(c.path) for c in changes]
    return "\n".join(summary)


APPLY_PATCH = Tool(
    "apply_patch", "一次提交多文件、多处修改。patch 使用 *** Begin Patch / *** End Patch；"
    "内部用 *** Add File: path（每行+）、*** Delete File: path、*** Update File: path；"
    "Update 可跟 *** Move to: path。修改块用 @@ 或 @@ 函数原文作锚点，空格开头为上下文，-删除，+新增。"
    "优先用于批量编辑；上下文必须唯一；保留原文件换行。仅支持工作区内 UTF-8 文件，拒绝符号链接和 Git 元数据。",
    {"type": "object", "properties": {"patch": {"type": "string", "minLength": 1, "maxLength": 2000000}},
     "required": ["patch"], "additionalProperties": False},
    apply_patch, requires_approval=True, risk="write", target_type="filesystem", scope="workspace",
    capabilities=frozenset({"filesystem_write"}),
    audit_redactor=lambda args: {"files": [op.path for op in parse_patch(args["patch"])], "patch_chars": len(args["patch"])},
)


PATCH_GUIDANCE = """【批量文件编辑】
提供 apply_patch 时，多文件或同文件多处修改优先用它，一次提交完整补丁以减少模型往返。
patch 示例：*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch。
Update 可用多个 @@ 修改块，新增用 Add File，删除用 Delete File，移动用 Move to。
上下文必须唯一；多处命中时增加上下文或使用 @@ 函数原文锚点。不要把补丁作为普通回复假装执行。
"""
