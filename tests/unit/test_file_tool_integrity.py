"""文件工具的数据完整性回归测试。

覆盖三类历史上会静默损坏用户文件/误导模型的缺陷:
  1. 换行符与编码:CRLF 不能被规范化成 LF,非 UTF-8 不能替换后写回
  2. 写入原子性:中途失败不能留下截断的半成品文件
  3. 输出有界:read_file 分页、list_dir 计数,避免被外层整体截断
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.tools.builtin.files import (
    DEFAULT_READ_LINES,
    EDIT_FILE,
    LIST_DIR,
    MAX_LIST_ENTRIES,
    MAX_READ_CHARS,
    READ_FILE,
    WRITE_FILE,
    _edit_file,
    _list_dir,
    _read_file,
    _write_file,
)
from app.tools.registry import ToolExecutionError, ToolRegistry


# ── 1. 换行符保留 ────────────────────────────────────────────────────────────

def test_edit_file_preserves_crlf(monkeypatch, tmp_path):
    """只改一行不能把整个 Windows 检出文件的换行符重写成 LF。"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    path = tmp_path / "crlf.py"
    path.write_bytes(b"def a():\r\n    return 1\r\n")

    _edit_file({"path": str(path), "old_str": "return 1", "new_str": "return 2"})

    assert path.read_bytes() == b"def a():\r\n    return 2\r\n"


def test_edit_file_matches_lf_old_str_against_crlf_file(monkeypatch, tmp_path):
    """模型给 LF 版 old_str 时按文件风格匹配,而不是陷入永远找不到的重试。"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    path = tmp_path / "crlf.py"
    path.write_bytes(b"def a():\r\n    return 1\r\n")

    out = _edit_file({
        "path": str(path),
        "old_str": "def a():\n    return 1",
        "new_str": "def a():\n    return 3",
    })

    assert "CRLF" in out
    assert path.read_bytes() == b"def a():\r\n    return 3\r\n"


def test_write_file_does_not_normalize_crlf(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    path = tmp_path / "win.txt"

    _write_file({"path": str(path), "content": "a\r\nb\r\n"})

    assert path.read_bytes() == b"a\r\nb\r\n"


# ── 2. 非 UTF-8 拒绝而非损坏 ─────────────────────────────────────────────────

def test_edit_file_refuses_non_utf8_instead_of_corrupting(monkeypatch, tmp_path):
    """用 U+FFFD 替换后写回会永久损坏原字节,必须拒绝。"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    path = tmp_path / "latin1.txt"
    original = b"caf\xe9 au lait"
    path.write_bytes(original)

    with pytest.raises(ToolExecutionError) as exc:
        _edit_file({"path": str(path), "old_str": "caf", "new_str": "CAF"})

    assert "UTF-8" in str(exc.value)
    assert path.read_bytes() == original  # 原字节一字未动


def test_read_file_replaces_non_utf8_without_error(monkeypatch, tmp_path):
    """只读展示可以有损,但不能抛错。"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    path = tmp_path / "latin1.txt"
    path.write_bytes(b"caf\xe9")

    out = _read_file({"path": str(path)})

    assert "caf" in out


# ── 3. 原子写入 ──────────────────────────────────────────────────────────────

def test_write_failure_leaves_original_intact(monkeypatch, tmp_path):
    """写入中途失败时,磁盘上必须仍是完整的旧内容,不能是半截文件。"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    path = tmp_path / "keep.txt"
    path.write_text("original", encoding="utf-8")

    from app.tools.builtin import files as files_module

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(files_module.os, "replace", explode)

    with pytest.raises(OSError, match="disk full"):
        _write_file({"path": str(path), "content": "replacement"})

    assert path.read_text(encoding="utf-8") == "original"
    # 临时文件必须被清理,不能在 workspace 里留下垃圾。
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]


def test_write_creates_missing_parents(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    path = tmp_path / "a" / "b" / "c.txt"

    _write_file({"path": str(path), "content": "x"})

    assert path.read_text(encoding="utf-8") == "x"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o600, 0o640, 0o755])
def test_atomic_write_preserves_permissions_before_content_is_written(monkeypatch, tmp_path, mode):
    import stat
    from app.tools.builtin import files

    path = tmp_path / "private.env"
    path.write_text("old", encoding="utf-8")
    path.chmod(mode)
    original_open = files.os.open
    original_fsync = files.os.fsync
    requested_modes = []
    observed = []

    def observe_open(name, flags, requested_mode):
        requested_modes.append(requested_mode)
        return original_open(name, flags, requested_mode)

    def observe_fsync(fd):
        sibling, = tmp_path.glob(".*.tmp")
        observed.append((stat.S_IMODE(sibling.stat().st_mode), sibling.read_text()))
        original_fsync(fd)

    monkeypatch.setattr(files.os, "open", observe_open)
    monkeypatch.setattr(files.os, "fsync", observe_fsync)
    files.atomic_write_text(path, "new secret")
    assert requested_modes == [mode]
    assert observed == [(mode, "new secret")]
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert path.read_text() == "new secret"


