"""内置工具聚合入口。

CLI 启动时调 default_tools() 一次性拿到所有内置工具(文件 + shell + ...),
注册到 ToolRegistry。新增内置工具只需在这里 + 一条 import。
"""
from __future__ import annotations

from app.tools.builtin.code_search import default_tools as _code_search_tools
from app.tools.builtin.files import default_tools as _file_tools
from app.tools.builtin.shell import default_tools as _shell_tools
from app.tools.builtin.web_fetch import default_tools as _web_fetch_tools
from app.tools.builtin.web_search import default_tools as _web_search_tools
from app.tools.builtin.confirmation import REQUEST_CONFIRMATION, CONFIRMATION_GUIDANCE
from app.tools.builtin.patch import APPLY_PATCH, PATCH_GUIDANCE
from app.tools.builtin.read_batch import READ_FILES, READ_GUIDANCE
from app.tools.registry import Tool


def default_tools() -> list[Tool]:
    """返回所有内置工具的合集。顺序仅影响 banner 显示。

    注意:交互式会话工具(pty_*)是有状态的,需要绑定会话级 PtySessionManager,
    所以不在这里,由 CLI 用 make_pty_tools(manager) 工厂按会话注入(同 todo_write)。
    """
    return [READ_FILES, APPLY_PATCH, REQUEST_CONFIRMATION, *_file_tools(), *_code_search_tools(), *_shell_tools(),
            *_web_search_tools(), *_web_fetch_tools()]


def with_tool_guidance(prompt: str, tools) -> str:
    """Attach instructions for available capabilities once, including custom profiles."""
    for names, guidance in (
        (("read_file", "read_files"), READ_GUIDANCE),
        (("apply_patch",), PATCH_GUIDANCE),
        (("request_confirmation",), CONFIRMATION_GUIDANCE),
    ):
        if guidance not in prompt and any(tools.get(name) is not None for name in names):
            prompt += "\n\n" + guidance
    return prompt
