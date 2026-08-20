"""图片附件校验、持久化引用和消息构建测试。"""
from io import BytesIO

import pytest
from PIL import Image

from app.attachments import (
    AttachmentError,
    AttachmentStore,
    build_user_content,
    image_block_to_data_url,
)


def _png(color=(255, 0, 0), size=(8, 6)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def test_attachment_store_saves_file_reference_not_base64(tmp_path, monkeypatch):
    from app import attachments
    monkeypatch.setattr(attachments, "DEFAULT_ATTACHMENT_ROOT", tmp_path / "attachments")
    store = AttachmentStore()
    attachment = store.add_bytes("s1", _png(), "shot.png")
    block = attachment.to_content_block()

    assert attachment.width == 8
    assert attachment.height == 6
    assert block["source"]["type"] == "file"
    assert "data" not in block["source"]
    assert image_block_to_data_url(block).startswith("data:image/png;base64,")


def test_build_user_content_keeps_text_compatibility(tmp_path):
    store = AttachmentStore(tmp_path / "attachments")
    attachment = store.add_bytes("s1", _png(), "shot.png")
    assert build_user_content("hello") == "hello"
    content = build_user_content("看图", [attachment])
    assert content[0] == {"type": "text", "text": "看图"}
    assert content[1]["type"] == "image"


def test_invalid_and_oversized_image_rejected(tmp_path):
    store = AttachmentStore(tmp_path / "attachments")
    with pytest.raises(AttachmentError, match="无法识别"):
        store.add_bytes("s1", b"not image", "bad.png")


def test_outside_workspace_requires_approval(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png())
    store = AttachmentStore(tmp_path / "attachments")

    class Deny:
        def request(self, *_):
            return False

    with pytest.raises(AttachmentError, match="拒绝"):
        store.add_path("s1", outside, workspace_root=workspace, approval=Deny())
