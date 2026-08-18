"""敏感信息脱敏 —— 用于 traceback / 错误日志输出前过滤凭据。

应用场景:
  1. CLI 兜底 except 把异常信息或 traceback 打印到 stderr 之前调 redact()
  2. 把工具输出 / 错误反馈给模型之前先 redact(),防止模型把 token 又复述回来
  3. 后续 SQLite 审计写入前调 redact()

原则:
  - 只脱敏值,保留字段名,方便排查"哪种凭据泄漏了"
  - 不破坏正常文本(尽量精确匹配,而不是宽泛遮蔽)
"""
from __future__ import annotations

import re
import traceback as _tb
from typing import Any, Iterable

# 各 provider 凭据的字面量 pattern。顺序很重要:
#   1. Bearer header 先跑,避免后续 token 字面量替换之后, "Bearer ***" 中的
#      "***" 长度不足以再触发 Bearer pattern 的匹配
#   2. token 字面量替换
#   3. 通用字段=值 pattern,但不包含 "authorization"(由 Bearer pattern 负责),
#      并用 \b 边界避免匹配 OPENAI_API_KEY 这类单词中间
_TOKEN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # HTTP Authorization: Bearer xxxx
    (re.compile(r"(Bearer\s+)([A-Za-z0-9._\-]{4,})"), r"\1***"),
    # Anthropic 官方 API key: sk-ant-xxxx
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-***"),
    # OpenAI API key: sk-xxxx (至少 20 位避免误伤)
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk-***"),
    # 自建代理 / Claude Code 网关 token: cr_xxxx
    (re.compile(r"cr_[A-Za-z0-9]{20,}"), "cr_***"),
    # 通用 header / kwarg 形式: x-api-key: xxx 或 api_key=xxx
    # 不含 authorization(由 Bearer pattern 负责),用 \b 防止匹配单词内部
    (re.compile(
        r"(\b(?:x-api-key|api[_-]?key|auth[_-]?token)"
        r"\s*[:=]\s*['\"]?)([^\s'\",;}]+)",
        re.IGNORECASE,
    ), r"\1***"),
]


def redact(text: str) -> str:
    """对单个字符串脱敏。多次匹配会全部替换。"""
    if not text:
        return text
    out = text
    for pattern, repl in _TOKEN_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_value(value: Any) -> Any:
    """递归脱敏 JSON 兼容值，保持容器结构和转义边界不变。"""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def redact_lines(lines: Iterable[str]) -> list[str]:
    """对一组字符串逐行脱敏。"""
    return [redact(line) for line in lines]


def format_exception(exc: BaseException) -> str:
    """格式化异常摘要(单行,适合 CLI 提示):'TypeName: message'。已脱敏。"""
    return redact(f"{type(exc).__name__}: {exc}")


def format_traceback(exc: BaseException) -> str:
    """格式化完整 traceback(多行,适合调试日志)。已脱敏。"""
    raw = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    return redact(raw)
