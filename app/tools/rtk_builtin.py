"""AgentLab 内置的 RTK 风格 Shell 输出压缩引擎。

设计灵感来自开源项目 RTK (Rust Token Killer, Apache-2.0)：
https://github.com/rtk-ai/rtk

本模块是面向 AgentLab Python Runtime 的独立实现，不依赖外部 ``rtk`` 二进制。
它在命令执行完成后按命令类型过滤 stdout/stderr，同时保留退出码；解析失败或输出
不适合压缩时原样回退，避免因节省 token 丢失关键错误。
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from typing import Callable, Mapping

RTK_ENV_ENABLED = "AGENTLAB_RTK_ENABLED"
RTK_ENV_ULTRA_COMPACT = "AGENTLAB_RTK_ULTRA_COMPACT"
_MAX_LINE_CHARS = 500
_MAX_FAILURE_LINES = 160
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class BuiltinRTKConfig:
    enabled: bool = True
    ultra_compact: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BuiltinRTKConfig":
        source = env if env is not None else os.environ
        return cls(
            enabled=_as_bool(source.get(RTK_ENV_ENABLED), True),
            ultra_compact=_as_bool(source.get(RTK_ENV_ULTRA_COMPACT), False),
        )


@dataclass(frozen=True)
class CompressionResult:
    output: str
    applied: bool
    category: str
    original_bytes: int
    output_bytes: int
    savings_percent: float
    reason: str = ""

    @property
    def saved_bytes(self) -> int:
        return max(0, self.original_bytes - self.output_bytes)


class BuiltinRTK:
    """对已完成命令的输出执行命令感知压缩。"""

    def __init__(self, config: BuiltinRTKConfig | None = None):
        self.config = config or BuiltinRTKConfig.from_env()

    def compress(
        self,
        command: str,
        stdout: str,
        stderr: str,
        returncode: int,
    ) -> CompressionResult:
        raw = _join_streams(stdout, stderr)
        original_bytes = len(raw.encode("utf-8", errors="replace"))
        if not self.config.enabled or not raw.strip():
            return _result(raw, False, "passthrough", original_bytes, "disabled_or_empty")

        category = classify_command(command)
        formatter = _FILTERS.get(category)
        if formatter is None:
            # 未识别命令只做无损 ANSI/进度清理；收益不足时仍返回原始输出。
            filtered = _clean_common(raw)
            reason = "generic_cleanup"
        else:
            try:
                filtered = formatter(stdout, stderr, returncode, self.config.ultra_compact)
                reason = "filtered"
            except Exception:
                return _result(raw, False, category, original_bytes, "filter_error")

        filtered = filtered.strip()
        if not filtered:
            # 成功且过滤器把噪声全部移除时，给模型一个明确结果。
            filtered = "ok" if returncode == 0 else _clean_common(raw).strip()
        filtered_bytes = len(filtered.encode("utf-8", errors="replace"))
        # 错误命令优先完整保留；只有专用过滤器确实压缩且保留失败信息才应用。
        minimum_saving = 0.05 if returncode != 0 and formatter else 0.10
        if filtered_bytes >= original_bytes * (1 - minimum_saving):
            return _result(raw, False, category, original_bytes, "insufficient_savings")
        return _result(filtered, True, category, original_bytes, reason)


def classify_command(command: str) -> str:
    """保守识别单命令/常见复合命令；无法确认时返回 generic。"""
    lowered = command.strip().lower()
    if not lowered:
        return "generic"
    # 复合命令优先识别测试类别；其他复杂管道用 generic，避免改变语义认知。
    if _contains_any(lowered, ("pytest", "python -m pytest")):
        return "pytest"
    if _contains_any(lowered, ("cargo test", "npm test", "npm run test", "pnpm test", "yarn test", "vitest", "jest", "go test")):
        return "test"
    if re.search(r"(^|[;&|]\s*)git\s+status\b", lowered):
        return "git_status"
    if re.search(r"(^|[;&|]\s*)git\s+diff\b", lowered):
        return "git_diff"
    if re.search(r"(^|[;&|]\s*)git\s+log\b", lowered):
        return "git_log"
    if re.search(r"(^|[;&|]\s*)(rg|grep)\b", lowered):
        return "grep"
    if re.search(r"(^|[;&|]\s*)(ls|tree|find)\b", lowered):
        return "listing"
    if re.search(r"(^|[;&|]\s*)(cat|head|tail)\b", lowered):
        return "read"
    if re.search(r"(^|[;&|]\s*)(docker|kubectl|oc)\b", lowered):
        return "table"
    if re.search(r"(^|[;&|]\s*)(ruff|mypy|eslint|tsc|golangci-lint)\b", lowered):
        return "diagnostics"
    return "generic"


def format_status(config: BuiltinRTKConfig | None = None) -> str:
    cfg = config or BuiltinRTKConfig.from_env()
    return "\n".join([
        "RTK: built-in (no external binary required)",
        f"enabled: {str(cfg.enabled).lower()}",
        f"ultra_compact: {str(cfg.ultra_compact).lower()}",
        "filters: git, pytest/tests, grep, listing/read, diagnostics, docker/kubernetes",
        f"toggle: {RTK_ENV_ENABLED}=true|false (restart AgentLab)",
    ])


def _filter_git_status(stdout: str, stderr: str, returncode: int, ultra: bool) -> str:
    clean = _clean_common(_join_streams(stdout, stderr), preserve_leading=True)
    if returncode != 0:
        return clean
    lines = [line for line in clean.splitlines() if line.strip()]
    porcelain = [line for line in lines if re.match(r"^[ MARCUD?!]{2}\s", line)]
    if porcelain:
        groups: dict[str, list[str]] = {"staged": [], "modified": [], "untracked": [], "deleted": []}
        for line in porcelain:
            code, path = line[:2], line[3:].strip()
            if code == "??":
                groups["untracked"].append(path)
            elif "D" in code:
                groups["deleted"].append(path)
            elif code[0] not in (" ", "?"):
                groups["staged"].append(path)
            else:
                groups["modified"].append(path)
        return _render_groups(groups, ultra)

    branch = next((line for line in lines if line.startswith(("On branch ", "## "))), "")
    interesting = [
        line.strip() for line in lines
        if re.match(r"^(modified:|new file:|deleted:|renamed:|\?\?)", line.strip())
    ]
    summary = []
    if branch:
        summary.append(branch.replace("On branch ", "branch: "))
    summary.extend(_dedupe(interesting))
    if not summary and "working tree clean" in clean.lower():
        return "working tree clean"
    return "\n".join(summary) or clean


def _filter_git_diff(stdout: str, stderr: str, returncode: int, ultra: bool) -> str:
    clean = _clean_common(_join_streams(stdout, stderr))
    lines = clean.splitlines()
    kept: list[str] = []
    for line in lines:
        if line.startswith(("diff --git ", "@@", "+", "-")) and not line.startswith(("+++", "---")):
            kept.append(_clip_line(line))
        elif line.startswith(("Binary files ", "rename from ", "rename to ")):
            kept.append(line)
    return "\n".join(_collapse_repeated(kept, ultra)) or clean


def _filter_git_log(stdout: str, stderr: str, returncode: int, ultra: bool) -> str:
    clean = _clean_common(_join_streams(stdout, stderr))
    out = []
    current = ""
    for line in clean.splitlines():
        if line.startswith("commit "):
            current = line.split()[1][:12]
        elif line.startswith("Author:"):
            continue
        elif line.startswith("Date:"):
            continue
        elif line.strip() and current:
            out.append(f"{current} {line.strip()}")
            current = ""
        elif re.match(r"^[0-9a-f]{7,40}\s", line):
            out.append(_clip_line(line))
    return "\n".join(out) or clean


def _filter_tests(stdout: str, stderr: str, returncode: int, ultra: bool) -> str:
    clean = _clean_common(_join_streams(stdout, stderr))
    lines = clean.splitlines()
    summary_patterns = (
        r"=+ .* (passed|failed|error|skipped|warnings?).*=+",
        r"\b(test result:|tests?:).*",
        r"\b(PASS|FAIL)\b.*",
        r"\b\d+ passed\b.*",
    )
    if returncode == 0:
        summaries = [
            line.strip() for line in lines
            if any(re.search(pattern, line, re.IGNORECASE) for pattern in summary_patterns)
        ]
        return "\n".join(_dedupe(summaries[-8:])) or "tests passed"

    markers = ("FAIL", "ERROR", "FAILED", "E   ", "AssertionError", "Traceback", "panic", "Caused by:")
    selected: list[str] = []
    for i, line in enumerate(lines):
        if any(marker.lower() in line.lower() for marker in markers):
            start, end = max(0, i - 2), min(len(lines), i + 8)
            selected.extend(lines[start:end])
    selected.extend(lines[-12:])
    return "\n".join(_dedupe(_clip_line(line) for line in selected)[-_MAX_FAILURE_LINES:]) or clean


def _filter_grep(stdout: str, stderr: str, returncode: int, ultra: bool) -> str:
    clean = _clean_common(_join_streams(stdout, stderr))
    groups: dict[str, list[str]] = {}
    ungrouped: list[str] = []
    for line in clean.splitlines():
        match = re.match(r"^([^:\n]+):(\d+)(?::\d+)?:?(.*)$", line)
        if match:
            groups.setdefault(match.group(1), []).append(
                f"{match.group(2)}:{_clip_line(match.group(3).strip())}"
            )
        else:
            ungrouped.append(_clip_line(line))
    if not groups:
        return "\n".join(_collapse_repeated(ungrouped, ultra))
    rendered = []
    for path, matches in groups.items():
        rendered.append(f"{path} ({len(matches)})")
        rendered.extend(f"  {item}" for item in matches[:40])
        if len(matches) > 40:
            rendered.append(f"  ... {len(matches) - 40} more")
    return "\n".join(rendered)


def _filter_listing(stdout: str, stderr: str, returncode: int, ultra: bool) -> str:
    clean = _clean_common(_join_streams(stdout, stderr))
    lines = [line.rstrip() for line in clean.splitlines() if line.strip()]
    if len(lines) <= 80:
        return "\n".join(lines)
    head, tail = lines[:55], lines[-10:]
    return "\n".join(head + [f"... {len(lines) - 65} entries omitted ..."] + tail)


def _filter_read(stdout: str, stderr: str, returncode: int, ultra: bool) -> str:
    clean = _clean_common(_join_streams(stdout, stderr))
    lines = clean.splitlines()
    if len(lines) <= 160:
        return clean
    # 保留头尾；Agent 需要完整内容时应使用 read_file 的精确范围，而非 shell cat。
    return "\n".join(lines[:120] + [f"... {len(lines) - 140} lines omitted ..."] + lines[-20:])


def _filter_diagnostics(stdout: str, stderr: str, returncode: int, ultra: bool) -> str:
    clean = _clean_common(_join_streams(stdout, stderr))
    lines = [_clip_line(line) for line in clean.splitlines() if line.strip()]
    if returncode == 0:
        summaries = [line for line in lines if re.search(r"(passed|success|no errors|found 0)", line, re.I)]
        return "\n".join(summaries[-5:]) or "checks passed"
    return "\n".join(_collapse_repeated(lines[-_MAX_FAILURE_LINES:], ultra))


def _filter_table(stdout: str, stderr: str, returncode: int, ultra: bool) -> str:
    clean = _clean_common(_join_streams(stdout, stderr))
    lines = [_clip_line(line) for line in clean.splitlines() if line.strip()]
    if len(lines) <= 80:
        return "\n".join(lines)
    return "\n".join(lines[:1] + lines[1:70] + [f"... {len(lines) - 70} rows omitted ..."])


def _clean_common(text: str, *, preserve_leading: bool = False) -> str:
    text = _ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.splitlines():
        # 去掉常见进度条覆盖行和尾随空白，但保留普通空行分隔。
        stripped = line.rstrip()
        if re.match(r"^\s*\d{1,3}%\s*[|█#=>.-]{3,}", stripped):
            continue
        lines.append(stripped)
    result = "\n".join(lines)
    return result.rstrip() if preserve_leading else result.strip()


def _join_streams(stdout: str, stderr: str) -> str:
    parts = []
    if stdout:
        parts.append(stdout.rstrip())
    if stderr:
        parts.extend(["[stderr]", stderr.rstrip()])
    return "\n".join(parts)


def _render_groups(groups: dict[str, list[str]], ultra: bool) -> str:
    lines = []
    for name in ("staged", "modified", "untracked", "deleted"):
        values = groups.get(name) or []
        if not values:
            continue
        if ultra:
            lines.append(f"{name}:{len(values)} " + " ".join(values[:12]))
        else:
            lines.append(f"{name} ({len(values)})")
            lines.extend(f"  {value}" for value in values[:30])
            if len(values) > 30:
                lines.append(f"  ... {len(values) - 30} more")
    return "\n".join(lines) or "working tree clean"


def _collapse_repeated(lines: list[str], ultra: bool) -> list[str]:
    if not lines:
        return []
    result: list[str] = []
    previous = None
    count = 0
    for line in lines:
        if line == previous:
            count += 1
            continue
        if previous is not None:
            result.append(f"{previous} (x{count})" if count > 1 else previous)
        previous, count = line, 1
    if previous is not None:
        result.append(f"{previous} (x{count})" if count > 1 else previous)
    return result


def _dedupe(lines) -> list[str]:
    result, seen = [], set()
    for line in lines:
        if line not in seen:
            seen.add(line)
            result.append(line)
    return result


def _clip_line(line: str) -> str:
    return line if len(line) <= _MAX_LINE_CHARS else line[:_MAX_LINE_CHARS] + "..."


def _result(output: str, applied: bool, category: str, original_bytes: int, reason: str) -> CompressionResult:
    output_bytes = len(output.encode("utf-8", errors="replace"))
    savings = ((original_bytes - output_bytes) / original_bytes * 100.0) if original_bytes else 0.0
    return CompressionResult(output, applied, category, original_bytes, output_bytes, max(0.0, savings), reason)


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_FILTERS: dict[str, Callable[[str, str, int, bool], str]] = {
    "git_status": _filter_git_status,
    "git_diff": _filter_git_diff,
    "git_log": _filter_git_log,
    "pytest": _filter_tests,
    "test": _filter_tests,
    "grep": _filter_grep,
    "listing": _filter_listing,
    "read": _filter_read,
    "diagnostics": _filter_diagnostics,
    "table": _filter_table,
}
