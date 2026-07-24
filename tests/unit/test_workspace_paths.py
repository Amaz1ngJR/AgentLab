"""离线测试：文件工具的 workspace 路径限制。"""
from pathlib import Path

import pytest

from app.config.loader import use_workspace_root
from app.tools.builtin.files import (
    EDIT_FILE,
    LIST_DIR,
    READ_FILE,
    WRITE_FILE,
    _list_dir,
    _read_file,
    _resolve_within_workspace,
    _write_file,
    WorkspacePathError,
)
from app.tools.registry import ToolRegistry


def test_resolve_within_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "sub" / "file.txt"
    resolved = _resolve_within_workspace(str(target))
    assert resolved == target.resolve()


def test_relative_path_is_based_on_workspace_override(monkeypatch, tmp_path):
    """Loop 切换 workspace 后，相对路径不能继续按主进程 CWD 解析。"""
    monkeypatch.chdir(tmp_path.parent)
    with use_workspace_root(tmp_path):
        out = _write_file({"path": "nested/hello.txt", "content": "hi"})
        resolved = _resolve_within_workspace("nested/hello.txt")

    assert out.startswith("wrote 2 chars")
    assert resolved == (tmp_path / "nested" / "hello.txt").resolve()
    assert (tmp_path / "nested" / "hello.txt").read_text() == "hi"


def test_resolve_outside_workspace_raises(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    with pytest.raises(WorkspacePathError):
        _resolve_within_workspace(str(tmp_path / "outside.txt"))


def test_read_file_outside_returns_refused(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("external")
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    out = _read_file({"path": str(outside)})
    assert out.startswith("refused:"), out
    assert "outside workspace" in out


def test_write_file_outside_returns_refused(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    out = _write_file({"path": str(outside), "content": "x"})
    assert out.startswith("refused:"), out
    assert not outside.exists()


def test_list_dir_outside_returns_refused(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    out = _list_dir({"path": str(outside)})
    assert out.startswith("refused:"), out


def test_registry_requires_matching_approval_for_outside_read(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("external")
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    registry = ToolRegistry()
    registry.register(READ_FILE)
    args = {"path": str(outside)}

    denied, is_error = registry.execute("read_file", args)
    assert is_error is True
    assert denied == "approval required: read_file_outside_workspace"

    output, is_error = registry.execute(
        "read_file",
        args,
        approved_action="read_file_outside_workspace",
    )
    assert is_error is False
    assert output == "external"


def test_registry_allows_approved_outside_write_and_list(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    registry = ToolRegistry()
    registry.register(WRITE_FILE)
    registry.register(EDIT_FILE)
    registry.register(LIST_DIR)
    target = outside / "created.txt"

    write_output, write_error = registry.execute(
        "write_file",
        {"path": str(target), "content": "created"},
        approved_action="write_file_outside_workspace",
    )
    assert write_error is False
    assert write_output.startswith("wrote 7 chars")
    assert target.read_text() == "created"

    edit_output, edit_error = registry.execute(
        "edit_file",
        {"path": str(target), "old_str": "created", "new_str": "edited"},
        approved_action="edit_file_outside_workspace",
    )
    assert edit_error is False
    assert edit_output.startswith("edited ")
    assert target.read_text() == "edited"

    list_output, list_error = registry.execute(
        "list_dir",
        {"path": str(outside)},
        approved_action="list_dir_outside_workspace",
    )
    assert list_error is False
    assert "created.txt" in list_output


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
