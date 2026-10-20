"""SubagentRuntimeFactory integration with the executor and restricted tools."""
import json
from pathlib import Path

import pytest

from app.agent.approval import AutoApprove
from app.agent.cancel import CancelToken, Cancelled
from app.agent.subagent_runtime import SubagentRuntimeFactory
from app.agent.subagents import SubagentSpec
from app.models.protocol import ModelResponse, ToolCall
from tests.support.delegation import FakeRouter, registry, response


def test_runtime_factory_uses_independent_child_runtime(tmp_path: Path):
    router = FakeRouter([
        response(json.dumps({"tasks": [{"id": "t1", "content": "读取文件"}]})),
        ModelResponse("", [ToolCall("c1", "read_file", {})],
                      {"input_tokens": 1, "output_tokens": 1}, []),
        response("child completed"),
    ])
    factory = SubagentRuntimeFactory(
        llm=router,
        tools=registry(),
        approval=AutoApprove(),
        parent_system="parent system",
        max_steps=3,
    )
    result = factory.delegate(
        SubagentSpec("child-1", "执行只读任务", role="research", read_only=True,
                     tools=("read_file",), max_steps=3),
        tmp_path,
        CancelToken(),
    )
    assert result.status == "succeeded"
    assert result.output
    assert "child-1" not in result.output
    assert router.calls


def test_factory_propagates_child_failure():
    router = FakeRouter([
        response(json.dumps({"tasks": [{"id": "t1", "content": "执行任务"}]})),
        ModelResponse("", [ToolCall("c1", "read_file", {})], {"input_tokens": 1, "output_tokens": 1}, []),
    ])
    factory = SubagentRuntimeFactory(
        llm=router, tools=registry(), approval=AutoApprove(), max_steps=1,
    )
    result = factory.delegate(
        SubagentSpec("child-fail", "执行任务", role="executor", tools=("read_file",), max_steps=1),
        Path("."), CancelToken(),
    )
    assert result.status == "budget_exhausted"
    assert result.error


def test_factory_propagates_cancel():
    token = CancelToken()
    token.cancel()
    factory = SubagentRuntimeFactory(llm=FakeRouter([]), tools=registry())
    with pytest.raises(Cancelled):
        factory.delegate(SubagentSpec("cancel", "任务", role="research", read_only=True), Path("."), token)
