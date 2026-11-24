"""Open a workspace image and return a validated attachment to the model."""
from app.attachments import AttachmentError, AttachmentStore, MAX_IMAGE_BYTES
from app.models.protocol import ImageToolOutput
from app.tools.builtin.files import WorkspacePathError, _resolve_within_workspace
from app.tools.registry import Tool, ToolExecutionError


def make_view_image_tool(session_id: str, supports_vision) -> Tool:
    def view(args: dict) -> ImageToolOutput:
        if not supports_vision():
            raise ToolExecutionError("当前模型不支持查看图片")
        try:
            source = _resolve_within_workspace(args["path"])
            if not source.is_file():
                raise AttachmentError("图片路径不是普通文件")
            with source.open("rb") as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
            image = AttachmentStore().add_bytes(session_id, data, source.name)
        except (AttachmentError, OSError, WorkspacePathError) as exc:
            raise ToolExecutionError(f"无法读取工作区图片：{exc}") from exc
        return ImageToolOutput(
            f"图片 {image.original_name}（{image.width} × {image.height}）",
            image,
        )

    return Tool(
        name="view_image",
        description="查看工作区内的 PNG、JPEG、WebP 或 GIF，返回供模型观察的图片。",
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1},
        }, "required": ["path"], "additionalProperties": False},
        executor=view, risk="read", target_type="file", scope="workspace", origin="builtin",
    )
