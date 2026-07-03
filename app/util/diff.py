"""文件改动 diff 渲染 —— 把 write_file 的改动以彩色 diff 呈现给用户。

使用场景:
  Agent 调用 write_file 覆盖已有文件时,先读旧内容,和新内容做行级 diff,
  在审批菜单之前打印出来。绿色 `+` 表示新增,红色 `-` 表示删除,灰色是上下文。
  另外若用户在 VS Code 集成终端里,可选地拉起 VS Code 原生 diff 编辑器。

设计要点:
  - 本模块自包含,不 import app.cli(cli.py 反过来 import 本模块,避免循环依赖)。
  - 渲染只依赖标准库 difflib / unicodedata,终端宽度由调用方传入。
  - VS Code diff 走 `code --diff old new`,失败静默降级(不影响主流程)。
"""
from __future__ import annotations

import difflib
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unicodedata

# ── ANSI 颜色 ────────────────────────────────────────────────────────────────
# 前景 + 深色背景铺满整行,视觉上接近 VS Code / Claude Code 的 diff 高亮。
_RESET = "\033[0m"
_DIM = "\033[2;90m"          # 灰色:上下文行 / 行号
_ADD = "\033[32m"            # 绿色前景:新增行文字
_ADD_BG = "\033[48;5;22m"    # 深绿背景:新增行整行
_DEL = "\033[31m"            # 红色前景:删除行文字
_DEL_BG = "\033[48;5;52m"    # 深红背景:删除行整行
_HEADER = "\033[1m"          # 加粗:文件头


def _vis_len(text: str) -> int:
    """终端显示宽度:东亚宽字符算 2,其他算 1。用于把整行背景铺到 width。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def diff_stats(old: str, new: str) -> tuple[int, int]:
    """返回 (新增行数, 删除行数)。用于打印 "Added N lines, removed M lines"。"""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    added = removed = 0
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def format_stats(added: int, removed: int) -> str:
    """把 (added, removed) 拼成一行人类可读的中文摘要。"""
    parts = []
    if added:
        parts.append(f"新增 {added} 行")
    if removed:
        parts.append(f"删除 {removed} 行")
    return "、".join(parts) if parts else "无实际改动"


def _pad_bg(marker_and_text: str, width: int) -> str:
    """把一行 (含行号列 + marker + 文本) 用空格补到 width,让背景色铺满整行。"""
    pad = width - _vis_len(marker_and_text)
    return marker_and_text + (" " * pad if pad > 0 else "")


def render_color_diff(
    old: str,
    new: str,
    width: int = 80,
    context: int = 3,
    max_lines: int = 200,
) -> str:
    """把 old→new 渲染成带 ANSI 颜色和行号的 unified diff 字符串。

    参数:
      width     - 终端宽度,新增/删除行的背景色会铺满这个宽度
      context   - 每段改动上下保留的上下文行数(类似 diff -U3)
      max_lines - diff 输出的行数上限,超出截断并附省略提示,避免刷屏

    输出样例(去掉颜色):
        68
        69
        70  def _display_width(text: str) -> int:
        71 -     \"\"\"旧的注释\"\"\"
        71 +     \"\"\"新的注释
        72 +
        73 +     多行说明……
        74     width = 0
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    opcodes = sm.get_opcodes()

    # 行号列宽:取新旧最大行号的位数,保证对齐
    max_ln = max(len(old_lines), len(new_lines), 1)
    ln_w = len(str(max_ln))

    def _ln(n: int | None) -> str:
        return (str(n).rjust(ln_w) if n is not None else " " * ln_w)

    out: list[str] = []
    truncated = False

    def _emit(line: str) -> bool:
        """追加一行;超过 max_lines 返回 False 通知调用方停止。"""
        nonlocal truncated
        if len(out) >= max_lines:
            truncated = True
            return False
        out.append(line)
        return True

    for idx, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            block = list(range(i1, i2))
            is_first = idx == 0
            is_last = idx == len(opcodes) - 1
            # 相等块只保留"贴着改动"的 context 行,其余折叠(git diff -U3 风格):
            #   - 第一块:前面没有改动,只留末尾 context 行(改动前的上文)
            #   - 最后一块:后面没有改动,只留开头 context 行(改动后的下文)
            #   - 中间块:两侧都挨着改动,留开头 + 末尾 context 行,中间折叠
            shown: list[int] | None
            folded_before = folded_after = 0
            if is_first and is_last:
                shown = block  # 全文件无改动(调用方通常已跳过)
            elif is_first:
                shown = block[-context:]
                folded_before = len(block) - len(shown)
            elif is_last:
                shown = block[:context]
                folded_after = len(block) - len(shown)
            elif len(block) <= 2 * context:
                shown = block
            else:
                shown = None  # 中间大块:head + ⋮ + tail

            if shown is None:
                for k in block[:context]:
                    if not _emit(f"{_DIM}{_ln(k + 1)}  {old_lines[k]}{_RESET}"):
                        break
                gap = len(block) - 2 * context
                _emit(f"{_DIM}{' ' * ln_w}  ⋮ ({gap} 行未改动){_RESET}")
                for k in block[-context:]:
                    if not _emit(f"{_DIM}{_ln(k + 1)}  {old_lines[k]}{_RESET}"):
                        break
            else:
                if folded_before > 0:
                    _emit(f"{_DIM}{' ' * ln_w}  ⋮ (上方 {folded_before} 行未改动){_RESET}")
                for k in shown:
                    if not _emit(f"{_DIM}{_ln(k + 1)}  {old_lines[k]}{_RESET}"):
                        break
                if folded_after > 0:
                    _emit(f"{_DIM}{' ' * ln_w}  ⋮ (下方 {folded_after} 行未改动){_RESET}")
        else:
            if tag in ("replace", "delete"):
                for k in range(i1, i2):
                    body = _pad_bg(f"{_ln(k + 1)} - {old_lines[k]}", width)
                    if not _emit(f"{_DEL_BG}{_DEL}{body}{_RESET}"):
                        break
            if tag in ("replace", "insert"):
                for k in range(j1, j2):
                    body = _pad_bg(f"{_ln(k + 1)} + {new_lines[k]}", width)
                    if not _emit(f"{_ADD_BG}{_ADD}{body}{_RESET}"):
                        break
        if truncated:
            break

    if truncated:
        out.append(f"{_DIM}{' ' * ln_w}  … diff 过长已截断{_RESET}")
    return "\n".join(out)


