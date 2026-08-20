"""CLI 图片命令解析和剪贴板快捷键测试。"""
from io import BytesIO
from unittest.mock import MagicMock, patch

from PIL import Image

from app.attachments import AttachmentStore, ClipboardContent, ClipboardImage
from app.cli import (
    _clipboard_key_bindings,
    _extract_pasted_images,
    _image_command,
    _image_help_text,
)


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (3, 3), "red").save(output, "PNG")
    return output.getvalue()


def test_bare_image_command_is_help_not_agent_input():
    assert _image_command("/image") == ("help", None)
    assert _image_command("/image   ") == ("help", None)
    assert "不会" not in _image_help_text()  # 帮助保持用户导向，不含内部实现术语


def test_image_path_and_immediate_prompt_parse():
    assert _image_command("/image ./shot.png") == ("path", "./shot.png")
    assert _image_command("/image ./shot.png -- 分析") == (
        "path", "./shot.png -- 分析",
    )


def test_clipboard_shortcuts_attach_image(tmp_path):
    pending = {"images": {}}
    store = AttachmentStore(tmp_path)
    bindings = _clipboard_key_bindings(pending, store, lambda: "s1")
    keys = {str(binding.keys[0]) for binding in bindings.bindings}
    assert keys == {"Keys.ControlV", "Keys.ShiftInsert"}

    event = MagicMock()
    content = ClipboardContent(image=ClipboardImage(_png()))
    with patch("app.cli.capture_system_clipboard", return_value=content):
        bindings.bindings[0].handler(event)
    inserted = event.current_buffer.insert_text.call_args.args[0]
    assert inserted.startswith("[AgentLab:image:")
    text, images = _extract_pasted_images(inserted + " 请分析", pending)
    assert text == "请分析"
    assert len(images) == 1


def test_clipboard_shortcut_falls_back_to_text(tmp_path):
    pending = {"images": {}}
    bindings = _clipboard_key_bindings(pending, AttachmentStore(tmp_path), lambda: "s1")
    event = MagicMock()
    with patch(
        "app.cli.capture_system_clipboard",
        return_value=ClipboardContent(text="hello"),
    ):
        bindings.bindings[0].handler(event)
    event.current_buffer.insert_text.assert_called_once_with("hello")
