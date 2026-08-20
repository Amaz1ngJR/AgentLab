"""Session 图片清理的引用、磁盘与 Service 互斥测试。"""
from io import BytesIO

import pytest
from PIL import Image

from app import attachments
from app.agent.approval import AutoApprove
from app.agent.profiles import AgentProfile
from app.agent.runtime import AgentSession
from app.agent.service import RuntimeService, _ActiveRun
from app.agent.session_router import SessionRouter
from app.attachments import AttachmentStore, build_user_content
from app.models.protocol import ModelResponse
from app.storage import Storage
from app.tools.registry import ToolRegistry


class _LLM:
    model = "fake"
    provider = "fake"
    supports_vision = True

    def create_message(self, messages, **kwargs):
        return ModelResponse(text="ok", tool_calls=[], usage={}, provider_payload=[])

    def format_tool_results(self, results):
        return []


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 4), "red").save(output, "PNG")
    return output.getvalue()


def _router(tmp_path, monkeypatch):
    root = tmp_path / "attachments"
    monkeypatch.setattr(attachments, "DEFAULT_ATTACHMENT_ROOT", root)
    storage = Storage(tmp_path / "db")

    def factory(profile, session_id):
        return AgentSession(_LLM(), ToolRegistry(), approval=AutoApprove())

    router = SessionRouter(
        storage,
        factory,
        {"default": AgentProfile("default", "Default", "fake")},
    )
    return router, storage, AttachmentStore()


def test_clear_session_images_keeps_text_and_deletes_files(tmp_path, monkeypatch):
    router, storage, attachment_store = _router(tmp_path, monkeypatch)
    sid = router.new("default")
    image = attachment_store.add_bytes(sid, _png(), "shot.png")
    router.current.messages = [
        {"role": "user", "content": build_user_content("分析图片", [image])},
        {"role": "assistant", "content": "图片是红色"},
    ]
    router.persist_current()

    result = router.clear_session_images()

    assert result == {"references": 1, "files": 1}
    assert router.current.messages[0]["content"] == [
        {"type": "text", "text": "分析图片"},
    ]
    assert router.current.messages[1]["content"] == "图片是红色"
    assert attachment_store.list_session(sid) == []
    assert storage.load_messages(sid) == router.current.messages


def test_clear_session_images_drops_image_only_message(tmp_path, monkeypatch):
    router, _, attachment_store = _router(tmp_path, monkeypatch)
    sid = router.new("default")
    image = attachment_store.add_bytes(sid, _png())
    router.current.messages = [
        {"role": "user", "content": build_user_content("", [image])},
        {"role": "assistant", "content": "done"},
    ]
    router.clear_session_images()
    assert router.current.messages == [{"role": "assistant", "content": "done"}]


def test_runtime_service_refuses_clear_during_active_run(tmp_path, monkeypatch):
    router, _, _ = _router(tmp_path, monkeypatch)
    sid = router.new("default")
    service = RuntimeService(router)
    service._runs["r1"] = _ActiveRun("r1", sid, object())
    service._session_runs[sid] = "r1"
    with pytest.raises(RuntimeError, match="正在执行"):
        service.clear_session_images()
