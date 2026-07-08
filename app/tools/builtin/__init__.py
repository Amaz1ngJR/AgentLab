"""内置工具聚合入口。

CLI 启动时调 default_tools() 一次性拿到所有内置工具(文件 + shell + ...),
注册到 ToolRegistry。新增内置工具只需在这里 + 一条 import。
"""
from __future__ import annotations

from app.tools.builtin.code_search import default_tools as _code_search_tools
from app.tools.builtin.files import default_tools as _file_tools
from app.tools.builtin.shell import default_tools as _shell_tools
from app.tools.builtin.web_search import default_tools as _web_search_tools
from app.tools.registry import Tool


def default_tools() -> list[Tool]:
    """返回所有内置工具的合集。顺序仅影响 banner 显示。

    注意:交互式会话工具(pty_*)是有状态的,需要绑定会话级 PtySessionManager,
    所以不在这里,由 CLI 用 make_pty_tools(manager) 工厂按会话注入(同 todo_write)。
    """
    return [*_file_tools(), *_code_search_tools(), *_shell_tools(), *_web_search_tools()]
