import json
from pathlib import Path

import pytest

from app.tools.rtk_builtin import BuiltinRTK, classify_command
from app.tools.output_archive import archive_output
from app.tools.builtin.files import _read_page, _list_dir


@pytest.mark.parametrize("command,category", [
    ("env CI=1 /usr/bin/python3 -m pytest -q", "pytest"),
    ("rtk proxy bash -lc 'git -C /tmp --no-pager diff'", "git_diff"),
    ("command /usr/bin/rg TODO src", "grep"),
    ("/bin/sh -c 'ls -la'", "listing"),
    ("sed -n '20,60p' main.cpp", "read"),
    ("echo 'pytest git diff'", "generic"),
    ("python3 -c 'print(\"pytest passed\")'", "generic"),
    ("git status; cat critical.log", "generic"),
    ("pytest && cat evidence.txt", "generic"),
    ("rg TODO | head -20", "generic"),
    ("ssh host 'pytest -q'", "generic"),
])
def test_wrappers_and_compounds_are_classified_without_executing(command, category):
    assert classify_command(command) == category


def test_mixed_output_never_becomes_test_success_summary():
    raw = "tests passed\n" + "important source content\n" * 20
    result = BuiltinRTK().compress("pytest; cat source.cpp", raw, "", 0)
    assert "important source content" in result.output


def test_terminal_partial_logs_are_not_fabricated_success(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    raw = "collecting tests\n" + "progress\n" * 100 + "Enter password: "
    out = BuiltinRTK().compress_terminal("pytest -q", raw)
    assert "tests passed" not in out
    assert "Enter password:" in out
    saved = list((tmp_path / ".agentlab/tool-output").glob("*.txt"))
    assert len(saved) == 1 and saved[0].read_text() == raw


def test_archive_is_immutable_and_rejects_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    path = archive_output("complete output\n")
    assert archive_output("complete output\n") == path
    assert (tmp_path / path).read_text() == "complete output\n"
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(other))
    (other / ".agentlab").symlink_to(tmp_path / ".agentlab", target_is_directory=True)
    assert archive_output("cannot escape") is None


def test_archive_expires_after_one_day_and_reuse_refreshes_age(tmp_path, monkeypatch):
    import os
    import time
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    expired = tmp_path / archive_output("expired")
    reused = tmp_path / archive_output("reused")
    recent = tmp_path / archive_output("recent")
    old = time.time() - 86401
    for path in (expired, reused):
        os.utime(path, (old, old))
    other = expired.parent / "keep-not-an-archive.txt"
    other.write_text("untouched")
    os.utime(other, (old, old))
    archive_output("reused")
    assert not expired.exists()
    assert reused.read_text() == "reused" and reused.stat().st_mtime > old
    assert recent.exists() and other.exists()


def test_source_long_lines_can_be_read_without_losing_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    source = "x" * 19000 + "\nshort line\n"
    (tmp_path / "source.cpp").write_text(source)
    offset, column = 1, 0
    chunks = []
    for _ in range(10):
        _, page = _read_page({"path": "source.cpp", "offset": offset, "column_offset": column})
        chunks.append(page["content"])
        if not page["truncated"]:
            break
        offset, column = page["next_offset"], page["next_column"] or 0
    assert "".join(chunks) == source


def test_directory_pagination_does_not_skip_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    for i in range(125):
        (tmp_path / f"f{i:03d}.txt").touch()
    outputs = [_list_dir({"path": ".", "offset": offset}) for offset in (0, 60, 120)]
    files = [line for text in outputs for line in text.splitlines() if line.startswith("f")]
    assert len(files) == len(set(files)) == 125
    assert "offset=60" in outputs[0] and "offset=120" in outputs[1]


def test_shell_retains_full_output_before_filtering(tmp_path, monkeypatch):
    import re
    from types import SimpleNamespace
    import app.tools.builtin.shell as shell
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTLAB_RTK_ENABLED", "true")
    raw = "collected 500 items\n" + "." * 2000 + "\n500 passed in 2s\n"
    monkeypatch.setattr(shell, "run_process", lambda *a, **k: SimpleNamespace(stdout=raw, stderr="", returncode=0))
    out = shell._run_shell({"command": "python3 -m pytest -q"})
    assert "500 passed" in out and "[exit code: 0]" in out
    assert len(out) < len(raw)
    path = re.search(r"完整输出: ([^；]+)", out).group(1)
    assert (tmp_path / path).read_text() == raw


def test_long_source_footer_continues_same_line(tmp_path, monkeypatch):
    from app.tools.builtin.files import _read_file
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "long.cpp").write_text("z" * 20000)
    result = _read_file({"path": "long.cpp"})
    assert "继续请传 offset=1 column_offset=9000" in result
    assert "本行已截断" not in result
