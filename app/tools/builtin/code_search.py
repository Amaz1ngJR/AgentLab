"""代码搜索工具 —— Agent 的高频只读能力,定位介于 list_dir/read_file 和 shell 之间。

使用场景:
  模型想"在项目里找某个函数/字符串/文件",不应该去拼 `shell` + `grep`/`find`
  (高风险、跨平台不可控)。code_search 提供稳定、结构化、受限、低成本的代码定位:
  返回相对路径 + 行号 + 列号 + preview + context,模型拿到后再用 read_file 精读。

四种模式(见 technical_architecture.md §7.7.3):
  text   - 普通文本(固定字符串)搜索,适合函数名、错误信息、配置 key
  regex  - 正则搜索,适合复杂模式
  file   - 文件名/路径搜索,适合找配置或组件文件
  symbol - 符号(函数/类/变量定义)搜索,启发式正则

实现策略(见 §7.7.5):
  1. 优先调 ripgrep(rg):快、自动遵守 .gitignore、跨平台
  2. 无 rg 时退化为 Python 扫描
  3. 所有路径经 workspace resolver 校验,越界搜索必须先审批
  4. 搜索设 timeout,结果数 / 输出大小硬截断,避免卡住或撑爆上下文

安全(见 §7.7.6):
  - workspace 内只读搜索免审批;workspace 外搜索使用独立审批动作
  - 命中行经 redact() 脱敏,疑似密钥(sk-xxx / Bearer xxx 等)不会原样回灌给(云端)模型
"""
from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.config.loader import workspace_root
from app.tools.builtin.files import (
    WorkspacePathError,
    _outside_workspace_approval,
    _resolve_within_workspace,
)
from app.tools.registry import Tool, ToolExecutionError, current_cancel_token
from app.util.redact import redact

# ── 限制常量 ──────────────────────────────────────────────────────────────────
DEFAULT_MAX_RESULTS = 50          # 默认最多返回多少条命中
DEFAULT_CONTEXT_LINES = 2         # 默认每条命中上下各取几行
DEFAULT_TIMEOUT = 15              # rg 子进程超时(秒)
MAX_OUTPUT_BYTES = 16_000         # 序列化后 JSON 的硬上限,超出从尾部裁剪命中
MAX_FILE_BYTES = 1_000_000        # 单个文件超过 1MB 跳过(疑似数据/产物文件)
MAX_PREVIEW_CHARS = 300           # 单行 preview 截断,避免压缩行(minified)撑爆

# 即使不在 .gitignore 里也默认跳过的大目录(见 §7.7.3)。
DEFAULT_IGNORE_DIRS = [
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".agentlab", "data", ".pytest_cache", ".idea", ".vscode",
]


def _find_ripgrep() -> Optional[str]:
    """返回 rg 可执行路径;没装返回 None。抽成函数方便测试 mock 走 fallback。"""
    return shutil.which("rg")


def _resolve_search_root(path_str: str) -> Path:
    """解析搜索根目录；workspace 外路径必须已获得 code_search 越界审批。

    与 files.py 不同:相对路径基于 workspace 根解析,而不是进程 CWD。这样
    默认 path='.' 总是指 workspace 根,不受 Agent 进程实际工作目录影响。
    """
    raw = (path_str or ".").strip()
    return _resolve_within_workspace(
        raw,
        "code_search_outside_workspace",
    )


def _build_symbol_pattern(query: str) -> str:
    """为 symbol 模式构造启发式正则(见 §7.7.3)。

    覆盖跨语言常见的"定义"写法,而不是任意出现:
      - def/class/func/fn/function/type/interface/struct/enum 后跟符号名
      - const/let/var/val NAME
      - NAME = / NAME: (赋值或带类型注解的定义)
    query 用 re.escape 转义,避免符号名里的特殊字符破坏正则。
    """
    import re as _re

    name = _re.escape(query)
    keyword = (
        r"(?:def|class|func|fn|function|type|interface|struct|enum|trait|"
        r"const|let|var|val)"
    )
    return rf"(?:\b{keyword}\s+{name}\b|\b{name}\s*[:=])"


def _compile_query(query: str, mode: str, case_sensitive: bool):
    """编译出用于 Python 端定列号/校验的正则。

    text   -> 固定字符串(escape)
    regex  -> 原样
    symbol -> 启发式 symbol pattern
    file   -> 不走这里(文件名匹配单独处理)
    """
    import re as _re

    flags = 0 if case_sensitive else _re.IGNORECASE
    if mode == "text":
        pattern = _re.escape(query)
    elif mode == "symbol":
        pattern = _build_symbol_pattern(query)
    else:  # regex
        pattern = query
    return _re.compile(pattern, flags)