def render_header(path: str, is_new: bool, added: int, removed: int,
                  is_delete: bool = False) -> str:
    """渲染 diff 上方的文件头:● Create/Update/Delete(path) + 改动摘要。"""
    if is_delete:
        verb, dot = "Delete", f"{_DEL}●{_RESET}"
    elif is_new:
        verb, dot = "Create", f"{_ADD}●{_RESET}"
    else:
        verb, dot = "Update", f"{_HEADER}●{_RESET}"
    summary = format_stats(added, removed)
    return f"{dot} {_HEADER}{verb}{_RESET}({path})\n  {_DIM}{summary}{_RESET}"


# ── VS Code 原生 diff ─────────────────────────────────────────────────────────

def vscode_diff_available() -> bool:
    """判断当前是否值得拉起 VS Code diff:在 VS Code 终端里且 `code` CLI 可用。

    受环境变量 AGENTLAB_VSCODE_DIFF 控制:
      - "0" / "off" / "false" : 强制关闭
      - "1" / "on"  / "true"  : 强制开启(只要 code CLI 在)
      - 未设置(默认)          : 仅当检测到 VS Code 集成终端时开启
    """
    flag = os.environ.get("AGENTLAB_VSCODE_DIFF", "").strip().lower()
    if flag in ("0", "off", "false", "no"):
        return False
    if shutil.which("code") is None:
        return False
    if flag in ("1", "on", "true", "yes"):
        return True
    # 默认:只在 VS Code 集成终端里自动开(TERM_PROGRAM=vscode)
    return os.environ.get("TERM_PROGRAM", "").lower() == "vscode"


