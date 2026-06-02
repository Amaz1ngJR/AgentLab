"""内置工具聚合入口。

CLI 启动时调 default_tools() 一次性拿到所有内置工具(文件 + shell + ...),
注册到 ToolRegistry。新增内置工具只需在这里 + 一条 import。
"""
from __future__ import annotations

from app.tools.builtin.code_search import default_tools as _code_search_tools
from app.tools.builtin.files import default_tools as _file_tools
from app.tools.builtin.shell import default_tools as _shell_tools
from app.tools.registry import Tool


def default_tools() -> list[Tool]:
    """返回所有内置工具的合集。顺序仅影响 banner 显示。"""
    return [*_file_tools(), *_code_search_tools(), *_shell_tools()]
