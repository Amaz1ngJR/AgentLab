"""离线测试:文件改动 diff 渲染(app/util/diff.py)。"""
import re

from app.util import diff


def _plain(s: str) -> str:
    """剥掉 ANSI 颜色码,方便断言可见内容。"""
    return re.sub(r"\033\[[0-9;]*m", "", s)


def test_diff_stats_replace():
    old = "a\nb\nc"
    new = "a\nB\nc\nd"
    added, removed = diff.diff_stats(old, new)
    assert (added, removed) == (2, 1)  # b->B 改 1 行 + 新增 d


def test_diff_stats_pure_insert():
    added, removed = diff.diff_stats("x\ny", "x\nNEW\ny")
    assert (added, removed) == (1, 0)


def test_diff_stats_no_change():
    assert diff.diff_stats("same", "same") == (0, 0)


def test_format_stats():
    assert diff.format_stats(6, 1) == "新增 6 行、删除 1 行"
    assert diff.format_stats(0, 0) == "无实际改动"
    assert diff.format_stats(3, 0) == "新增 3 行"


def test_render_header_create_vs_update():
    h_new = _plain(diff.render_header("a.py", True, 3, 0))
    assert "Create(a.py)" in h_new and "新增 3 行" in h_new
    h_upd = _plain(diff.render_header("a.py", False, 1, 1))
    assert "Update(a.py)" in h_upd


def test_render_color_diff_markers():
    body = _plain(diff.render_color_diff("a\nb\nc", "a\nB\nc", width=40))
    lines = body.split("\n")
    # 删除行带 -,新增行带 +,上下文行不带
    assert any(" - b" in ln for ln in lines)
    assert any(" + B" in ln for ln in lines)
    assert any("a" in ln and "-" not in ln and "+" not in ln for ln in lines)


def test_render_color_diff_line_numbers_aligned():
    body = _plain(diff.render_color_diff("l1\nl2", "l1\nl2\nl3", width=40))
    # 行号列右对齐,新增行 l3 应带行号 3
    assert any(ln.strip().startswith("3 +") for ln in body.split("\n"))


def test_render_color_diff_truncates():
    old = "\n".join(f"old{i}" for i in range(500))
    new = "\n".join(f"new{i}" for i in range(500))
    body = diff.render_color_diff(old, new, width=40, max_lines=20)
    assert "diff 过长已截断" in _plain(body)


def test_render_color_diff_bg_pads_to_width():
    # 新增/删除行去色后可见宽度应铺满 width(ASCII 场景精确)
    body = diff.render_color_diff("a", "b", width=25)
    for ln in body.split("\n"):
        plain = _plain(ln)
        if " - " in plain or " + " in plain:
            assert diff._vis_len(plain) == 25


def test_render_color_diff_folds_unchanged_head():
    # 末尾改动:前面 76 行不该整个打印,只留 context 行 + 折叠提示
    old = "\n".join([f"line{i}" for i in range(1, 81)])
    new = "\n".join([f"line{i}" for i in range(1, 77)])  # 删掉末尾 4 行
    body = _plain(diff.render_color_diff(old, new, width=60, context=3))
    lines = body.split("\n")
    # 折叠提示存在,且总行数远小于 80(context 3 + 删除 4 + 折叠提示 1 ≈ 8)
    assert any("未改动" in ln for ln in lines)
    assert len(lines) < 15
    # 开头的 line1..line70 不该出现
    assert not any(ln.strip().startswith("1 ") and "line1" in ln for ln in lines)
    # 改动附近的 line74/75/76 应作为上下文出现
    assert any("line76" in ln for ln in lines)


def test_render_color_diff_folds_both_sides_for_middle_change():
    old = "\n".join([f"x{i}" for i in range(30)])
    new = old.replace("x15", "x15_CHANGED")
    body = _plain(diff.render_color_diff(old, new, width=40, context=3))
    # 中间改动:上下都该有折叠提示
    assert body.count("未改动") == 2
    # x0 / x29 这种远处的行不该出现
    assert "x0\n" not in body and not body.strip().endswith("x29")


def test_render_color_diff_small_file_no_fold():
    body = _plain(diff.render_color_diff("a\nb\nc", "a\nB\nc", width=40))
    assert "未改动" not in body  # 小文件全显示,不折叠


