"""离线测试：审批策略。不需要网络或模型。"""
from unittest.mock import patch

import pytest

from app.agent.approval import AutoApprove, DenyAll, InteractivePolicy


def test_auto_approve():
    assert AutoApprove().request("write_file", {"path": "/tmp/x"}) is True


def test_deny_all():
    assert DenyAll().request("write_file", {"path": "/tmp/x"}) is False


def test_interactive_yes():
    with patch("builtins.input", return_value="y"):
        assert InteractivePolicy().request("write_file", {}) is True


def test_interactive_no():
    with patch("builtins.input", return_value="n"):
        assert InteractivePolicy().request("write_file", {}) is False


def test_interactive_always():
    policy = InteractivePolicy()
    with patch("builtins.input", return_value="a"):
        assert policy.request("write_file", {}) is True
    # 第二次不再询问
    assert policy.request("write_file", {}) is True


def test_interactive_eof():
    with patch("builtins.input", side_effect=EOFError):
        assert InteractivePolicy().request("write_file", {}) is False
