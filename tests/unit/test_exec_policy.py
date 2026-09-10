"""Offline tests for conservative, session-scoped command prefix rules."""
from app.agent.exec_policy import SessionExecPolicy, analyze_command


def test_compound_read_command_proposes_each_prefix(tmp_path):
    analysis = analyze_command(
        "nl -ba server/DiskSpaceManager.cpp | sed -n '1465,1750p'; echo done",
        cwd=tmp_path,
        workspace=tmp_path,
    )

    assert analysis.reusable is True
    assert [rule.tokens for rule in analysis.suggestions] == [
        ("nl", "-ba"),
        ("sed", "-n"),
        ("echo",),
    ]


def test_session_rule_requires_every_compound_segment(tmp_path):
    policy = SessionExecPolicy()
    first = analyze_command("git status", cwd=tmp_path, workspace=tmp_path)
    policy.remember(first, workspace=tmp_path)

    assert policy.allows(
        analyze_command("git status --short", cwd=tmp_path, workspace=tmp_path),
        workspace=tmp_path,
    )
    assert not policy.allows(
        analyze_command("git status; rg TODO", cwd=tmp_path, workspace=tmp_path),
        workspace=tmp_path,
    )


def test_rules_are_scoped_to_workspace(tmp_path):
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    policy = SessionExecPolicy()
    analysis = analyze_command("pytest tests", cwd=first_workspace, workspace=first_workspace)
    policy.remember(analysis, workspace=first_workspace)

    assert policy.allows(analysis, workspace=first_workspace)
    assert not policy.allows(
        analyze_command("pytest tests", cwd=second_workspace, workspace=second_workspace),
        workspace=second_workspace,
    )


def test_advanced_or_dangerous_commands_are_not_reusable(tmp_path):
    commands = [
        "echo ok > result.txt",
        "echo $(whoami)",
        "rm -rf build",
        "python -c 'print(1)'",
        "git diff --ext-diff",
    ]

    for command in commands:
        analysis = analyze_command(command, cwd=tmp_path, workspace=tmp_path)
        assert analysis.reusable is False, command


def test_obvious_outside_path_is_not_reusable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    analysis = analyze_command(
        "cat ../secret.txt",
        cwd=workspace,
        workspace=workspace,
    )

    assert analysis.reusable is False
    assert "工作区外" in analysis.reason


def test_windows_executable_name_is_normalized(tmp_path):
    analysis = analyze_command(
        r'rg.exe -n TODO src',
        cwd=tmp_path,
        workspace=tmp_path,
    )

    assert analysis.reusable is True
    assert analysis.commands[0][0] == "rg"
    assert analysis.suggestions[0].tokens == ("rg",)