# ── 命中构造 ─────────────────────────────────────────────────────────────────

def _make_match(rel_path: str, lineno: int, column: int, line_text: str,
                lines: list[str], context_lines: int, kind: str) -> dict:
    """把单行命中组装成结构化结果(含上下文,全部脱敏)。

    lines 是该文件按行切分的列表(0-based 索引),lineno 是 1-based 命中行号。
    """
    idx = lineno - 1
    start = max(0, idx - context_lines)
    end = min(len(lines), idx + context_lines + 1)
    context = [f"{i + 1}:{redact(lines[i])[:MAX_PREVIEW_CHARS]}" for i in range(start, end)]
    return {
        "path": rel_path,
        "line": lineno,
        "column": column,
        "kind": kind,
        "preview": redact(line_text)[:MAX_PREVIEW_CHARS],
        "context": context,
    }


def _is_binary(data: bytes) -> bool:
    """前 8KB 含 NUL 字节即判为二进制,跳过搜索。"""
    return b"\x00" in data[:8192]


def _rel(path: Path, root: Path) -> str:
    """转成相对 workspace 根的 POSIX 风格路径,跨平台展示稳定(§7.7.4)。"""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# ── ripgrep 后端 ─────────────────────────────────────────────────────────────

def _run_ripgrep(rg: str, root: Path, query: str, mode: str, glob: Optional[str],
                 case_sensitive: bool, timeout: int, max_results: int = DEFAULT_MAX_RESULTS,
                 ) -> tuple[list[str], bool, bool]:
    """有界流式读取 rg；按实际有效命中计数，符号用途不挤占定义的限额。"""
    argv = [rg, "--json", "--max-filesize", str(MAX_FILE_BYTES)]
    if not case_sensitive:
        argv.append("-i")
    if mode in ("text", "symbol"):
        argv.append("-F")
    for directory in DEFAULT_IGNORE_DIRS:
        argv += ["-g", f"!{directory}/"]
    if glob:
        argv += ["-g", glob]
    search_directory = root if root.is_dir() else root.parent
    argv += ["--", query, "." if root.is_dir() else root.name]
    regex = _compile_query(query, mode, case_sensitive)
    # 队列与单行均有界，宽泛查询不会把完整 stdout 放入内存。
    proc = subprocess.Popen(argv, cwd=search_directory, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    chunks: queue.Queue = queue.Queue(maxsize=8)
    stop = threading.Event()

    def read_output():
        try:
            while not stop.is_set():
                line = proc.stdout.readline(MAX_FILE_BYTES * 2 + 1)
                if not line:
                    break
                while not stop.is_set():
                    try:
                        chunks.put(line, timeout=0.05)
                        break
                    except queue.Full:
                        pass
        finally:
            while not stop.is_set():
                try:
                    chunks.put(None, timeout=0.05)
                    break
                except queue.Full:
                    pass

    reader = threading.Thread(target=read_output, daemon=True, name="agentlab-rg-output")
    reader.start()
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    total_bytes = 0
    timed_out = limited = False
    cancel = current_cancel_token()
    try:
        while True:
            if cancel is not None:
                cancel.raise_if_cancelled()
            if time.monotonic() >= deadline:
                timed_out = True
                break
            try:
                raw = chunks.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if raw is None:
                break
            total_bytes += len(raw)
            if total_bytes > MAX_FILE_BYTES * 2:
                limited = True
                break
            line = raw.decode("utf-8", errors="replace")
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") != "match":
                continue
            data = obj["data"]
            if data["path"].get("text") is None:
                continue
            if mode == "symbol" and not regex.search(data["lines"].get("text", "")):
                continue
            lines.append(line)
            if len(lines) > max_results:  # 多看一条实际命中，准确标记截断。
                limited = True
                break
    finally:
        stop.set()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        reader.join(timeout=1)
        proc.stdout.close()
    if not timed_out and not limited and proc.returncode not in (0, 1):
        raise ToolExecutionError(f"ripgrep failed with exit code {proc.returncode}")
    return lines, timed_out, limited


def _parse_ripgrep(json_lines: list[str], root: Path, regex, mode: str,
                   context_lines: int, max_results: int, search_root: Path | None = None,
                   ) -> tuple[list[dict], bool]:
    """解析 rg --json 输出为结构化命中列表,返回 (matches, truncated)。

    对每条 rg match,用 Python regex 在该行重新定位列号:
      - symbol 模式:Python 正则是权威,不匹配的行(只是名字出现)直接丢弃
      - text/regex:rg 已确认命中,正则只用来定列号,定不到就退回列 1
    """
    file_cache: dict[str, list[str]] = {}
    matches: list[dict] = []
    truncated = False

    for raw in json_lines:
        if len(matches) >= max_results:
            truncated = True
            break
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj["data"]
        path_text = data["path"].get("text")
        if path_text is None:
            continue  # 二进制路径(bytes)跳过
        lineno = data["line_number"]
        line_text = data["lines"].get("text", "").rstrip("\n")

        m = regex.search(line_text)
        if mode == "symbol" and m is None:
            continue  # 名字出现但不是定义,丢弃
        column = (m.start() + 1) if m else 1

        abs_path = Path(path_text)
        if not abs_path.is_absolute():
            base = search_root or root
            abs_path = (base if base.is_dir() else base.parent) / abs_path
        rel = _rel(abs_path, root)
        if rel not in file_cache:
            file_cache[rel] = _read_lines(abs_path)
        matches.append(_make_match(rel, lineno, column, line_text,
                                    file_cache[rel], context_lines,
                                    "symbol" if mode == "symbol" else mode))
    return matches, truncated


# ── Python fallback 后端 ─────────────────────────────────────────────────────

def _load_gitignore(root: Path):
    """加载 root 下 .gitignore 为 pathspec(没装 pathspec 或无文件则返回 None)。"""
    try:
        import pathspec
    except ImportError:
        return None
    gi = root / ".gitignore"
    if not gi.exists():
        return None
    lines = gi.read_text(encoding="utf-8").splitlines()
    # pathspec 新版用 'gitignore' factory,老版只有 'gitwildmatch';依次尝试
    for factory in ("gitignore", "gitwildmatch"):
        try:
            return pathspec.PathSpec.from_lines(factory, lines)
        except (ValueError, KeyError, LookupError):
            continue
    return None


def _read_lines(path: Path) -> list[str]:
    """读文件并按行切分;读不了返回空列表(context 退化为空)。"""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if _is_binary(data) or len(data) > MAX_FILE_BYTES:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def _iter_files(root: Path, spec):
    """遍历 root 下文件,跳过默认忽略目录和 .gitignore 命中项,yield 绝对路径。"""
    if root.is_file():
        yield root
        return
    ignore = set(DEFAULT_IGNORE_DIRS)
    for dirpath, dirnames, filenames in _walk(root):
        # 原地裁剪 dirnames,os.walk 就不会下钻被忽略的目录
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for fname in filenames:
            abs_path = Path(dirpath) / fname
            rel = abs_path.relative_to(root).as_posix()
            if spec is not None and spec.match_file(rel):
                continue
            yield abs_path


def _walk(root: Path):
    """os.walk 包一层,方便测试。"""
    import os
    return os.walk(root)


@lru_cache(maxsize=256)
def _glob_regex(pattern: str):
    """rg 风格路径 glob：* 不跨目录，**/ 匹配零或多层目录。"""
    import fnmatch
    anchored = pattern.startswith("/")
    pattern = pattern.lstrip("/")
    prefix = "" if anchored or "/" in pattern else "(?:.*/)?"

    def translate(value):
        parts = []
        i = 0
        while i < len(value):
            char = value[i]
            if value[i:i + 3] == "**/" and (i == 0 or value[i - 1] == "/"):
                parts.append("(?:.*/)?")
                i += 3
            elif value[i:i + 2] == "**" and (i == 0 or value[i - 1] == "/") and i + 2 == len(value):
                parts.append(".*")
                i += 2
            elif char == "*":
                parts.append("[^/]*")
                i += 1
            elif char == "?":
                parts.append("[^/]")
                i += 1
            elif char == "{":
                end = value.find("}", i + 1)
                if end == -1:
                    parts.append(re.escape(char))
                    i += 1
                else:
                    parts.append("(?:" + "|".join(translate(x) for x in value[i + 1:end].split(",")) + ")")
                    i = end + 1
            elif char == "[":
                end = value.find("]", i + 1)
                if end == -1:
                    parts.append(r"\[")
                    i += 1
                else:
                    translated = fnmatch.translate(value[i:end + 1])
                    parts.append("(?!/)" + translated[4:-3])
                    i = end + 1
            else:
                parts.append(re.escape(char))
                i += 1
        return "".join(parts)

    return re.compile(prefix + translate(pattern))


def _glob_matches(path: str, pattern: str) -> bool:
    negative = pattern.startswith("!")
    matched = _glob_regex(pattern[1:] if negative else pattern).fullmatch(path) is not None
    return not matched if negative else matched


def _search_python(root: Path, query: str, mode: str, glob: Optional[str],
                   regex, context_lines: int, max_results: int,
                   ) -> tuple[list[dict], bool, int]:
    """无 rg 时的 Python 扫描。返回 (matches, truncated, skipped_binary)。"""
    spec = _load_gitignore(root)
    matches: list[dict] = []
    truncated = False
    skipped_binary = 0

    for abs_path in _iter_files(root, spec):
        if len(matches) >= max_results:
            truncated = True
            break
        rel = abs_path.name if root.is_file() else _rel(abs_path, root)
        if glob and not _glob_matches(rel, glob):
            continue
        try:
            data = abs_path.read_bytes()
        except OSError:
            continue
        if _is_binary(data):
            skipped_binary += 1
            continue
        if len(data) > MAX_FILE_BYTES:
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            if len(matches) >= max_results:
                truncated = True
                break
            m = regex.search(line)
            if m is None:
                continue
            matches.append(_make_match(_rel(abs_path, workspace_root()), i + 1, m.start() + 1, line,
                                       lines, context_lines,
                                       "symbol" if mode == "symbol" else mode))
    return matches, truncated, skipped_binary


# ── file 模式(文件名/路径搜索)────────────────────────────────────────────────

def _search_files(root: Path, query: str, glob: Optional[str],
                  case_sensitive: bool, max_results: int,
                  ) -> tuple[list[dict], bool]:
    """file 模式:按文件名/路径片段或 glob 匹配,line/column 置 0。"""
    spec = _load_gitignore(root)
    needle = query if case_sensitive else query.lower()
    matches: list[dict] = []
    truncated = False

    for abs_path in _iter_files(root, spec):
        if len(matches) >= max_results:
            truncated = True
            break
        rel = abs_path.name if root.is_file() else _rel(abs_path, root)
        hay = rel if case_sensitive else rel.lower()
        hit = needle in hay
        if glob:
            hit = _glob_matches(rel, glob)
        if not hit:
            continue
        matches.append({
            "path": _rel(abs_path, workspace_root()),
            "line": 0,
            "column": 0,
            "kind": "file",
            "preview": _rel(abs_path, workspace_root()),
            "context": [],
        })
    return matches, truncated


# ── 主入口 ───────────────────────────────────────────────────────────────────

def _enforce_output_limit(result: dict) -> dict:
    """序列化后超过 MAX_OUTPUT_BYTES 时,从尾部裁掉命中并标记 truncated。"""
    while result["matches"]:
        if len(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")) <= MAX_OUTPUT_BYTES:
            break
        result["matches"].pop()
        result["truncated"] = True
    return result


def _code_search(args: dict) -> str:
    """code_search 工具入口。返回 JSON 字符串;越界/参数错误抛 ToolExecutionError。

    拒绝必须表现为错误而不是普通返回值:Registry 用 is_error 判定本次调用是否
    算"成功执行证据",而"没有进展的拒绝"若被记为成功,会让模型拿它当作任务完成
    的依据(见 executor 的 successful_tool_calls 计数)。
    """
    query = (args.get("query") or "").strip()
    if not query:
        raise ToolExecutionError("refused: empty query")

    mode = args.get("mode") or "text"
    if mode not in ("text", "regex", "file", "symbol"):
        raise ToolExecutionError(
            f"refused: unknown mode '{mode}' (use text/regex/file/symbol)")

    case_sensitive = bool(args.get("case_sensitive", False))
    glob = args.get("glob") or None
    try:
        context_lines = int(args.get("context_lines", DEFAULT_CONTEXT_LINES))
    except (TypeError, ValueError):
        context_lines = DEFAULT_CONTEXT_LINES
    try:
        max_results = int(args.get("max_results", DEFAULT_MAX_RESULTS))
    except (TypeError, ValueError):
        max_results = DEFAULT_MAX_RESULTS
    context_lines = max(0, min(context_lines, 10))
    max_results = max(1, min(max_results, 1000))

    root = workspace_root()
    try:
        search_root = _resolve_search_root(args.get("path", "."))
    except WorkspacePathError as exc:
        raise ToolExecutionError(f"refused: {exc}") from exc
    if not search_root.exists():
        raise ToolExecutionError(f"path not found: {search_root}")

    summary: dict = {}

    if mode == "file":
        matches, truncated = _search_files(search_root, query, glob,
                                           case_sensitive, max_results)
    else:
        try:
            regex = _compile_query(query, mode, case_sensitive)
        except Exception as exc:  # 无效正则
            raise ToolExecutionError(
                f"refused: invalid regex: {type(exc).__name__}: {exc}") from exc

        rg = _find_ripgrep()
        if rg:
            json_lines, timed_out, limited = _run_ripgrep(rg, search_root, query, mode,
                                                  glob, case_sensitive, DEFAULT_TIMEOUT, max_results)
            matches, truncated = _parse_ripgrep(json_lines, root, regex, mode,
                                                context_lines, max_results, search_root)
            truncated = truncated or limited
            summary["backend"] = "ripgrep"
            if limited:
                summary["note"] = "search stopped at result or output limit"
            if timed_out:
                truncated = True
                summary["note"] = f"search timed out after {DEFAULT_TIMEOUT}s, results partial"
        else:
            matches, truncated, skipped = _search_python(
                search_root, query, mode, glob, regex, context_lines, max_results)
            summary["backend"] = "python"
            if skipped:
                summary["skipped_binary"] = skipped

    result = {
        "query": query,
        "mode": mode,
        "root": _rel(search_root, root) or ".",
        "truncated": truncated,
        "count": len(matches),
        "matches": matches,
    }
    if summary:
        result["summary"] = summary
    result = _enforce_output_limit(result)
    result["count"] = len(result["matches"])
    return json.dumps(result, ensure_ascii=False, indent=2)


def _code_search_audit_summary(args: dict, result: str) -> tuple[str, str]:
    safe_args = {
        key: args.get(key)
        for key in ("query", "mode", "path", "glob", "max_results")
        if key in args
    }
    try:
        body = json.loads(result)
        result_summary = json.dumps(
            {
                "count": body.get("count", 0),
                "truncated": body.get("truncated", False),
                "backend": (body.get("summary") or {}).get("backend", ""),
            },
            ensure_ascii=False,
        )
    except (json.JSONDecodeError, TypeError, AttributeError):
        result_summary = result
    return json.dumps(safe_args, ensure_ascii=False), result_summary


CODE_SEARCH = Tool(
    parallel_safe=True,
    name="code_search",
    description=(
        "搜索代码并返回文件路径、行号、列号和上下文片段。workspace 内免审批,"
        "搜索外部目录前必须获得用户审批。"
        "比用 shell 拼 grep/find 更安全可控,应作为搜代码的首选。"
        "四种模式:text(普通文本,默认)、regex(正则)、file(文件名/路径)、"
        "symbol(函数/类/变量定义)。只读,自动遵守 .gitignore,跳过 node_modules 等大目录。"
        "拿到结果后用 read_file 精读具体文件。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词、正则或 symbol 名称",
            },
            "mode": {
                "type": "string",
                "enum": ["text", "regex", "file", "symbol"],
                "default": "text",
                "description": "text=文本 regex=正则 file=文件名 symbol=定义",
            },
            "path": {
                "type": "string",
                "description": "可选搜索目录;外部目录需要审批,默认 workspace 根目录 '.'",
                "default": ".",
            },
            "glob": {
                "type": "string",
                "description": "可选文件 glob,例如 '*.py'、'app/**/*.ts'",
            },
            "case_sensitive": {"type": "boolean", "default": False},
            "context_lines": {
                "type": "integer",
                "default": DEFAULT_CONTEXT_LINES,
                "description": f"每条命中上下文行数,默认 {DEFAULT_CONTEXT_LINES}",
            },
            "max_results": {
                "type": "integer",
                "default": DEFAULT_MAX_RESULTS,
                "description": f"最多返回命中数,默认 {DEFAULT_MAX_RESULTS}",
            },
        },
        "required": ["query"],
    },
    executor=_code_search,
    risk="read",
    target_type="filesystem",
    scope="workspace_or_approved_external",
    origin="builtin",
    audit_redactor=_code_search_audit_summary,
    requires_approval=False,  # 只读,风险等级 read,不需审批
    approval_resolver=lambda args: _outside_workspace_approval(
        "code_search",
        args,
    ),
)


def default_tools() -> list[Tool]:
    """返回 code_search 模块的默认工具列表。"""
    return [CODE_SEARCH]
