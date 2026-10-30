"""离线测试:code_search 工具。

覆盖 technical_architecture.md §7.7.6 的测试要求:
  text/regex/file/symbol 四种模式、workspace 越界拒绝、.gitignore 与默认忽略目录、
  rg 可用与 fallback、max_results、context_lines、timeout、二进制跳过、疑似密钥脱敏。

Python fallback 用显式 mock 强制选择；rg 流式读取同时使用模拟子进程和真实 rg 验证。
"""
import json
import subprocess
import io

import pytest

from app.tools.builtin import code_search
from app.tools.builtin.code_search import (
    CODE_SEARCH,
    _build_symbol_pattern,
    _code_search,
)
from app.tools.registry import ToolExecutionError, ToolRegistry


def _force_python_backend(monkeypatch):
    """强制走 Python fallback,让测试结果不依赖机器上是否装了 rg。"""
    monkeypatch.setattr(code_search, "_find_ripgrep", lambda: None)


def _search(monkeypatch, tmp_path, **args):
    """设好 workspace 后调用工具,成功时返回解析后的 dict。

    拒绝现在以 ToolExecutionError 抛出(而不是返回 "refused:" 字符串),
    这里统一转回字符串,既保留旧断言的表达力,也顺带验证拒绝确实是错误。
    """
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    try:
        out = _code_search(args)
    except ToolExecutionError as exc:
        return str(exc)
    if out.startswith(("error:", "path not found:")):
        return out
    return json.loads(out)


# ── 四种模式 ─────────────────────────────────────────────────────────────────

