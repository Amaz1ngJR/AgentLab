import json

import pytest

from app.agent.profiles import AgentProfile
from app.agent.runtime import AgentSession
from app.agent.service import RuntimeService
from app.agent.session_router import SessionRouter
from app.storage import Storage
from app.tools.builtin.user_input import make_request_user_input_tool
from app.tools.registry import ToolRegistry
from tests.support.runtime import FakeRouter, _resp_text, _resp_tool


QUESTIONS = [
    {"id": "scope", "question": "选择变更范围", "options": [{"id": "api", "label": "API"}]},
    {"id": "details", "question": "还需要什么？"},
]


@pytest.mark.parametrize("cancel", [False, True])
def test_structured_question_round_trips_through_runtime_service(tmp_path, cancel):
    llm = FakeRouter([
        _resp_tool("ask", "request_user_input", {"questions": QUESTIONS}),
        _resp_text("收到"),
    ])
    storage = Storage(tmp_path / "db")
    registry = ToolRegistry()
    router = SessionRouter(
        storage, lambda profile, sid: AgentSession(llm, registry),
        {"default": AgentProfile("default", "Default", "fake")},
    )
    sid = router.new("default")
    service = RuntimeService(router)
    registry.register(make_request_user_input_tool(
        lambda questions, token: service.request_user_input(sid, questions, token),
    ))
    received = []

    def answer(event):
        if event.kind != "user_input.requested":
            return
        received.append((event.thread_id, event.turn_id, event.payload["questions"]))
        request_id = event.payload["request_id"]
        if cancel:
            service.cancel_run("run-question")
            return
        received.append(service.answer_request(request_id, "approve"))
        with pytest.raises(ValueError, match="invalid answer"):
            service.answer_request(request_id, {"answers": {
                "scope": {"option_id": "invalid"}, "details": {"text": "执行"},
            }})
        received.append(service.answer_request(request_id, {"answers": {
            "scope": {"option_id": "api"}, "details": {"text": "  只读  "},
        }}))
        received.append(service.answer_request(request_id, {"answers": {
            "scope": {"option_id": "api"}, "details": {"text": "迟到"},
        }}))

    service.subscribe_protocol(answer)
    try:
        reply = service.send_message("确认范围", run_id="run-question")
        assert received[0] == (sid, "run-question", QUESTIONS)
        if cancel:
            assert reply == "已中断。"
            assert storage.get_runtime_turn("run-question")["status"] == "cancelled"
            assert len(llm.calls) == 1
        else:
            assert received[1:] == [False, True, False]
            assert reply == "收到"
            tool_result = llm.calls[1][-1]["content"][0]
            assert json.loads(tool_result["content"]) == {
                "status": "answered",
                "answers": {"scope": {"option_id": "api"}, "details": {"text": "只读"}},
            }
        assert service.approval_broker.pending() == []
    finally:
        service.close()
        storage.close()


def test_tool_rejects_duplicate_question_ids_without_waiting():
    registry = ToolRegistry()
    registry.register(make_request_user_input_tool(
        lambda *_: pytest.fail("invalid questions must not create a pending request"),
    ))
    text, error = registry.execute("request_user_input", {"questions": [QUESTIONS[0], QUESTIONS[0]]})
    assert error and "unique" in text


def test_cli_prompt_collects_choice_and_text(monkeypatch):
    import sys
    import prompt_toolkit
    from app.agent.user_questions import prompt_questions
    from app.util import menu

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(menu, "select_menu", lambda **_: ("option_id", "api"))

    class Prompt:
        def __init__(self, **_):
            pass

        def prompt(self, _):
            return "只读"

    monkeypatch.setattr(prompt_toolkit, "PromptSession", Prompt)
    result = prompt_questions(QUESTIONS)
    assert result.answers == {"scope": {"option_id": "api"}, "details": {"text": "只读"}}
