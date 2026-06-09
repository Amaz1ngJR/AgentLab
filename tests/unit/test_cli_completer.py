"""离线测试:CLI 斜杠命令补全器 _SlashCompleter。"""
from unittest.mock import MagicMock

from prompt_toolkit.document import Document

from app.cli import _SlashCompleter


def _completer():
    router = MagicMock()
    router.list_sessions.return_value = [
        {"id": "abc12345", "title": "会话A"},
        {"id": "def67890", "title": "会话B"},
    ]
    p_coder = MagicMock()
    p_coder.name = "代码助手"
    p_default = MagicMock()
    p_default.name = "默认助手"
    router.list_profiles.return_value = {"coder": p_coder, "default": p_default}
    return _SlashCompleter(router)


def _complete(c, text):
    return [x.text for x in c.get_completions(Document(text, len(text)), None)]


def test_top_level_slash():
    c = _completer()
    assert set(_complete(c, "/")) == {"/reset", "/session"}


def test_top_level_prefix():
    c = _completer()
    assert _complete(c, "/se") == ["/session"]


def test_session_subcommands():
    c = _completer()
    out = _complete(c, "/session ")
    assert "list" in out and "switch" in out and "archive" in out


def test_session_subcommand_prefix():
    c = _completer()
    assert _complete(c, "/session sw") == ["switch"]


def test_switch_completes_session_ids():
    c = _completer()
    assert _complete(c, "/session switch ") == ["abc12345", "def67890"]


def test_switch_session_id_prefix():
    c = _completer()
    assert _complete(c, "/session switch a") == ["abc12345"]


def test_new_completes_agent_ids():
    c = _completer()
    assert set(_complete(c, "/session new ")) == {"coder", "default"}


def test_plain_text_no_completion():
    """普通对话输入(不以 / 开头)不应补全,避免打扰。"""
    c = _completer()
    assert _complete(c, "帮我看 README") == []
    assert _complete(c, "hello") == []


def test_unknown_slash_command_no_deep_completion():
    """非 /session 的命令后接空格不给子命令补全。"""
    c = _completer()
    assert _complete(c, "/reset ") == []
