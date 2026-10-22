import copy

import pytest

from app.tools.registry import ToolExecutionError
from tests.support.delegation import FakeRouter, ForkRouter, manager, response


def test_resume_keeps_history_distinct_results_and_budget(tmp_path):
    calls = []

    class Child(FakeRouter):
        def create_message(self, messages, **kwargs):
            calls.append(copy.deepcopy(messages))
            result = response("FIRST_FINDING" if len(calls) == 1 else "SECOND_FINDING")
            result.provider_payload = [{"role": "assistant", "content": result.text}]
            return result

    class Parent(ForkRouter):
        def fork(self):
            return Child([])

    m = manager(tmp_path)
    m.llm = Parent([])
    try:
        first = m.spawn("调查模块", max_steps=3, max_tool_calls=3)["child_run_id"]
        assert m.wait([first], 2)["agents"][0]["resumable"]
        rounds = m.rounds_left
        second = m.resume(first, "追问模块", max_steps=3, max_tool_calls=3)["child_run_id"]
        assert second != first
        record = m.wait([second], 2)["agents"][0]
        assert record["session_id"] == first
        assert m.snapshot(first)["summary"] == "FIRST_FINDING"
        assert m.snapshot(first)["continuation_id"] == second
        assert "FIRST_FINDING" in str(calls[1]) and "调查模块" in str(calls[1])
        assert "追问模块" in str(calls[1]) and "工作区可能已变化" in str(calls[1])
        assert m.usage == {"input_tokens": 2, "output_tokens": 2}
        assert m.rounds_left == rounds - 1
        with pytest.raises(ToolExecutionError, match="cannot resume"):
            m.resume(first, "重复启动")
        m.close(second)
        with pytest.raises(ToolExecutionError, match="cannot resume"):
            m.resume(second, "已关闭")
    finally:
        m.finish()


def test_restored_results_are_not_resumable(tmp_path):
    m, restored = manager(tmp_path), manager(tmp_path)
    try:
        sid = m.spawn("调查")["child_run_id"]
        m.wait([sid], 2)
        restored.restore_results(m.records())
        assert not restored.snapshot(sid)["resumable"]
        with pytest.raises(ToolExecutionError, match="cannot resume"):
            restored.resume(sid, "继续")
    finally:
        m.finish()
        restored.finish()


