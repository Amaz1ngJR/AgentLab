"""图片附件的校验、受控存储与模型消息转换。

附件不会以 base64 形式写入 SQLite；消息历史只保存位于 ``data/attachments``
下的受控文件引用。各 Provider Adapter 在发请求前才读取并编码图片，避免数据库和
上下文被大段 base64 撑满。
"""
from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ATTACHMENT_ROOT = PROJECT_ROOT / "data" / "attachments"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
SUPPORTED_IMAGE_FORMATS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
}


class AttachmentError(ValueError):
    """附件不合法、越界或当前环境无法读取时抛出。"""


@dataclass(frozen=True)
class ImageAttachment:
    """可安全持久化的图片引用。"""

    attachment_id: str
    path: str
    media_type: str
    size_bytes: int
    width: int
    height: int
    sha256: str
    original_name: str

    def to_content_block(self) -> dict[str, Any]:
        """转换为 AgentLab 内部多模态 content block。"""
        return {
            "type": "image",
            "source": {
                "type": "file",
                "path": self.path,
                "media_type": self.media_type,
                "sha256": self.sha256,
            },
            "metadata": {
                "attachment_id": self.attachment_id,
                "size_bytes": self.size_bytes,
                "width": self.width,
                "height": self.height,
                "original_name": self.original_name,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClipboardImage:
    """尚未落盘的剪贴板图片。"""

    data: bytes
    name: str = "clipboard.png"


@dataclass(frozen=True)
class ClipboardContent:
    image: ClipboardImage | None = None
    text: str | None = None


class AttachmentStore:
    """把用户图片复制到按 session 隔离的附件目录。"""

    def __init__(self, root: Path | None = None):
        self.root = (root or DEFAULT_ATTACHMENT_ROOT).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def add_path(
        self,
        session_id: str,
        path: str | Path,
        *,
        workspace_root: Path,
        approval=None,
    ) -> ImageAttachment:
        source = Path(path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise AttachmentError(f"图片路径不是文件: {source}")
        workspace = workspace_root.expanduser().resolve()
        if not _is_relative_to(source, workspace):
            if approval is None or not approval.request(
                "attach_image_outside_workspace",
                {"path": str(source), "purpose": "image_attachment"},
            ):
                raise AttachmentError("用户拒绝读取工作区外的图片")
        if source.stat().st_size > MAX_IMAGE_BYTES:
            raise AttachmentError(f"图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB 上限")
        return self.add_bytes(session_id, source.read_bytes(), source.name)

    def add_bytes(
        self,
        session_id: str,
        data: bytes,
        original_name: str = "image",
    ) -> ImageAttachment:
        if not data:
            raise AttachmentError("图片内容为空")
        if len(data) > MAX_IMAGE_BYTES:
            raise AttachmentError(f"图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB 上限")
        image_format, width, height = _inspect_image(data)
        media_type, suffix = SUPPORTED_IMAGE_FORMATS[image_format]
        digest = hashlib.sha256(data).hexdigest()
        safe_session = _safe_component(session_id)
        session_dir = (self.root / safe_session).resolve()
        if not _is_relative_to(session_dir, self.root):
            raise AttachmentError("非法 session_id")
        session_dir.mkdir(parents=True, exist_ok=True)
        target = session_dir / f"{digest}{suffix}"
        if not target.exists():
            temp = target.with_suffix(target.suffix + ".tmp")
            temp.write_bytes(data)
            os.replace(temp, target)
        return ImageAttachment(
            attachment_id=f"image-{digest[:16]}",
            path=str(target),
            media_type=media_type,
            size_bytes=len(data),
            width=width,
            height=height,
            sha256=digest,
            original_name=Path(original_name).name[:200] or f"image{suffix}",
        )

    def list_session(self, session_id: str) -> list[Path]:
        """列出某 Session 已落盘的所有图片文件。"""
        path = self._session_dir(session_id)
        if not path.exists():
            return []
        return sorted(item for item in path.iterdir() if item.is_file())

    def delete_session(self, session_id: str) -> int:
        """删除某 Session 的附件目录，返回删除的文件数。"""
        path = self._session_dir(session_id)
        count = len(self.list_session(session_id))
        shutil.rmtree(path, ignore_errors=False) if path.exists() else None
        return count

    def _session_dir(self, session_id: str) -> Path:
        path = (self.root / _safe_component(session_id)).resolve()
        if not _is_relative_to(path, self.root):
            raise AttachmentError("非法 session_id")
        return path


def strip_image_blocks(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """从历史中移除所有图片 block，保留同条消息里的文本和工具结果。"""
    cleaned: list[dict[str, Any]] = []
    removed = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            cleaned.append(message)
            continue
        blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image":
                removed += 1
            else:
                blocks.append(block)
        # 图片消息可能没有文字；移除后为空时整条丢弃，避免 provider 收到空 content。
        if blocks:
            cleaned.append({**message, "content": blocks})
        elif not content:
            cleaned.append(message)
    return cleaned, removed


def build_user_content(text: str, images: list[ImageAttachment] | None = None) -> str | list[dict]:
    """构建内部用户消息；没有图片时保持原字符串格式以兼容旧路径。"""
    if not images:
        return text
    blocks: list[dict] = []
    if text.strip():
        blocks.append({"type": "text", "text": text})
    blocks.extend(image.to_content_block() for image in images)
    return blocks


def materialize_image_block(block: dict[str, Any]) -> tuple[str, str]:
    """将内部图片块读取为 ``(MIME, base64)``，并防止消息伪造任意路径。"""
    source = block.get("source") or {}
    if source.get("type") == "base64":
        return source.get("media_type", "image/png"), source.get("data", "")
    if source.get("type") != "file":
        raise AttachmentError("不支持的图片 source 类型")
    path = Path(str(source.get("path") or "")).expanduser().resolve(strict=True)
    root = DEFAULT_ATTACHMENT_ROOT.resolve()
    if not _is_relative_to(path, root):
        raise AttachmentError("图片引用不在受控附件目录")
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise AttachmentError("持久化图片超过大小上限")
    image_format, _, _ = _inspect_image(data)
    detected_mime = SUPPORTED_IMAGE_FORMATS[image_format][0]
    digest = hashlib.sha256(data).hexdigest()
    expected = source.get("sha256")
    if expected and expected != digest:
        raise AttachmentError("图片附件哈希校验失败")
    return detected_mime, base64.b64encode(data).decode("ascii")


def image_block_to_data_url(block: dict[str, Any]) -> str:
    media_type, encoded = materialize_image_block(block)
    return f"data:{media_type};base64,{encoded}"


def capture_system_clipboard() -> ClipboardContent:
    """读取系统剪贴板；优先图片，没有图片时返回文本。

    Pillow 的 ImageGrab 在 Windows/macOS 可直接读取图片，在常见 Linux 桌面会调用
    wl-paste/xclip。终端若吞掉 Ctrl+V，用户仍可用 ``/paste-image`` 触发同一逻辑。
    """
    try:
        from PIL import ImageGrab
        value = ImageGrab.grabclipboard()
        if value is not None and hasattr(value, "save"):
            output = io.BytesIO()
            value.save(output, format="PNG")
            return ClipboardContent(image=ClipboardImage(output.getvalue()))
        if isinstance(value, list):
            for item in value:
                path = Path(item)
                if path.is_file() and _looks_like_image_path(path):
                    return ClipboardContent(
                        image=ClipboardImage(path.read_bytes(), path.name)
                    )
    except Exception:
        pass
    text = _read_clipboard_text()
    return ClipboardContent(text=text if text else None)


def _read_clipboard_text() -> str | None:
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()
    except Exception:
        return None


def _inspect_image(data: bytes) -> tuple[str, int, int]:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise AttachmentError(
            "图片支持需要 Pillow，请执行: python -m pip install Pillow"
        ) from exc
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AttachmentError(f"无法识别图片: {exc}") from exc
    if image_format not in SUPPORTED_IMAGE_FORMATS:
        raise AttachmentError(
            f"不支持 {image_format or 'unknown'} 图片，仅支持 PNG/JPEG/WEBP/GIF"
        )
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise AttachmentError("图片尺寸无效或像素数超过安全上限")
    return image_format, width, height


def _safe_component(value: str) -> str:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in "-_")
    if not safe:
        raise AttachmentError("非法 session_id")
    return safe


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _looks_like_image_path(path: Path) -> bool:
    mime, _ = mimetypes.guess_type(path.name)
    return bool(mime and mime.startswith("image/"))
