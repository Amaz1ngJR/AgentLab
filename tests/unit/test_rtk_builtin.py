"""AgentLab 内置 RTK 风格过滤器测试。"""
from app.tools.rtk_builtin import (
    BuiltinRTK,
    BuiltinRTKConfig,
    classify_command,
    format_status,
)


def test_classifies_common_commands_cross_platform_strings():
    assert classify_command("git status --short") == "git_status"
    assert classify_command("python -m pytest tests -q") == "pytest"
    assert classify_command("rg foo app") == "grep"
    assert classify_command("Get-ChildItem") == "generic"  # 安全回退，不猜 PowerShell


def test_pytest_success_collapses_passing_noise():
    raw = "\n".join(["tests/test_x.py::test_ok PASSED"] * 100 + ["100 passed in 2.0s"])
    result = BuiltinRTK().compress("pytest -v", raw, "", 0)
    assert result.applied
    assert result.output == "100 passed in 2.0s"
    assert result.savings_percent > 90


def test_pytest_failure_keeps_failure_and_exit_context():
    raw = "\n".join(
        ["noise"] * 100
        + ["FAILED tests/test_x.py::test_bad", "E   AssertionError: expected 1", "1 failed"]
    )
    result = BuiltinRTK().compress("pytest", raw, "", 1)
    assert result.applied
    assert "FAILED tests/test_x.py::test_bad" in result.output
    assert "AssertionError" in result.output


def test_git_status_porcelain_groups_files():
    raw = "\n".join(
        [f" M app/module_{i}.py" for i in range(20)]
        + [f"?? app/new_{i}.py" for i in range(10)]
        + [f"D  app/old_{i}.py" for i in range(10)]
    )
    engine = BuiltinRTK(BuiltinRTKConfig(ultra_compact=True))
    result = engine.compress("git status --short", raw, "", 0)
    assert result.applied
    assert "modified:20" in result.output
    assert "untracked:10" in result.output
    assert "deleted:10" in result.output


def test_grep_groups_matches_and_truncates():
    raw = "\n".join([f"app/a.py:{i}:match {i}" for i in range(1, 80)])
    result = BuiltinRTK().compress("rg match app", raw, "", 0)
    assert result.applied
    assert "app/a.py (79)" in result.output
    assert "more" in result.output


def test_unknown_short_output_falls_back_exactly():
    raw = "hello\nworld"
    result = BuiltinRTK().compress("custom-tool", raw, "", 0)
    assert not result.applied
    assert result.output == raw


def test_disabled_returns_raw_output():
    engine = BuiltinRTK(BuiltinRTKConfig(enabled=False))
    result = engine.compress("pytest", "10 passed", "warning", 0)
    assert not result.applied
    assert result.output == "10 passed\n[stderr]\nwarning"


def test_status_explicitly_says_no_binary_required():
    status = format_status(BuiltinRTKConfig(enabled=True, ultra_compact=True))
    assert "built-in" in status
    assert "no external binary required" in status
    assert "ultra_compact: true" in status
