"""Verifier 单元测试。"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.agent.approval import AutoApprove, DenyAll
from app.agent.goals import VerificationCheck
from app.agent.verifier import CheckResult, VerificationResult, Verifier


@pytest.fixture
def temp_workspace():
    """临时工作区。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def _approved_verifier(workspace: Path) -> Verifier:
    return Verifier(workspace_root=str(workspace), approval=AutoApprove())


def test_verifier_command_pass(temp_workspace):
    """命令 exit code 0 应通过。"""
    verifier = _approved_verifier(temp_workspace)
    check = VerificationCheck(type="command", command="echo hello")
    result = verifier.verify([check])
    assert result.is_success()
    assert result.checks[0].status == "pass"


def test_verifier_command_fail(temp_workspace):
    """命令 exit code 非 0 应失败。"""
    verifier = _approved_verifier(temp_workspace)
    check = VerificationCheck(type="command", command="exit 1")
    result = verifier.verify([check])
    assert not result.is_success()
    assert result.status == "fail"
    assert result.checks[0].status == "fail"


def test_verifier_command_expected_nonzero(temp_workspace):
    """可以期望非 0 的 exit code。"""
    verifier = _approved_verifier(temp_workspace)
    check = VerificationCheck(
        type="command",
        command="exit 2",
        expected_exit_code=2,
    )
    result = verifier.verify([check])
    assert result.is_success()


def test_verifier_command_timeout(temp_workspace):
    """命令超时应失败。"""
    verifier = _approved_verifier(temp_workspace)
    check = VerificationCheck(
        type="command",
        command=f'"{sys.executable}" -c "import time; time.sleep(10)"',
        timeout=1,
    )
    result = verifier.verify([check])
    assert result.status == "fail"
    assert "超时" in result.checks[0].summary


def test_verifier_file_exists_pass(temp_workspace):
    """文件存在检查通过。"""
    test_file = temp_workspace / "test.txt"
    test_file.write_text("hello")

    verifier = _approved_verifier(temp_workspace)
    check = VerificationCheck(
        type="file_assertion",
        path="test.txt",
        exists=True,
    )
    result = verifier.verify([check])
    assert result.is_success()


def test_verifier_file_exists_fail(temp_workspace):
    """文件不存在检查失败。"""
    verifier = _approved_verifier(temp_workspace)
    check = VerificationCheck(
        type="file_assertion",
        path="nonexistent.txt",
        exists=True,
    )
    result = verifier.verify([check])
    assert not result.is_success()
    assert "不存在" in result.checks[0].summary


def test_verifier_file_not_exists_pass(temp_workspace):
    """文件不应存在检查通过。"""
    verifier = _approved_verifier(temp_workspace)
    check = VerificationCheck(
        type="file_assertion",
        path="should_not_exist.txt",
        exists=False,
    )
    result = verifier.verify([check])
    assert result.is_success()


def test_verifier_file_contains_pass(temp_workspace):
    """文件内容包含检查通过。"""
    test_file = temp_workspace / "config.txt"
    test_file.write_text("debug=true\nport=8080")

    verifier = Verifier(workspace_root=str(temp_workspace))
    check = VerificationCheck(
        type="file_assertion",
        path="config.txt",
        contains="port=8080",
    )
    result = verifier.verify([check])
    assert result.is_success()


def test_verifier_file_contains_fail(temp_workspace):
    """文件内容不包含检查失败。"""
    test_file = temp_workspace / "config.txt"
    test_file.write_text("debug=false")

    verifier = Verifier(workspace_root=str(temp_workspace))
    check = VerificationCheck(
        type="file_assertion",
        path="config.txt",
        contains="port=8080",
    )
    result = verifier.verify([check])
    assert not result.is_success()


def test_verifier_file_not_contains_pass(temp_workspace):
    """文件内容不应包含检查通过。"""
    test_file = temp_workspace / "secrets.txt"
    test_file.write_text("safe_config=yes")

    verifier = Verifier(workspace_root=str(temp_workspace))
    check = VerificationCheck(
        type="file_assertion",
        path="secrets.txt",
        not_contains="password",
    )
    result = verifier.verify([check])
    assert result.is_success()


def test_verifier_file_not_contains_fail(temp_workspace):
    """文件包含不应有的内容应失败。"""
    test_file = temp_workspace / "secrets.txt"
    test_file.write_text("password=123456")

    verifier = Verifier(workspace_root=str(temp_workspace))
    check = VerificationCheck(
        type="file_assertion",
        path="secrets.txt",
        not_contains="password",
    )
    result = verifier.verify([check])
    assert not result.is_success()


