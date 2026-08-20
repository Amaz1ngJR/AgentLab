"""AgentLab 版本信息的单一来源。

发布新版本时只修改 ``__version__``；``pyproject.toml`` 会从这里读取构建版本，
CLI 的启动横幅、``--version`` 和 REPL 的 ``/version`` 也复用同一个值，避免
安装包版本与界面显示不一致。
"""

__version__ = "0.1.2"


def version_text() -> str:
    """返回适合 CLI 展示的稳定版本字符串。"""
    return f"AgentLab {__version__}"
