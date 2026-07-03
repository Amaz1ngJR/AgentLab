"""内部统一数据协议 —— 所有 provider adapter 向 Runtime 输出相同的结构。

按 technical_architecture.md §7.1 定义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# 流式回调签名
ProgressCallback = Callable[[dict[str, int]], None]
"""token 进度回调，参数 {"input_tokens": int, "output_tokens": int}"""

TextDeltaCallback = Callable[[str], None]
"""文本增量回调，参数为本次新增的文本片段"""

ThinkingDeltaCallback = Callable[[str], None]
"""思考(推理)增量回调，参数为本次新增的思考文本片段。

深度思考模型(如 Qwen3 系列、DeepSeek-R1)在正式作答前会先输出一段
推理过程(OpenAI 兼容协议里走 delta.reasoning_content)。这段内容只用于
实时展示,不进入对话历史(provider_payload),也不算最终答案。
"""


@dataclass
class ToolCall:
    """模型请求执行的单个工具调用。

    id        - provider 分配的唯一 ID，把工具结果喂回模型时需要对应
    name      - 工具名称，例如 "read_file"
    arguments - 工具参数字典，例如 {"path": "README.md"}
    """
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """单个工具调用执行后的结果，由 Runtime 收集后交给 adapter 格式化回传。

    tool_call_id - 对应 ToolCall.id
    output       - 工具的输出字符串（已被审批 / 已执行）
    is_error     - 工具是否出错（异常文本作为 output 时设为 True）
    """
    tool_call_id: str
    output: str
    is_error: bool = False


@dataclass
class ModelResponse:
    """adapter 向 Runtime 返回的统一响应结构。

    text             - 模型输出的纯文本（可能为空，例如本轮只返回了工具调用）
    reasoning        - 深度思考模型的推理过程文本（仅用于展示/记录，不回放进
                       对话历史，也不是最终答案）；非思考模型恒为空字符串
    tool_calls       - 模型请求执行的工具列表；为空表示模型已给出最终答案
    usage            - token 用量 {"input_tokens": int, "output_tokens": int}
    provider_payload - provider 原生 content blocks（dict 列表），
                       必须原样追加到 messages 历史再发下一轮请求
    finish_reason    - provider 原生停止原因，例如 "end_turn" / "tool_use" / "stop"
    actual_model     - API 响应中真实使用的模型 ID。
                       很多代理会"静默映射":请求 claude-opus-4-9 实际跑的可能是
                       claude-3-5-sonnet。把这个值露出来,让用户能识别这种情况,
                       不至于以为自己用上了某个不存在的型号。
                       None 表示 provider 没返回该字段。
    """
    text: str
    tool_calls: list[ToolCall]
    usage: dict[str, int]
    provider_payload: Any
    finish_reason: str | None = None
    actual_model: str | None = None
    reasoning: str = ""
