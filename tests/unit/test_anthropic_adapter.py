"""Anthropic 图片 file 引用转换测试。"""
from PIL import Image

from app import attachments
from app.attachments import AttachmentStore, build_user_content
from app.models.anthropic_adapter import _materialize_anthropic_messages


def test_anthropic_file_image_materializes_to_base64(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "DEFAULT_ATTACHMENT_ROOT", tmp_path)
    path = tmp_path / "shot.png"
    Image.new("RGB", (5, 4), "green").save(path)
    attachment = AttachmentStore(tmp_path).add_path(
        "s1", path, workspace_root=tmp_path,
    )
    messages = [{
        "role": "user", "content": build_user_content("分析", [attachment]),
    }]
    converted = _materialize_anthropic_messages(messages)
    image = converted[0]["content"][1]
    assert image["type"] == "image"
    assert image["source"]["type"] == "base64"
    assert image["source"]["media_type"] == "image/png"
    assert image["source"]["data"]
    # 转换不修改持久化历史中的 file 引用。
    assert messages[0]["content"][1]["source"]["type"] == "file"
