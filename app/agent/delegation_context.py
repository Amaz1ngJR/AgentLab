"""Delegation briefs retain complete sections, not arbitrary character prefixes."""
from __future__ import annotations

import json
import re

from app.util.redact import redact

OMISSION = "[交接资料有省略；请向父级索取缺失证据，不要将省略视为已验证。]"
CONTEXT_LIMIT = 20_000

# These are recommendations for the existing free-text context field, not a new protocol.
CONTEXT_GUIDANCE = (
    "按段提供已确认事实及来源、待验证假设、代码定位（路径/符号/必要原文）、"
    "已完成工作、本次边界和交付要求；目标放在 instruction。"
    "优先传必要证据，不重复粘贴全部历史。"
)
CHILD_GUIDANCE = (
    "优先利用交接中的已确认事实、代码定位和已完成工作，避免无理由重复调查；"
    "假设不等于事实，证据矛盾或文件变化时应重新核实。"
    "缺少必要证据或工具时明确报告阻塞，不得声称已验证。"
)

_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*)?(目标|已确认事实|待验证假设|代码定位|已完成工作|本次边界|交付要求)\s*[:：]"
)
_PRIORITY = {"目标": 0, "本次边界": 0, "交付要求": 0, "代码定位": 1,
             "已确认事实": 2, "待验证假设": 2, "已完成工作": 3}


def _blocks(text: str) -> list[str]:
    """Keep JSON documents and fenced blocks atomic; recognize brief section headings."""
    try:
        json.loads(text)
    except (ValueError, TypeError):
        pass
    else:
        return [text]
    blocks, current = [], []
    fence = None
    section = False
    json_end = 0
    offset = 0
    decoder = json.JSONDecoder()
    for line in text.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        line = line.rstrip("\r\n")
        stripped = line.lstrip()
        # JSON embedded in prose may contain blank lines. Keep its full range atomic.
        if fence is None and line_start >= json_end and stripped.startswith(("{", "[")):
            start = line_start + len(line) - len(stripped)
            try:
                _, json_end = decoder.raw_decode(text, start)
            except ValueError:
                json_end = 0
        if line_start < json_end:
            current.append(line)
            continue
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if fence is None and marker:
            fence = marker.group(1)
        elif fence is not None and re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", stripped):
            fence = None
        elif fence is None:
            heading = _HEADING.match(line)
            if heading or (not stripped and not section):
                if current:
                    blocks.append("\n".join(current).strip())
                    current = []
                section = bool(heading)
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def bounded_brief(text: str, limit: int) -> tuple[str, bool]:
    """Return complete blocks in original order, prioritizing boundaries and evidence locations.

    An oversized indivisible block is omitted, never disguised as a complete JSON/code snippet.
    Small limits are a caller error: there must always be room for the omission notice.
    """
    if limit < len(OMISSION):
        raise ValueError("brief limit must fit the omission notice")
    text = redact(text).strip()
    if len(text) <= limit:
        return text, False
    blocks = _blocks(text)

    def priority(index):
        heading = _HEADING.match(blocks[index])
        return _PRIORITY.get(heading.group(1), 4) if heading else 4

    remaining = limit - len(OMISSION)
    selected = set()
    for index in sorted(range(len(blocks)), key=priority):
        size = len(blocks[index]) + 2  # separator before the final notice, too
        if size <= remaining:
            selected.add(index)
            remaining -= size
    return "\n\n".join([block for i, block in enumerate(blocks) if i in selected] + [OMISSION]), True


def build_handoff(parent: str, context: str, snapshot: str = "", *,
                  snapshot_truncated: bool = False) -> tuple[str, dict]:
    """Prefer explicit task evidence over incidental parent context; no model call required."""
    parts = []
    truncated = snapshot_truncated
    for label, text, limit in (("本次任务交接", context, 10_000), ("父级背景", parent, 5_000)):
        if text.strip():
            brief, clipped = bounded_brief(text, limit)
            parts.append(f"【{label}】\n{brief}")
            truncated |= clipped
    # The legacy history snapshot is already normalized, redacted and bounded by its caller.
    if snapshot:
        parts.append("【历史文本快照（非原生前缀）】\n" + snapshot)
    rendered = "\n\n".join(parts)
    return rendered, {
        "mode": "snapshot" if snapshot else "brief",
        "input_chars": len(parent) + len(context) + len(snapshot),
        "output_chars": len(rendered),
        "truncated": truncated,
    }
