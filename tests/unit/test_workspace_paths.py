"""离线测试：文件工具的 workspace 路径限制。"""
from pathlib import Path

import pytest

from app.tools.builtin.files import (
    _list_dir,
    _read_file,
    _resolve_within_workspace,
    _write_file,
    WorkspacePathError,
)


def test_resolve_within_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "sub" / "file.txt"
    resolved = _resolve_within_workspace(str(target))
    assert resolved == target.resolve()


def test_resolve_outside_workspace_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    with pytest.raises(WorkspacePathError):
        _resolve_within_workspace("/etc/passwd")


def test_read_file_outside_returns_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    out = _read_file({"path": "/etc/passwd"})
    assert out.startswith("refused:"), out
    assert "outside workspace" in out


def test_write_file_outside_returns_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    out = _write_file({"path": "/tmp/agentlab_should_not_exist.txt", "content": "x"})
    assert out.startswith("refused:"), out
    assert not Path("/tmp/agentlab_should_not_exist.txt").exists()


def test_list_dir_outside_returns_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    out = _list_dir({"path": "/etc"})
    assert out.startswith("refused:"), out


def test_read_write_round_trip_within_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "hello.txt"
    write_out = _write_file({"path": str(target), "content": "hi"})
    assert write_out.startswith("wrote 2 chars")
    read_out = _read_file({"path": str(target)})
    assert read_out == "hi"


def test_list_dir_within_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    out = _list_dir({"path": str(tmp_path)})
    assert "a.txt" in out
    assert "sub/" in out