def test_vscode_diff_disabled_by_env(monkeypatch):
    monkeypatch.setenv("AGENTLAB_VSCODE_DIFF", "0")
    assert diff.vscode_diff_available() is False


def test_vscode_diff_launch_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("AGENTLAB_VSCODE_DIFF", "0")
    # 关闭时返回 None(没有句柄),不应抛异常
    assert diff.launch_vscode_diff("old", "new", "x.py") is None


def test_close_vscode_diff_handles_none():
    # 传 None(未启用 / 启动失败)应静默,不抛异常
    diff.close_vscode_diff(None)


def test_close_vscode_diff_terminates_handle():
    # 传一个假句柄:poll() 返回 None(还在跑)则应调 terminate()
    class _FakeProc:
        def __init__(self):
            self.terminated = False
        def poll(self):
            return None
        def terminate(self):
            self.terminated = True
    p = _FakeProc()
    diff.close_vscode_diff(p)
    assert p.terminated is True


# ── shell 命令文件改动探测 ────────────────────────────────────────────────────

def test_shell_targets_append_redirect():
    cmd = "printf 'x\\n' >> /tmp/a.env && tail -3 /tmp/a.env"
    assert diff.shell_write_targets(cmd) == ["/tmp/a.env"]


def test_shell_targets_overwrite_redirect():
    assert diff.shell_write_targets("echo hi > out.txt") == ["out.txt"]


def test_shell_targets_tee_multiple():
    assert diff.shell_write_targets("cat x | tee -a a.log b.log") == ["a.log", "b.log"]


def test_shell_targets_sed_inplace():
    assert diff.shell_write_targets("sed -i 's/a/b/g' config.yaml") == ["config.yaml"]


def test_shell_targets_skips_devnull():
    assert diff.shell_write_targets("grep foo bar.txt > /dev/null") == []


def test_shell_targets_readonly_command():
    assert diff.shell_write_targets("ls -la") == []
    assert diff.shell_write_targets("cat foo.txt") == []


def test_shell_targets_empty():
    assert diff.shell_write_targets("") == []


def test_read_text_safe_missing_file():
    assert diff.read_text_safe("/nonexistent/path/xyz.txt") == ""


# ── workspace 快照(不依赖命令解析) ──────────────────────────────────────────

def test_snapshot_tree_captures_text_files(tmp_path):
    (tmp_path / "a.py").write_text("print(1)\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("hello\n")
    snap = diff.snapshot_tree(tmp_path)
    assert snap is not None
    vals = set(snap.values())
    assert "print(1)\n" in vals and "hello\n" in vals


def test_snapshot_tree_skips_git_and_pycache(tmp_path):
    (tmp_path / "keep.py").write_text("x\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "cfg").write_text("secret\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "m.pyc").write_text("bytecode\n")
    snap = diff.snapshot_tree(tmp_path)
    assert "x\n" in set(snap.values())
    assert "secret\n" not in set(snap.values())
    assert "bytecode\n" not in set(snap.values())


def test_snapshot_tree_skips_binary(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02data")
    (tmp_path / "ok.txt").write_text("ok\n")
    snap = diff.snapshot_tree(tmp_path)
    assert "ok\n" in set(snap.values())
    assert all("\x00" not in v for v in snap.values())


def test_diff_snapshots_detects_update_create_delete():
    before = {"a": "1\n2\n", "b": "keep\n", "c": "gone\n"}
    after = {"a": "1\n2\n3\n", "b": "keep\n", "d": "new\n"}
    changes = dict((path, (old, new, is_new))
                   for path, old, new, is_new in diff.diff_snapshots(before, after))
    # a 改了、c 被删、d 新建;b 没变不出现
    assert "b" not in changes
    assert changes["a"] == ("1\n2\n", "1\n2\n3\n", False)
    assert changes["c"] == ("gone\n", "", False)      # 删除:新内容为空
    assert changes["d"] == ("", "new\n", True)         # 新建


def test_diff_snapshots_none_returns_empty():
    assert diff.diff_snapshots(None, {"a": "x"}) == []
    assert diff.diff_snapshots({"a": "x"}, None) == []


def test_render_header_delete():
    h = _plain(diff.render_header("gone.txt", False, 0, 5, is_delete=True))
    assert "Delete(gone.txt)" in h and "删除 5 行" in h


