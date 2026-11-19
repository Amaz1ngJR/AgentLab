"""工具审计摘要的统一构造 —— 只保留可回放的少量字段，避免把正文写进审计库。

三个内置工具（code_search / web_search / web_fetch）原先各写了一遍同样的兜底：

    try:
        body = json.loads(result)
        result_summary = json.dumps({...挑几个字段...}, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError, AttributeError):
        result_summary = result          # 解析不了就回落原文

逻辑一模一样，只有"挑哪些字段"不同。重复带来的实际代价是：兜底口径不一致时
很难发现。比如某一处漏了 AttributeError，工具返回非 dict 时就会直接抛异常，
把审计链路拖垮。这里把 except 元组固定在一处。
"""
from __future__ import annotations

import json
from typing import Any, Callable


def audit_summary(
    args: dict,
    result: str,
    *,
    keys: tuple[str, ...],
    projector: Callable[[dict], dict[str, Any]],
) -> tuple[str, str]:
    """返回 ``(脱敏后的参数 JSON, 结果摘要)``。

    args      - 工具入参，只保留 keys 里出现的字段（避免把文件正文等大字段落库）
    result    - 工具返回的原始 JSON 文本
    projector - 从解析后的结果体里挑出要留存的字段

    result 不是合法 JSON、不是对象、或字段类型不符合预期时，摘要在原样返回
    result —— 审计宁可留下偏大的原文，也不能因为摘要失败而丢掉整条记录。
    """
    safe_args = {key: args.get(key) for key in keys if key in args}
    try:
        body = json.loads(result)
        result_summary = json.dumps(projector(body), ensure_ascii=False)
    except (json.JSONDecodeError, TypeError, AttributeError):
        result_summary = result
    return json.dumps(safe_args, ensure_ascii=False), result_summary