def test_native_prefix_final_serialized_request_and_fallback(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import patch
    from app.models.openai_adapter import OpenAIAdapter
    from app.models.router import ModelRouter
    from tests.unit.test_openai_adapter import _cfg, _message_item, _FakeStream

    with patch("openai.OpenAI") as sdk:
        client = sdk.return_value
        final = SimpleNamespace(output=[_message_item("ok")], status="completed", model="gpt-5",
                                usage=SimpleNamespace(input_tokens=20, output_tokens=2))
        client.responses.stream.side_effect = lambda **kw: _FakeStream([], final)
        router = ModelRouter(OpenAIAdapter(_cfg()))
        parent_messages = [{"role": "user", "content": "shared parent evidence"}]
        router.create_message(parent_messages, system="stable system", tools=[])
        parent_request = copy.deepcopy(client.responses.stream.call_args.kwargs)
        m = manager(tmp_path)
        m.llm = router
        try:
            sid = m.spawn("检查这个证据", context_mode="full_prefix")["child_run_id"]
            result = m.wait([sid], 2)["agents"][0]
            child_request = client.responses.stream.call_args.kwargs
            assert child_request["input"][:len(parent_request["input"])] == parent_request["input"]
            assert child_request["instructions"] == parent_request["instructions"]
            assert "新创建的子 Agent" in str(child_request["input"][len(parent_request["input"]):])
            assert result["context_diagnostics"]["prefix_scope"] == "messages_and_system"
            assert not result["context_diagnostics"]["tools_match"]
            assert all(t["name"] not in {"shell", "spawn_agent", "resume_agent"} for t in child_request.get("tools", []))
            assert parent_messages == [{"role": "user", "content": "shared parent evidence"}]
            with pytest.raises(ToolExecutionError, match="conflicts"):
                m.spawn("bad", fork_history=True, context_mode="full_prefix")
        finally:
            m.finish()


def test_new_round_delivery_does_not_reuse_old_receipt(tmp_path):
    m = manager(tmp_path)
    try:
        sid = m.spawn("first")["child_run_id"]
        m.wait([sid], 2, consume=False)
        history = []
        m.deliver(history)
        assert m.result_delivered(sid)
        next_id = m.resume(sid, "second")["child_run_id"]
        m.wait([next_id], 2, consume=False)
        assert not m.result_delivered(next_id)
        m.deliver(history)
        assert m.result_delivered(sid) and m.result_delivered(next_id)
    finally:
        m.finish()


@pytest.mark.parametrize("failure", ["thread", "checkpoint", "factory"])
def test_resume_startup_rolls_back_budget_and_session(tmp_path, monkeypatch, failure):
    import app.agent.agent_manager as module
    m = manager(tmp_path)
    try:
        sid = m.spawn("first", max_steps=3, max_tool_calls=3)["child_run_id"]
        m.wait([sid], 2)
        before = (m.rounds_left, m.tools_left)
        def fail(*args, **kwargs):
            raise RuntimeError("startup failed")
        with monkeypatch.context() as patcher:
            if failure == "thread":
                patcher.setattr(module.threading.Thread, "start", fail)
            elif failure == "factory":
                patcher.setattr(module, "SubagentRuntimeFactory", fail)
            else:
                patcher.setattr(m, "_checkpoint", fail)
            with pytest.raises(RuntimeError, match="startup failed"):
                m.resume(sid, "followup", max_steps=3, max_tool_calls=3)
        assert (m.rounds_left, m.tools_left) == before
        assert len(m.children) == 1
        assert m.snapshot(sid)["resumable"]
        assert m.snapshot(sid)["continuation_id"] is None
        next_id = m.resume(sid, "retry", max_steps=3, max_tool_calls=3)["child_run_id"]
        assert m.wait([next_id], 2)["agents"][0]["status"] == "succeeded"
    finally:
        m.finish()


def test_failed_followup_keeps_successful_checkpoint_for_explicit_retry(tmp_path):
    calls = []
    class Child(FakeRouter):
        def create_message(self, messages, **kwargs):
            calls.append(copy.deepcopy(messages))
            if len(calls) == 2:
                raise RuntimeError("temporary provider failure")
            result = response("FIRST" if len(calls) == 1 else "RECOVERED")
            result.provider_payload = [{"role": "assistant", "content": result.text}]
            return result
    class Parent(ForkRouter):
        def fork(self):
            return Child([])
    m = manager(tmp_path)
    m.llm = Parent([])
    try:
        first = m.spawn("first", max_steps=3, max_tool_calls=3)["child_run_id"]
        m.wait([first], 2)
        second = m.resume(first, "FAILURE_DIRECTIVE", max_steps=3, max_tool_calls=3)["child_run_id"]
        failed = m.wait([second], 2)["agents"][0]
        assert failed["status"] == "failed" and failed["resumable"]
        assert any(second in item for item in m.unresolved())
        with pytest.raises(ToolExecutionError, match="cannot resume"):
            m.resume(first, "old checkpoint")
        third = m.resume(second, "recover", max_steps=3, max_tool_calls=3)["child_run_id"]
        assert m.wait([third], 2)["agents"][0]["status"] == "succeeded"
        assert "FIRST" in str(calls[2]) and "FAILURE_DIRECTIVE" not in str(calls[2])
        assert m.snapshot(second)["status"] == "failed"
        assert m.unresolved() == []
        assert m.usage == {"input_tokens": 2, "output_tokens": 2}
    finally:
        m.finish()


@pytest.mark.parametrize("mode", ["direct", "task"])
@pytest.mark.parametrize("cache", [{}, {"cache_read_input_tokens": 0},
                                  {"cache_read_input_tokens": 7, "cache_creation_input_tokens": 3}])
def test_parent_session_aggregates_optional_child_cache_usage(tmp_path, mode, cache):
    import json
    from app.agent.runtime import AgentSession
    from app.config.loader import use_workspace_root
    from app.models.protocol import ModelResponse, ToolCall
    from tests.support.delegation import registry

    class Parent(FakeRouter):
        work_rounds = 0
        parent_rounds = 0

        def fork(self):
            result = response("CHILD_DONE")
            result.usage.update(cache)
            return FakeRouter([result])

        def create_message(self, messages, **kwargs):
            self.parent_rounds += 1
            if not kwargs.get("tools"):
                return response(json.dumps({"tasks": [{"id": "t", "content": "总结材料"}]}))
            self.work_rounds += 1
            if self.work_rounds == 1:
                return ModelResponse("", [ToolCall("spawn", "spawn_agent", {
                    "instruction": "总结材料", "max_steps": 3, "max_tool_calls": 3,
                })], {"input_tokens": 1, "output_tokens": 1}, [])
            return response("PARENT_DONE")

    model = Parent([])
    session = AgentSession(model, registry(), orchestrate=True, mode=mode,
                           delegation_policy="auto", max_steps=16, max_task_steps=12,
                           max_tool_calls=20)
    with use_workspace_root(tmp_path):
        assert session.chat("使用子 agent 总结材料") == "PARENT_DONE"
    assert session.last_run_status == "completed"
    expected = {"input_tokens": model.parent_rounds + 1,
                "output_tokens": model.parent_rounds + 1, **cache}
    assert session.last_turn_usage == expected
    assert session.cumulative_usage == expected


def test_started_observer_failure_does_not_erase_running_child(tmp_path):
    m = manager(tmp_path)
    try:
        sid = m.spawn("first", max_steps=3, max_tool_calls=3)["child_run_id"]
        m.wait([sid], 2)
        original = m._emit
        def observer(event):
            if event.kind == "subagent_started":
                raise RuntimeError("observer failure")
            original(event)
        m._emit = observer
        next_id = m.resume(sid, "followup", max_steps=3, max_tool_calls=3)["child_run_id"]
        assert m.wait([next_id], 2)["agents"][0]["status"] == "succeeded"
        assert len(m.children) == 2
    finally:
        m.finish()
