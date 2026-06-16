"""Verifier 单元测试。"""
import tempfile
from pathlib import Path

import pytest

from app.agent.goals import VerificationCheck
from app.agent.verifier import CheckResult, VerificationResult, Verifier


@pytest.fixture
def temp_workspace():
    """临时工作区。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def test_verifier_command_pass(temp_workspace):
    """命令 exit code 0 应通过。"""
    verifier = Verifier(workspace_root=str(temp_workspace))
    check = VerificationCheck(type="command", command="echo hello")
    result = verifier.verify([check])
    assert result.is_success()
    assert result.checks[0].status == "pass"


def test_verifier_command_fail(temp_workspace):
    """命令 exit code 非 0 应失败。"""
    verifier = Verifier(workspace_root=str(temp_workspace))
    check = VerificationCheck(type="command", command="exit 1")
    result = verifier.verify([check])
    assert not result.is_success()
    assert result.status == "fail"
    assert result.checks[0].status == "fail"


def test_verifier_command_expected_nonzero(temp_workspace):
    """可以期望非 0 的 exit code。"""
    verifier = Verifier(workspace_root=str(temp_workspace))
    check = VerificationCheck(
        type="command",
        command="exit 2",
        expected_exit_code=2,
    )
    result = verifier.verify([check])
    assert result.is_success()


def test_verifier_command_timeout(temp_workspace):
    """命令超时应失败。"""
    verifier = Verifier(workspace_root=str(temp_workspace))
    check = VerificationCheck(
        type="command",
        command="sleep 10",
        timeout=1,
    )
    result = verifier.verify([check])
    assert result.status == "fail"
    assert "超时" in result.checks[0].summary


def test_verifier_file_exists_pass(temp_workspace):
    """文件存在检查通过。"""
    test_file = temp_workspace / "test.txt"
    test_file.write_text("hello")

    verifier = Verifier(workspace_root=str(temp_workspace))
    check = VerificationCheck(
        type="file_assertion",
        path="test.txt",
        exists=True,
    )
    result = verifier.verify([check])
    assert result.is_success()


def test_verifier_file_exists_fail(temp_workspace):
    """文件不存在检查失败。"""
    verifier = Verifier(workspace_root=str(temp_workspace))
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
    verifier = Verifier(workspace_root=str(temp_workspace))
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

    verifier = Verifier(workspace_root=str(temp_workspace))
    checks = [
        VerificationCheck(type="command", command="echo test"),
        VerificationCheck(type="file_assertion", path="test.txt", exists=True),
    ]
    result = verifier.verify(checks)
    assert result.is_success()
    assert len(result.checks) == 2


def test_verifier_multiple_checks_one_fail(temp_workspace):
    """多个检查中一个失败则整体失败。"""
    verifier = Verifier(workspace_root=str(temp_workspace))
    checks = [
        VerificationCheck(type="command", command="echo test"),
        VerificationCheck(type="file_assertion", path="missing.txt", exists=True),
    ]
    result = verifier.verify(checks)
    assert not result.is_success()
    assert result.status == "fail"


def test_verifier_blocked_takes_priority(temp_workspace):
    """blocked 优先级高于 fail。"""
    verifier = Verifier(workspace_root=str(temp_workspace))
    checks = [
        VerificationCheck(type="command", command="exit 1"),  # fail
        VerificationCheck(type="human", description="需人工确认"),  # blocked
    ]
    result = verifier.verify(checks)
    assert result.status == "blocked"


def test_verifier_empty_checks():
    """空验证计划应返回 blocked。"""
    verifier = Verifier()
    result = verifier.verify([])
    assert result.status == "blocked"
    assert result.failure_category == "no_verifier"


def test_verifier_human_check_blocked(temp_workspace):
    """human 类型总是返回 blocked。"""
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
