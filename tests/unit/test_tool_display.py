import json

import pytest
from prompt_toolkit.utils import get_cwidth

from app.util.tool_display import render_tool_result


def test_shell_shows_command_and_four_output_lines():
    text = render_tool_result("shell", {"command": "date; uptime"}, "first\nsecond\nthird\nfourth\nfifth", elapsed=3.3)
    assert text.splitlines() == ["  ● Bash(date; uptime) · 3.3s", "    ⎿ first", "      second", "      third", "      fourth … +1 行"]


def test_batch_reads_decode_json_instead_of_showing_envelope():
    output = json.dumps({"files": [{"path": "src/test.cpp", "start_line": 10, "end_line": 13,
                                    "content": "int main() {\n  return 0;\n}\n// end\n"}]})
    text = render_tool_result("read_files", {"requests": [{"path": "src/test.cpp"}]}, output)
    assert "ReadFiles(src/test.cpp)" in text
    assert "⎿ src/test.cpp (10-13)" in text
    assert "int main() {" in text
    assert "return 0;" in text
    assert '"files"' not in text
    assert "… +1 行" in text


def test_interactive_terminal_identifies_session_and_sent_command():
    text = render_tool_result("terminal_send", {"session_id": "term-4", "input": "vmstat 1 3"}, "cpu stats")
    assert "Terminal[term-4](vmstat 1 3)" in text
    assert "⎿ cpu stats" in text


def test_multiline_command_wraps_but_does_not_flood_terminal():
    command = "python3 - <<'PY'\n" + "print('hello')\n" * 100 + "PY"
    text = render_tool_result("shell", {"command": command}, "hello", width=60, elapsed=2.3)
    lines = text.splitlines()
    assert len(lines) == 3
    assert "python3 - <<'PY'" in text and "print('hello')" in text
    assert "2.3s" in text and "…" in text
    assert all(get_cwidth(line) <= 60 for line in lines)


def test_long_command_and_result_never_exceed_two_plus_four_lines():
    text = render_tool_result("shell", {"command": "echo " + "长命令" * 100},
                              "\n".join(["较长的结果" * 50] * 20), width=60, elapsed=12.5)
    lines = text.splitlines()
    assert len(lines) == 6
    assert lines[1].endswith(" · 12.5s")
    assert "… +16 行" in lines[-1]
    assert all(get_cwidth(line) <= 60 for line in lines)


def test_unicode_lines_respect_terminal_columns():
    text = render_tool_result("read_file", {"path": "中文文件.txt"}, "内容很长" * 100, width=45)
    assert all(get_cwidth(line) <= 45 for line in text.splitlines())
    assert "…" in text


def test_empty_output_and_errors_stay_explicit():
    text = render_tool_result("shell", {"command": "false"}, "", error=True)
    assert "ERR" in text and "Bash(false)" in text and "(无输出)" in text


def test_safe_header_does_not_dump_written_file_contents():
    text = render_tool_result("write_file", {"path": "main.py", "content": "huge private content"}, "written")
    assert "Write(main.py)" in text
    assert "huge private content" not in text


def test_strip_terminal_escapes_and_redact_before_rendering():
    text = render_tool_result("shell", {"command": "curl -H 'Authorization: Bearer secret123'"},
                              "\x1b[31merror\x1b[0m\napi_key=secret456")
    assert "\x1b" not in text
    assert "secret123" not in text and "secret456" not in text
    assert "error" in text


@pytest.mark.parametrize("legacy", [False, True])
def test_both_cli_execution_paths_render_invocations(capsys, legacy):
    from app import cli
    from app.agent.runtime import TurnEvent
    from app.agent.events import RunEvent, TOOL_COMPLETED
    kwargs = dict(tool_name="shell", tool_input={"command": "printf test"}, tool_output="test", elapsed_seconds=.2)
    if legacy:
        cli._print_event(TurnEvent(kind="tool_result", **kwargs))
    else:
        cli._print_run_event(RunEvent(kind=TOOL_COMPLETED, **kwargs))
    text = capsys.readouterr().out
    assert "Bash(printf test)" in text and "⎿ test" in text


def test_read_only_shell_has_no_no_changes_noise(monkeypatch, capsys):
    from app import cli
    import app.util.diff as diff
    monkeypatch.setattr(cli, "_SHELL_TREE_BEFORE", {})
    monkeypatch.setattr(diff, "snapshot_tree", lambda root: {})
    monkeypatch.setattr(diff, "diff_snapshots", lambda before, after: [])
    cli._render_shell_diff()
    assert capsys.readouterr().out == ""