def test_text_mode_finds_literal(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    (tmp_path / "a.py").write_text("def hello():\n    return 'world'\n")
    result = _search(monkeypatch, tmp_path, query="hello", mode="text")
    assert result["count"] == 1
    m = result["matches"][0]
    assert m["path"] == "a.py"
    assert m["line"] == 1
    assert m["column"] == 5  # 'hello' 在 'def hello' 第 5 列
    assert m["kind"] == "text"


def test_regex_mode(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    (tmp_path / "a.py").write_text("x = 123\ny = 4567\nz = abc\n")
    result = _search(monkeypatch, tmp_path, query=r"\d{4}", mode="regex")
    assert result["count"] == 1
    assert result["matches"][0]["line"] == 2  # 只有 4567 是 4 位数字


def test_file_mode_matches_path(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    (tmp_path / "config.yaml").write_text("a: 1")
    (tmp_path / "main.py").write_text("x = 1")
    result = _search(monkeypatch, tmp_path, query="config", mode="file")
    paths = [m["path"] for m in result["matches"]]
    assert "config.yaml" in paths
    assert "main.py" not in paths
    assert result["matches"][0]["line"] == 0  # file 模式无行号
    assert result["matches"][0]["kind"] == "file"


def test_file_mode_glob(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    result = _search(monkeypatch, tmp_path, query="anything", mode="file", glob="*.py")
    paths = [m["path"] for m in result["matches"]]
    assert paths == ["a.py"]


def test_symbol_mode_matches_definition_not_usage(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    # 第 1 行是定义,第 4 行只是调用,symbol 模式应只命中定义
    (tmp_path / "a.py").write_text(
        "def compute():\n"
        "    return 1\n"
        "\n"
        "result = compute()\n"
    )
    result = _search(monkeypatch, tmp_path, query="compute", mode="symbol")
    assert result["count"] == 1
    assert result["matches"][0]["line"] == 1
    assert result["matches"][0]["kind"] == "symbol"


def test_symbol_pattern_covers_keywords():
    """symbol 正则应覆盖 def/class/const 等定义,不匹配单纯出现。"""
    import re

    pat = re.compile(_build_symbol_pattern("Foo"))
    assert pat.search("class Foo:")
    assert pat.search("const Foo = 1")
    assert pat.search("Foo = bar")
    assert pat.search("Foo: int")
    assert not pat.search("    return otherFoo(1)")  # 词边界,不匹配子串


# ── workspace 越界 ───────────────────────────────────────────────────────────

def test_workspace_out_of_bounds_refused(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    with pytest.raises(ToolExecutionError) as exc:
        _code_search({"query": "x", "path": "/etc"})
    assert str(exc.value).startswith("refused:")
    assert "outside workspace" in str(exc.value)


def test_workspace_out_of_bounds_allowed_after_approval(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "external.py").write_text("def approved_symbol():\n    pass\n")
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    args = {"query": "approved_symbol", "path": str(outside)}
    registry = ToolRegistry()
    registry.register(CODE_SEARCH)

    output, is_error = registry.execute(
        "code_search",
        args,
        approved_action="code_search_outside_workspace",
    )

    assert is_error is False
    parsed = json.loads(output)
    assert parsed["count"] == 1
    assert parsed["matches"][0]["path"].endswith("external.py")


def test_empty_query_refused():
    with pytest.raises(ToolExecutionError) as exc:
        _code_search({"query": "  "})
    assert str(exc.value).startswith("refused:")


def test_unknown_mode_refused(monkeypatch, tmp_path):
    out = _search(monkeypatch, tmp_path, query="x", mode="bogus")
    assert out.startswith("refused:")
    assert "unknown mode" in out


def test_invalid_regex_refused(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    out = _search(monkeypatch, tmp_path, query="(unclosed", mode="regex")
    assert out.startswith("refused:")
    assert "invalid regex" in out


# ── .gitignore 与默认忽略目录 ─────────────────────────────────────────────────

def test_gitignore_respected(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    (tmp_path / "ignored.py").write_text("TARGET = 1\n")
    (tmp_path / "kept.py").write_text("TARGET = 2\n")
    result = _search(monkeypatch, tmp_path, query="TARGET", mode="text")
    paths = [m["path"] for m in result["matches"]]
    assert paths == ["kept.py"]


def test_default_ignore_dirs_skipped(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "lib.js").write_text("NEEDLE = 1\n")
    (tmp_path / "app.js").write_text("NEEDLE = 2\n")
    result = _search(monkeypatch, tmp_path, query="NEEDLE", mode="text")
    paths = [m["path"] for m in result["matches"]]
    assert paths == ["app.js"]


# ── rg 路径与 fallback ───────────────────────────────────────────────────────

def test_ripgrep_backend_parses_json(monkeypatch, tmp_path):
    """mock rg --json 输出,验证解析、列号重算、相对路径与 backend 标记。"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "src" / "mod.py"
    target.parent.mkdir()
    target.write_text("alpha = 1\nbeta = call_thing()\ngamma = 3\n")

    rg_json = "\n".join([
        json.dumps({"type": "begin", "data": {"path": {"text": str(target)}}}),
        json.dumps({"type": "match", "data": {
            "path": {"text": str(target)},
            "lines": {"text": "beta = call_thing()\n"},
            "line_number": 2,
        }}),
        json.dumps({"type": "end", "data": {}}),
    ])

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.stdout = io.BytesIO(rg_json.encode())
            self.returncode = 0
        def poll(self): return self.returncode
        def wait(self): return self.returncode
        def kill(self): self.returncode = -9

    monkeypatch.setattr(code_search, "_find_ripgrep", lambda: "/fake/rg")
    monkeypatch.setattr(subprocess, "Popen", FakeProcess)

    out = json.loads(_code_search({"query": "call_thing", "mode": "text"}))
    assert out["summary"]["backend"] == "ripgrep"
    assert out["count"] == 1
    m = out["matches"][0]
    assert m["path"] == "src/mod.py"
    assert m["line"] == 2
    assert m["column"] == 8  # 'call_thing' 在 'beta = call_thing' 第 8 列


def test_fallback_used_when_no_ripgrep(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    (tmp_path / "a.py").write_text("NEEDLE\n")
    result = _search(monkeypatch, tmp_path, query="NEEDLE", mode="text")
    assert result["summary"]["backend"] == "python"


def test_ripgrep_timeout_marks_truncated(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "a.py").write_text("x = 1\n")

    monkeypatch.setattr(code_search, "_find_ripgrep", lambda: "/fake/rg")
    monkeypatch.setattr(code_search, "_run_ripgrep", lambda *args: ([], True, False))

    out = json.loads(_code_search({"query": "x", "mode": "text"}))
    assert out["truncated"] is True
    assert "timed out" in out["summary"]["note"]


# ── 限制:max_results / context_lines ────────────────────────────────────────

def test_max_results_truncates(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    (tmp_path / "a.py").write_text("\n".join("HIT" for _ in range(10)) + "\n")
    result = _search(monkeypatch, tmp_path, query="HIT", mode="text", max_results=3)
    assert result["count"] == 3
    assert result["truncated"] is True


def test_context_lines(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    (tmp_path / "a.py").write_text("l1\nl2\nMATCH\nl4\nl5\n")
    result = _search(monkeypatch, tmp_path, query="MATCH", mode="text", context_lines=1)
    ctx = result["matches"][0]["context"]
    assert ctx == ["2:l2", "3:MATCH", "4:l4"]  # 上下各 1 行 + 命中行


def test_context_lines_zero(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    (tmp_path / "a.py").write_text("l1\nMATCH\nl3\n")
    result = _search(monkeypatch, tmp_path, query="MATCH", mode="text", context_lines=0)
    assert result["matches"][0]["context"] == ["2:MATCH"]


# ── 二进制跳过 ───────────────────────────────────────────────────────────────

def test_binary_files_skipped(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    # 含 NUL 字节 -> 判为二进制,即使包含 query 也跳过
    (tmp_path / "blob.bin").write_bytes(b"NEEDLE\x00\x01\x02NEEDLE")
    (tmp_path / "text.py").write_text("NEEDLE\n")
    result = _search(monkeypatch, tmp_path, query="NEEDLE", mode="text")
    paths = [m["path"] for m in result["matches"]]
    assert paths == ["text.py"]
    assert result["summary"].get("skipped_binary", 0) >= 1


# ── 疑似密钥脱敏 ─────────────────────────────────────────────────────────────

def test_secret_redacted_in_preview(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    (tmp_path / "cfg.py").write_text(
        'OPENAI_KEY = "sk-abcdefghijklmnopqrstuvwxyz0123"\n'
    )
    result = _search(monkeypatch, tmp_path, query="OPENAI_KEY", mode="text")
    m = result["matches"][0]
    assert "sk-abcdefghij" not in m["preview"]
    assert "sk-***" in m["preview"]
    # context 行也应脱敏
    assert all("sk-abcdefghij" not in line for line in m["context"])


def test_bearer_token_redacted(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    (tmp_path / "h.py").write_text('HEADER = "Bearer abc123def456ghi789"\n')
    result = _search(monkeypatch, tmp_path, query="HEADER", mode="text")
    assert "abc123def456" not in result["matches"][0]["preview"]


# ── 工具定义 ─────────────────────────────────────────────────────────────────

def test_tool_is_read_only():
    """code_search 风险等级 read,不需审批(§7.7.6)。"""
    assert CODE_SEARCH.requires_approval is False
    assert CODE_SEARCH.name == "code_search"


def test_path_not_found(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    out = _search(monkeypatch, tmp_path, query="x", path="nonexistent_subdir")
    assert out.startswith("path not found:")


@pytest.mark.parametrize("mode", ["text", "file"])
def test_recursive_glob_includes_direct_and_nested_children(monkeypatch, tmp_path, mode):
    _force_python_backend(monkeypatch)
    for name in ("app/agent/direct.py", "app/agent/nested/deep.py", "app/other.py", "app/agent/no.txt"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("needle\n")
    result = _search(monkeypatch, tmp_path, query="needle", mode=mode, glob="app/agent/**/*.py")
    assert {m["path"] for m in result["matches"]} == {"app/agent/direct.py", "app/agent/nested/deep.py"}
    result = _search(monkeypatch, tmp_path, query="needle", mode=mode, glob="app/agent/*.py")
    assert [m["path"] for m in result["matches"]] == ["app/agent/direct.py"]


@pytest.mark.parametrize("glob,paths", [
    ("*.py", {"a.py", "src/b.py"}),
    ("**/*.py", {"a.py", "src/b.py"}),
    ("src/*.{py,ts}", {"src/b.py", "src/c.ts"}),
    ("src/[bc].?s", {"src/c.ts"}),
    ("!**/*.py", {"src/c.ts", "src/nested/d.txt"}),
])
def test_glob_shapes_do_not_cross_single_star_directory_boundaries(glob, paths):
    candidates = {"a.py", "src/b.py", "src/c.ts", "src/nested/d.txt"}
    assert {p for p in candidates if code_search._glob_matches(p, glob)} == paths


def test_fallback_glob_matches_real_rg(monkeypatch, tmp_path):
    rg = code_search._find_ripgrep()
    if rg is None:
        pytest.skip("ripgrep not installed; deterministic glob cases tested separately")
    for name in ("app/agent/a.py", "app/agent/nested/b.py", "app/agent/c.ts", "app/d.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("needle\n")
    for glob in ("app/agent/**/*.py", "app/agent/*.py", "*.py", "app/agent/*.{py,ts}"):
        monkeypatch.setattr(code_search, "_find_ripgrep", lambda: rg)
        native = _search(monkeypatch, tmp_path, query="needle", glob=glob)
        _force_python_backend(monkeypatch)
        fallback = _search(monkeypatch, tmp_path, query="needle", glob=glob)
        assert {m["path"] for m in native["matches"]} == {m["path"] for m in fallback["matches"]}


def _rg_event(path, text="needle", line=1):
    return json.dumps({"type": "match", "data": {
        "path": {"text": str(path)}, "lines": {"text": text}, "line_number": line,
    }}) + "\n"


def test_rg_stops_running_process_after_global_match_limit(monkeypatch, tmp_path):
    import sys
    real_popen = subprocess.Popen
    processes = []
    payload = _rg_event(tmp_path / "a.py")
    script = "import sys,time\nfor i in range(1000000):\n sys.stdout.write(" + repr(payload) + ")\n sys.stdout.flush()\n time.sleep(.001)\n"
    def launch(argv, **kwargs):
        process = real_popen([sys.executable, "-u", "-c", script], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", launch)
    lines, timed_out, limited = code_search._run_ripgrep("rg", tmp_path, "needle", "text", None, False, 3, 2)
    assert len(lines) == 3 and limited and not timed_out
    assert processes[0].poll() is not None


def test_rg_output_byte_limit_also_bounds_non_matching_symbol_uses(monkeypatch, tmp_path):
    class Process:
        def __init__(self, *args, **kwargs):
            self.stdout = io.BytesIO((_rg_event(tmp_path / "a.py", "foo()" * 1000) * 1000).encode())
            self.returncode = None
        def poll(self): return self.returncode
        def kill(self): self.returncode = -9
        def wait(self): self.returncode = self.returncode or 0
    monkeypatch.setattr(subprocess, "Popen", Process)
    lines, timeout, limited = code_search._run_ripgrep("rg", tmp_path, "foo", "symbol", None, False, 3, 1)
    assert lines == [] and limited and not timeout


def test_rg_symbol_limit_counts_definitions_after_filtering(monkeypatch, tmp_path):
    payload = "".join(_rg_event(tmp_path / "a.py", "foo()", n) for n in range(20))
    payload += _rg_event(tmp_path / "a.py", "def foo():", 21)
    class Process:
        def __init__(self, *args, **kwargs):
            self.stdout = io.BytesIO(payload.encode())
            self.returncode = 0
        def poll(self): return self.returncode
        def wait(self): return self.returncode
    monkeypatch.setattr(subprocess, "Popen", Process)
    lines, timeout, limited = code_search._run_ripgrep("rg", tmp_path, "foo", "symbol", None, False, 3, 1)
    assert len(lines) == 1 and not limited and not timeout


def test_rg_timeout_kills_and_reaps_process(monkeypatch, tmp_path):
    import sys
    real_popen = subprocess.Popen
    processes = []
    def launch(argv, **kwargs):
        process = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", launch)
    _, timeout, limited = code_search._run_ripgrep("rg", tmp_path, "needle", "text", None, False, .05, 2)
    assert timeout and not limited
    assert processes[0].poll() is not None


@pytest.mark.parametrize("path", ["src", "src/a.py"])
def test_search_subroot_returns_workspace_relative_paths_in_both_backends(monkeypatch, tmp_path, path):
    target = tmp_path / "src" / "a.py"
    target.parent.mkdir()
    target.write_text("needle\n")
    rg = code_search._find_ripgrep()
    backends = [None, rg] if rg else [None]
    for backend in backends:
        monkeypatch.setattr(code_search, "_find_ripgrep", lambda: backend)
        result = _search(monkeypatch, tmp_path, query="needle", path=path, glob="*.py")
        assert [m["path"] for m in result["matches"]] == ["src/a.py"]


def test_serialized_chinese_results_respect_byte_limit(monkeypatch, tmp_path):
    _force_python_backend(monkeypatch)
    (tmp_path / "a.py").write_text(("命中" + "中文" * 100 + "\n") * 100)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    output = _code_search({"query": "命中", "max_results": 100, "context_lines": 2})
    assert len(output.encode("utf-8")) <= code_search.MAX_OUTPUT_BYTES
    assert json.loads(output)["truncated"]