def test_verifier_multiple_checks_all_pass(temp_workspace):
    """多个检查都通过。"""
    (temp_workspace / "test.txt").write_text("ok")

    verifier = _approved_verifier(temp_workspace)
    checks = [
        VerificationCheck(type="command", command="echo test"),
        VerificationCheck(type="file_assertion", path="test.txt", exists=True),
    ]
    result = verifier.verify(checks)
    assert result.is_success()
    assert len(result.checks) == 2


def test_verifier_multiple_checks_one_fail(temp_workspace):
    """多个检查中一个失败则整体失败。"""
    verifier = _approved_verifier(temp_workspace)
    checks = [
        VerificationCheck(type="command", command="echo test"),
        VerificationCheck(type="file_assertion", path="missing.txt", exists=True),
    ]
    result = verifier.verify(checks)
    assert not result.is_success()
    assert result.status == "fail"


def test_verifier_blocked_takes_priority(temp_workspace):
    """blocked 优先级高于 fail。"""
    verifier = _approved_verifier(temp_workspace)
    checks = [
        VerificationCheck(type="command", command="exit 1"),  # fail
        VerificationCheck(type="human", description="需人工确认"),  # blocked
    ]
    result = verifier.verify(checks)
    assert result.status == "blocked"


def test_verifier_command_without_policy_is_blocked(temp_workspace):
    verifier = Verifier(workspace_root=str(temp_workspace))
    result = verifier.verify([
        VerificationCheck(type="command", command="echo should-not-run"),
    ])
    assert result.status == "blocked"
    assert result.checks[0].error == "approval_policy_missing"


def test_verifier_command_denied_is_blocked(temp_workspace):
    verifier = Verifier(
        workspace_root=str(temp_workspace),
        approval=DenyAll(),
    )
    result = verifier.verify([
        VerificationCheck(type="command", command="echo should-not-run"),
    ])
    assert result.status == "blocked"
    assert result.checks[0].error == "approval_denied"


def test_verifier_empty_checks():
    """空验证计划应返回 blocked。"""
    verifier = Verifier()
    result = verifier.verify([])
    assert result.status == "blocked"
    assert result.failure_category == "no_verifier"


def test_verifier_human_check_uses_interactive_confirmation(temp_workspace):
    verifier = Verifier(
        workspace_root=str(temp_workspace),
        human_confirm=lambda prompt: prompt == "确认页面正常",
    )
    check = VerificationCheck(type="human", prompt="确认页面正常")
    result = verifier.verify([check])
    assert result.status == "pass"
    assert result.checks[0].evidence_ref == "human:confirmed"


def test_verifier_api_check_success(temp_workspace):
    response = MagicMock()
    response.status = 201
    response.read.return_value = b'{"ok": true}'
    response.__enter__.return_value = response
    verifier = Verifier(workspace_root=str(temp_workspace), approval=AutoApprove())
    check = VerificationCheck(
        type="api",
        method="POST",
        endpoint="https://example.com/api",
        expected_status=201,
        request_body='{"name":"test"}',
        response_contains='"ok": true',
    )
    with patch("urllib.request.urlopen", return_value=response) as urlopen:
        result = verifier.verify([check])
    assert result.status == "pass"
    assert result.checks[0].evidence_ref == "api:POST:https://example.com/api"
    assert urlopen.call_args.args[0].method == "POST"


def test_verifier_api_check_requires_approval(temp_workspace):
    verifier = Verifier(workspace_root=str(temp_workspace), approval=DenyAll())
    check = VerificationCheck(type="api", endpoint="https://example.com/api")
    result = verifier.verify([check])
    assert result.status == "blocked"
    assert "拒绝" in result.checks[0].summary

def test_verifier_human_check_blocked_without_interactor(temp_workspace):
    """没有交互器时 human verifier 安全阻塞。"""
    verifier = Verifier(workspace_root=str(temp_workspace))
    check = VerificationCheck(type="human", description="请确认功能正常")
    result = verifier.verify([check])
    assert result.status == "blocked"
    assert "人工确认" in result.checks[0].summary or "手动确认" in result.checks[0].summary


def test_verifier_unimplemented_type(temp_workspace):
    """未实现的验证器类型应返回 blocked。"""
    verifier = Verifier(workspace_root=str(temp_workspace))
    check = VerificationCheck(type="browser", url="http://example.com")
    result = verifier.verify([check])
    assert result.status == "blocked"
    assert "未实现" in result.checks[0].summary


def test_verifier_workspace_root_default():
    """workspace_root 默认为当前目录。"""
    verifier = Verifier()
    assert verifier.workspace_root == Path.cwd()
