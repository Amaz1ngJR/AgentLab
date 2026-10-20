"""Protocol-shaped model responses for runtime and persistence tests."""
from app.models.protocol import ModelResponse, ToolCall, ToolResult
from app.tools.registry import Tool, ToolRegistry


class FakeRouter:
    """按预设序列返回 ModelResponse,记录每次收到的 messages。"""

    def __init__(self, responses: list[ModelResponse]):
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def provider(self) -> str:
        return "fake"

    def create_message(
        self,
        messages,
        tools=None,
        system=None,
        temperature=None,
        max_tokens=4096,
        on_progress=None,
        on_text_delta=None,
        on_thinking_delta=None,
    ) -> ModelResponse:
        # 拷贝 messages 快照,避免被后续修改污染
        self.calls.append([dict(m) for m in messages])
        if not self._responses:
            return ModelResponse(text="(no more responses)", tool_calls=[],
                                 usage={"input_tokens": 0, "output_tokens": 0},
                                 provider_payload=[])
        return self._responses.pop(0)

    @staticmethod
    def format_tool_results(results: list[ToolResult]) -> list[dict]:
        """测试用的格式:Anthropic 风格,把多个 tool_result 包在一条 user 消息里。

        生产代码三家 adapter 各自实现自己的格式,Runtime 只关心拿到 list[dict]
        然后 messages.extend 即可。
        """
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": r.tool_call_id,
                "content": r.output,
                "is_error": r.is_error,
            }
            for r in results
        ]
        return [{"role": "user", "content": blocks}]


def _resp_text(text: str, in_tokens: int = 5, out_tokens: int = 3) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=[],
        usage={"input_tokens": in_tokens, "output_tokens": out_tokens},
        provider_payload=[{"type": "text", "text": text}],
    )


def _resp_tool(tool_id: str, name: str, args: dict) -> ModelResponse:
    return ModelResponse(
        text="",
        tool_calls=[ToolCall(id=tool_id, name=name, arguments=args)],
        usage={"input_tokens": 5, "output_tokens": 1},
        provider_payload=[{"type": "tool_use", "id": tool_id, "name": name, "input": args}],
    )


def _registry_with(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _echo_tool() -> Tool:
    return Tool(
        name="echo",
        description="echo back",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
        executor=lambda args: f"echo: {args.get('msg', '')}",
    )
