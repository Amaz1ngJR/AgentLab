"""Deterministic model and delegation helpers shared by both test layers."""
import copy

from app.agent.agent_manager import AgentManager
from app.agent.approval import AutoApprove
from app.agent.cancel import CancelToken
from app.models.protocol import ModelResponse
from app.tools.registry import Tool, ToolRegistry


class FakeRouter:
    def __init__(self, responses: list[ModelResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create_message(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if self.responses:
            return self.responses.pop(0)
        return ModelResponse("done", [], {"input_tokens": 1, "output_tokens": 1}, [])

    @staticmethod
    def format_tool_results(results):
        return [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": result.tool_call_id,
                "content": result.output,
                "is_error": result.is_error,
            } for result in results],
        }]


class SnapshotRouter(FakeRouter):
    def create_message(self, messages, **kwargs):
        return super().create_message(copy.deepcopy(messages), **kwargs)


class ForkRouter(FakeRouter):
    def fork(self):
        return FakeRouter([response("CHILD_FINDING")])


def response(text: str) -> ModelResponse:
    return ModelResponse(text, [], {"input_tokens": 1, "output_tokens": 1}, [])


def registry() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(Tool("read_file", "read", {}, lambda args: "read"))
    tools.register(Tool("edit_file", "edit", {}, lambda args: "edited", requires_approval=False))
    tools.register(Tool("shell", "shell", {}, lambda args: "ran", requires_approval=False))
    return tools


def manager(tmp_path, **kwargs):
    return AgentManager(llm=ForkRouter([]), tools=registry(), approval=AutoApprove(),
                        workspace=tmp_path, cancel=CancelToken(), allowed=True, **kwargs)
