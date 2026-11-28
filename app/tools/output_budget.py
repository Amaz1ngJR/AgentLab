"""Bounds for text and structured tool results stored in model history."""

from app.agent.context_budget import estimate_tokens

TOOL_OUTPUT_MAX_CHARS = 10_000
TOOL_OUTPUT_MAX_TOKENS = 3_200
TOOL_PAGE_MAX_TOKENS = TOOL_OUTPUT_MAX_TOKENS - 300
OUTPUT_TOKEN_LIMIT_SCHEMA = {"type": "integer", "minimum": 128, "maximum": TOOL_OUTPUT_MAX_TOKENS,
                             "description": "本次输出的估算 token 预算。状态和续读提示单独保留。"}


def fits_output(text: str, max_chars: int = TOOL_OUTPUT_MAX_CHARS) -> bool:
    return len(text) <= max_chars and estimate_tokens(text) <= TOOL_OUTPUT_MAX_TOKENS


def fit_prefix(text: str, fits) -> str:
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if fits(text[:mid]):
            low = mid
        else:
            high = mid - 1
    return text[:low]


def truncate_tool_output(output: str, max_chars: int = TOOL_OUTPUT_MAX_CHARS, *, reference: str = "",
                         max_tokens: int = TOOL_OUTPUT_MAX_TOKENS) -> str:
    if not output or (len(output) <= max_chars and estimate_tokens(output) <= max_tokens):
        return output
    hint = reference or "如需完整内容请用更精确的参数重新调用"
    notice = f"\n\n[工具输出超过历史预算，已省略中间部分（共 {len(output)} 字符）；{hint}]\n\n"
    if len(notice) > max_chars or estimate_tokens(notice) > max_tokens:
        return fit_prefix(notice[:max_chars], lambda text: estimate_tokens(text) <= max_tokens)
    low, high = 0, min(len(output), max_chars - len(notice))
    while low < high:
        mid = (low + high + 1) // 2
        tail = mid // 5
        preview = output[:mid - tail] + notice + (output[-tail:] if tail else "")
        if estimate_tokens(preview) <= max_tokens:
            low = mid
        else:
            high = mid - 1
    tail = low // 5
    return output[:low - tail] + notice + (output[-tail:] if tail else "")
