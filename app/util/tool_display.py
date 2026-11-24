"""Compact terminal rendering only; never modifies model input or tool history."""
from __future__ import annotations

import json
import re

from prompt_toolkit.utils import get_cwidth
from app.util.redact import redact

_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")


def _clean(text):
    text = _ANSI.sub("", str(text)).replace("\r\n", "\n").replace("\r", "\n")
    return redact("".join(c for c in text if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)).expandtabs(4)


def _clip(text, width):
    if get_cwidth(text) <= width:
        return text
    result, used = [], 0
    for char in text:
        used += max(0, get_cwidth(char))
        if used > width - 1:
            break
        result.append(char)
    return "".join(result) + "…"


def action_label(name, args):
    """Describe the invocation, not its first output line."""
    args = args or {}
    if name in {"shell", "bash", "terminal_open"}:
        label = "Bash" if name != "terminal_open" else "TerminalOpen"
        return f"{label}({args.get('command', '')})"
    if name == "terminal_send":
        return f"Terminal[{args.get('session_id', '?')}]({args.get('input', '')})"
    if name == "exec_command":
        return f"Exec({args.get('command', '')})"
    if name == "write_stdin":
        return f"Terminal[{args.get('session_id', '?')}]"
    if name == "terminal_close":
        return f"TerminalClose({args.get('session_id', '?')})"
    if name == "read_files":
        requests = args.get("requests", [])
        paths = [str(item.get("path", "?")) for item in requests if isinstance(item, dict)]
        return f"ReadFiles({', '.join(paths)})"
    if name in {"read_file", "write_file", "edit_file", "edit_file_outside_workspace", "list_dir"}:
        label = {"read_file": "Read", "write_file": "Write", "list_dir": "List"}.get(name, "Edit")
        target = args.get("path", ".")
        if name == "read_file" and args.get("offset"):
            target = f"{target}, offset={args['offset']}"
        return f"{label}({target})"
    # Avoid rendering file contents, replacement strings, or large tool schemas.
    target = next((args[key] for key in ("command", "query", "pattern", "url", "path", "session_id") if args.get(key)), "")
    return f"{name}({target})"


def _result_text(name, output):
    if name in {"exec_command", "write_stdin"}:
        try:
            data = json.loads(output)
            if data["status"] == "running":
                status = f"仍在运行 · {data['session_id']}"
            else:
                status = f"已退出 · exit_code={data['exit_code']}"
            return "\n".join(part for part in (data["output"], status) if part)
        except (ValueError, KeyError, TypeError):
            return output
    if name == "read_files":
        try:
            data = json.loads(output)
            files = data["files"]
            if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
                return output
            parts = []
            for entry in files:
                span = f" ({entry['start_line']}-{entry.get('end_line', '?')})" if "start_line" in entry else ""
                parts.append(str(entry.get("path", "?")) + span)
                parts.append(str(entry.get("error") or entry.get("content") or entry.get("note") or "(无内容)"))
                if entry.get("truncated"):
                    parts.append("… 文件内容已分页")
            if data.get("pending_indices"):
                parts.append(f"尚未读取 {len(data['pending_indices'])} 个请求")
            return "\n".join(parts)
        except (ValueError, KeyError, TypeError):
            pass
    return output


def activity_label(name, args, width=90):
    return _clip(_clean(action_label(name, args)).replace("\n", " ↵ "), max(24, width))


def render_tool_result(name, args, output, *, error=False, elapsed=0, width=100):
    width = max(24, width)
    duration = f"{elapsed * 1000:.0f}ms" if elapsed < 1 else f"{elapsed:.1f}s"
    title = ("ERR · " if error else "") + _clean(action_label(name, args)).replace("\n", " ↵ ")
    # Keep commands readable across terminal lines, with a bounded header for scripts.
    header, current, used = [], "", 0
    for char in f"{title} · {duration}":
        size = max(0, get_cwidth(char))
        if used + size > width - 4:
            header.append(current)
            current, used = "", 0
        current += char
        used += size
    header.append(current)
    if len(header) > 2:
        header = header[:2]
        suffix = f" · {duration}"
        header[-1] = _clip(header[-1] + "…", width - 4 - get_cwidth(suffix)) + suffix
    rendered = [("  ● " if index == 0 else "    ") + line for index, line in enumerate(header)]
    lines = _clean(_result_text(name, output or "")).splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    lines = lines or ["(无输出)"]
    for index, line in enumerate(lines[:4]):
        suffix = f" … +{len(lines) - 4} 行" if index == 3 and len(lines) > 4 else ""
        rendered.append(("    ⎿ " if index == 0 else "      ") +
                        _clip(line, width - 6 - get_cwidth(suffix)) + suffix)
    return "\n".join(rendered)