def test_permission_failure_aborts_atomic_write_and_cleans_up(monkeypatch, tmp_path):
    from app.tools.builtin import files

    path = tmp_path / "private.env"
    path.write_text("original", encoding="utf-8")
    observed_contents = []

    def refuse_chmod(sibling, mode):
        observed_contents.append(sibling.read_bytes())
        raise PermissionError("permission setup failed")

    monkeypatch.setattr(files.os, "chmod", refuse_chmod)
    with pytest.raises(PermissionError, match="permission setup failed"):
        files.atomic_write_text(path, "secret replacement")
    assert observed_contents == [b""]
    assert path.read_text() == "original"
    assert list(tmp_path.iterdir()) == [path]


# ── 4. 输出有界 ──────────────────────────────────────────────────────────────

def test_read_file_pages_large_file(monkeypatch, tmp_path):
    """大文件按页返回,不会整个读进内存,也不会被外层截断。"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    path = tmp_path / "big.txt"
    path.write_text("".join(f"line {i}\n" for i in range(1, 1001)), encoding="utf-8")

    first = _read_file({"path": str(path)})
    assert "line 1\n" in first
    assert "line 1000\n" not in first
    assert len(first) <= MAX_READ_CHARS + 500  # 页脚开销之外不超预算
    # 页脚必须给出续读位置。
    assert f"offset={DEFAULT_READ_LINES + 1}" in first

    second = _read_file({"path": str(path), "offset": DEFAULT_READ_LINES + 1})
    assert second.startswith(f"{path} (lines {DEFAULT_READ_LINES + 1}-")
    assert f"line {DEFAULT_READ_LINES + 1}\n" in second
    assert "line 1000\n" not in second

    # 一路翻到最后一页,确认能完整读到文件末尾。
    offset = DEFAULT_READ_LINES + 1
    page = second
    for _ in range(10):
        marker = "继续请传 offset="
        if marker not in page:
            break
        offset = int(page.rsplit(marker, 1)[1].rstrip("]\n"))
        page = _read_file({"path": str(path), "offset": offset})
    assert "line 1000\n" in page


def test_read_file_small_file_is_unchanged(monkeypatch, tmp_path):
    """小文件不分页,返回值保持原样(向后兼容)。"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    path = tmp_path / "small.txt"
    path.write_text("hello", encoding="utf-8")

    assert _read_file({"path": str(path)}) == "hello"


def test_read_file_refuses_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    path = tmp_path / "image.bin"
    path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    with pytest.raises(ToolExecutionError) as exc:
        _read_file({"path": str(path)})

    assert "二进制" in str(exc.value)


def test_list_dir_caps_huge_directory(monkeypatch, tmp_path):
    """超大目录只返回前 N 条并说明剩余数量,而不是被外层截断后中间静默消失。"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    for i in range(MAX_LIST_ENTRIES + 25):
        (tmp_path / f"f{i:04d}.txt").touch()

    out = _list_dir({"path": str(tmp_path)})

    assert f"还有 25 个条目未显示" in out
    assert out.count(".txt") == MAX_LIST_ENTRIES


# ── 5. 拒绝语义 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool,args", [
    (READ_FILE, {"path": "../outside.txt"}),
    (WRITE_FILE, {"path": "../outside.txt", "content": "x"}),
    (LIST_DIR, {"path": ".."}),
    (EDIT_FILE, {"path": "../outside.txt", "old_str": "a", "new_str": "b"}),
])
def test_outside_workspace_is_an_error_not_a_success(tool, args, monkeypatch, tmp_path):
    """拒绝必须是 is_error=True,否则会混进"成功工具证据"里。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    registry = ToolRegistry()
    registry.register(tool)

    output, is_error = registry.execute(tool.name, args)

    assert is_error is True
    assert output.startswith(("refused:", "approval required:")), output


def test_code_search_empty_query_is_an_error():
    """空查询在 registry 路径下可达,必须计为错误。"""
    from app.tools.builtin.code_search import CODE_SEARCH

    registry = ToolRegistry()
    registry.register(CODE_SEARCH)

    output, is_error = registry.execute("code_search", {"query": "  "})

    assert is_error is True
    assert output.startswith("refused:")


# ── 6. 校验器缓存 ────────────────────────────────────────────────────────────

def test_validator_is_compiled_once_and_still_validates():
    tool = ToolRegistry()
    tool.register(WRITE_FILE)

    assert WRITE_FILE._validator is not None
    assert WRITE_FILE._validator is WRITE_FILE._validator  # 构造期缓存,不是每次重建
    assert WRITE_FILE.validate_arguments({"path": "x", "content": "y"}) is None
    error = WRITE_FILE.validate_arguments({"path": "x"})  # 缺必填 content
    assert error is not None and "required" in error


def test_invalid_schema_is_refused():
    from app.tools.registry import Tool

    broken = Tool(
        name="broken",
        description="broken schema",
        input_schema={"type": "not-a-real-type"},
        executor=lambda args: "should not run",
    )
    assert broken._validator is None
    assert broken.validate_arguments({}) == "invalid tool schema: execution refused"