def launch_vscode_diff(old: str, new: str, filename: str):
    """在 VS Code 里打开 old/new 的并排 diff,返回可用于关闭它的句柄(或 None)。

    做法:把旧内容/新内容各写到临时文件,用 `code --wait --diff a b` 打开。
    加 --wait 让 code 进程一直存活到那个 diff tab 被关闭;反过来 —— agent 在
    用户允许/拒绝后调 close_vscode_diff(handle),terminate 掉这个进程,VS Code
    的 diff tab 就随之关闭。

    返回值:成功时返回 subprocess.Popen(句柄),失败 / 未启用返回 None。
    """
    if not vscode_diff_available():
        return None
    try:
        base = os.path.basename(filename) or "file"
        tmp = tempfile.mkdtemp(prefix="agentlab_diff_")
        old_path = os.path.join(tmp, f"{base}.old")
        new_path = os.path.join(tmp, f"{base}.new")
        with open(old_path, "w", encoding="utf-8") as f:
            f.write(old)
        with open(new_path, "w", encoding="utf-8") as f:
            f.write(new)
        # --wait:code 进程存活到 tab 关闭;terminate 该进程即可关掉 diff。
        # --reuse-window:复用当前窗口,不新开。
        proc = subprocess.Popen(
            ["code", "--wait", "--reuse-window", "--diff", old_path, new_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc
    except (OSError, ValueError):
        return None


def close_vscode_diff(handle) -> None:
    """关闭 launch_vscode_diff 打开的 diff tab。handle 为其返回的 Popen。失败静默。

    原理:launch 用了 `code --wait`,该进程会一直挂着直到 tab 关闭。terminate
    它,VS Code 端的 diff 编辑器就会随之关闭(--wait 的进程被杀等价于 tab 关)。
    """
    if handle is None:
        return
    try:
        if handle.poll() is None:  # 还在运行(tab 还开着)
            handle.terminate()
    except (OSError, ValueError):
        pass


# ── shell 命令里的文件改动探测 ────────────────────────────────────────────────
# 模型有时绕过 write_file,直接用 shell 重定向 / sed -i / tee 改文件。这里从
# 命令文本里尽力解析出"会被写入的文件路径",让 CLI 能在执行前后做 diff。
# 只做保守的启发式匹配,匹配不到就返回空(退化为不显示 diff,不影响主流程)。

# 重定向: `> path` / `>> path` / `1> path` / `2>> path`(取路径,忽略 fd 号)
_REDIRECT_RE = re.compile(r"(?:^|\s)\d*>{1,2}\s*([^\s;&|<>]+)")
# tee: `tee path` / `tee -a path ...`(可能多个目标,逐个取)
_TEE_RE = re.compile(r"(?:^|\s|\|)\s*tee\s+((?:-a\s+)?[^\s;&|]+(?:\s+[^\s;&|-][^\s;&|]*)*)")
# sed 原地编辑: `sed -i ...  path`(GNU/BSD 都近似,取末尾非选项参数)
_SED_I_RE = re.compile(r"(?:^|\s|;|&&|\|)\s*sed\b[^;&|]*\s-i\b([^;&|]*)")


def _unquote(tok: str) -> str:
    """去掉 shell 引号,失败原样返回。"""
    try:
        parts = shlex.split(tok)
        return parts[0] if parts else tok
    except ValueError:
        return tok.strip("'\"")


def shell_write_targets(command: str) -> list[str]:
    """从 shell 命令文本里解析出可能被写入的文件路径(去重,保序)。

    覆盖最常见的三类:重定向 `>`/`>>`、`tee`、`sed -i`。解析不到返回空列表。
    这是启发式,不追求完备 —— 目标是把"模型用 shell 偷偷改文件"的高频写法
    捞出来做 diff,漏掉的极端写法退化为不显示,不影响正确性。
    """
    if not command:
        return []
    targets: list[str] = []

    def _add(raw: str) -> None:
        p = _unquote(raw.strip())
        # 跳过 /dev/null、fd 重定向(&1)、进程替换等非普通文件
        if not p or p.startswith("&") or p.startswith("/dev/"):
            return
        if p not in targets:
            targets.append(p)

    for m in _REDIRECT_RE.finditer(command):
        _add(m.group(1))
    for m in _TEE_RE.finditer(command):
        # tee 后面可能是 "-a a.txt b.txt",拆开逐个加
        for tok in m.group(1).split():
            if tok in ("-a", "--append"):
                continue
            _add(tok)
    for m in _SED_I_RE.finditer(command):
        toks = [t for t in m.group(1).split() if t]
        # sed -i 的文件通常是最后一个非选项、非脚本参数;保守取末尾一个
        if toks:
            _add(toks[-1])
    return targets


def read_text_safe(path) -> str:
    """读文本,任何异常(不存在 / 二进制 / 权限)都返回空串。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ── workspace 快照:shell 执行前后比对,不依赖解析命令 ─────────────────────────
# 模型改文件的方式无穷(重定向 / sed / python -c / heredoc / perl …),靠解析
# 命令文本猜"改了哪个文件"注定漏。改为:执行前后各拍一次 workspace 文件内容
# 快照,diff 出真正落盘的变化 —— 不管命令怎么写,磁盘上变了就抓得到。

# 快照上限,防止在超大仓库里遍历/读取拖慢每次 shell 调用
_SNAP_MAX_FILES = 4000        # 最多快照多少个文件
_SNAP_MAX_FILE_BYTES = 262_144  # 单文件超过 256KB 不进快照(大概率非源码)
_SNAP_MAX_TOTAL_BYTES = 32 * 1024 * 1024  # 总字节上限 32MB
# 遍历时跳过的目录名(体积大 / 无关改动 / 会误报)
_SNAP_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode",
    "dist", "build", ".next", "target", ".tox", "site-packages",
}


def _looks_binary(data: bytes) -> bool:
    """含 NUL 字节就当二进制(和 git 的启发式一致),不进快照。"""
    return b"\x00" in data[:8192]


def snapshot_tree(root) -> dict[str, str] | None:
    """遍历 root 下的文本文件,返回 {绝对路径: 内容}。

    超过规模上限(文件数 / 总字节)时返回 None,表示"仓库太大不做全量快照",
    调用方应退化到基于命令解析的窄路径(shell_write_targets)。
    """
    root = os.path.abspath(str(root))
    snap: dict[str, str] = {}
    total = 0
    count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # 原地过滤子目录,os.walk 不会再进入被删掉的项
            dirnames[:] = [d for d in dirnames if d not in _SNAP_SKIP_DIRS
                           and not d.startswith(".git")]
            for name in filenames:
                fp = os.path.join(dirpath, name)
                try:
                    st = os.lstat(fp)
                except OSError:
                    continue
                # 跳过符号链接 / 非普通文件 / 过大文件
                if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                    continue
                if st.st_size > _SNAP_MAX_FILE_BYTES:
                    continue
                count += 1
                if count > _SNAP_MAX_FILES:
                    return None
                try:
                    with open(fp, "rb") as f:
                        data = f.read(_SNAP_MAX_FILE_BYTES + 1)
                except OSError:
                    continue
                if len(data) > _SNAP_MAX_FILE_BYTES or _looks_binary(data):
                    continue
                total += len(data)
                if total > _SNAP_MAX_TOTAL_BYTES:
                    return None
                snap[fp] = data.decode("utf-8", errors="replace")
    except OSError:
        return None
    return snap


def diff_snapshots(
    before: dict[str, str] | None,
    after: dict[str, str] | None,
) -> list[tuple[str, str, str, bool]]:
    """比对两次快照,返回发生变化的文件列表。

    每项为 (路径, 旧内容, 新内容, 是否新建)。删除的文件 新内容为 ""。
    before/after 任一为 None(快照放弃)时返回空列表。
    """
    if before is None or after is None:
        return []
    changed: list[tuple[str, str, str, bool]] = []
    keys = sorted(set(before) | set(after))
    for k in keys:
        old = before.get(k, "")
        new = after.get(k, "")
        if old == new:
            continue
        is_new = k not in before
        changed.append((k, old, new, is_new))
    return changed
