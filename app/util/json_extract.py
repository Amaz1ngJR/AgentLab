"""从模型输出里抠 JSON 对象 —— 唯一实现。

为什么独立成模块：
  同一件事（容忍 markdown 围栏、前后散文、嵌套对象、被截断）原先在
  planner 和 context_compaction 里各写了一份，而且两份行为并不一致：

      '{"a": {"b": 1}} 尾巴 {"c": 2}'
        planner 版          -> None            （非贪婪 `\\{.*?\\}` 停在首个内层 '}'）
        compaction 版       -> {'a': {'b': 1}} （括号配平扫描，正确）

  planner 拿不到计划就会触发重试，属于实打实的行为缺陷。这里统一采用配平扫描
  版本，口径固定在一处，后续再改只需改这里。

只处理 dict：调用方要的都是"一个对象"，返回 list/标量反而会让上层写更多防御分支。
"""
from __future__ import annotations

import json
import re
from typing import Optional

# 围栏只在开头匹配一次；分组留给调用方自行取用，这里只需要起点。
_FENCE_RE = re.compile(r"```(?:json)?\s*")


def find_balanced_json(text: str, start: int) -> Optional[str]:
    """从 text[start] 处的 '{' 起做括号配平扫描，返回完整的对象子串。

    正确处理嵌套对象/数组，并跳过字符串字面量里的花括号（含反斜杠转义）。
    配不平（花括号未闭合）返回 None。这是比 `\\{.*?\\}` 更可靠的抠取方式 ——
    非贪婪会停在第一个 '}'，贪婪又会多吞，只有配平能精确切出整个对象。
    """
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json_object(text: str) -> Optional[dict]:
    """从模型输出抠出第一个 JSON 对象；抠不出合法 dict 时返回 None。"""
    if not text:
        return None
    # 优先在围栏内找：围栏存在时 JSON 通常完整落在里面。
    fenced = _FENCE_RE.search(text)
    search_from = fenced.end() if fenced else 0
    start = text.find("{", search_from)
    if start == -1 and search_from:
        start = text.find("{")  # 围栏后没找到，退回全文找
    if start == -1:
        return None

    candidate = find_balanced_json(text, start)
    if candidate is None:
        # 配平失败（JSON 被截断等）：退回"首 { 到末 }"的粗略切法兜底。
        end = text.rfind("}")
        if end <= start:
            return None
        candidate = text[start : end + 1]

    try:
        obj = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None
