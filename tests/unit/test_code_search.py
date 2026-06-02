"""离线测试:code_search 工具。

覆盖 technical_architecture.md §7.7.6 的测试要求:
  text/regex/file/symbol 四种模式、workspace 越界拒绝、.gitignore 与默认忽略目录、
  rg 可用与 fallback、max_results、context_lines、timeout、二进制跳过、疑似密钥脱敏。

本机没有独立 rg 二进制(rg 是 shell function),所以默认走 Python fallback;
rg 路径单独用 mock subprocess + mock _find_ripgrep 验证。
"""
import json
import subprocess

import pytest

from app.tools.builtin import code_search
from app.tools.builtin.code_search import (
    CODE_SEARCH,
    _build_symbol_pattern,
    _code_search,
)


def _force_python_backend(monkeypatch):
    """强制走 Python fallback,让测试结果不依赖机器上是否装了 rg。"""
    monkeypatch.setattr(code_search, "_find_ripgrep", lambda: None)


def _search(monkeypatch, tmp_path, **args):
    """设好 workspace 后调用工具,成功时返回解析后的 dict。"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    out = _code_search(args)
    if out.startswith(("refused:", "error:", "path not found:")):
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
    out = _code_search({"query": "x", "path": "/etc"})
    assert out.startswith("refused:")
    assert "outside workspace" in out


def test_empty_query_refused():
    assert _code_search({"query": "  "}).startswith("refused:")


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

    def fake_run(argv, **kwargs):
        cp = subprocess.CompletedProcess(argv, 0, stdout=rg_json, stderr="")
        return cp

    monkeypatch.setattr(code_search, "_find_ripgrep", lambda: "/fake/rg")
    monkeypatch.setattr(subprocess, "run", fake_run)

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

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=15, output="")

    monkeypatch.setattr(code_search, "_find_ripgrep", lambda: "/fake/rg")
    monkeypatch.setattr(subprocess, "run", fake_run)

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
