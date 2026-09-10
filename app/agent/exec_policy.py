"""Session-scoped command prefix policy for interactive shell approvals.

This is intentionally narrower than an operating-system sandbox. It only reduces
repeated prompts after the user approves a concrete command family; it does not
claim to contain arbitrary child processes. Ambiguous shell syntax and dangerous
command families therefore never receive reusable suggestions.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shlex
from typing import Iterable


_SAFE_OPERATORS = {"|", "||", "&&", ";"}
_SHELL_PUNCTUATION = ";&|<>()"
_CONTROL_WORDS = {
    "case", "do", "done", "elif", "else", "esac", "fi", "for",
    "foreach", "function", "if", "in", "then", "trap", "try", "until",
    "while",
}
_NO_REUSABLE_PREFIX = {
    "bash", "cmd", "cmd.exe", "dd", "del", "diskpart", "doas", "erase",
    "fish", "format", "halt", "mkfs", "node", "osascript", "perl",
    "php", "poweroff", "powershell", "pwsh", "python", "python3", "rd",
    "reboot", "reg", "rm", "rmdir", "ruby", "sh", "shutdown", "su",
    "sudo", "zsh",
}
_SIMPLE_READ_COMMANDS = {
    "cat", "echo", "get-childitem", "get-content", "grep", "head", "ls",
    "nl", "printf", "pwd", "rg", "select-string", "stat", "tail", "type",
    "wc",
}
_GIT_READ_SUBCOMMANDS = {
    "diff", "log", "rev-parse", "show", "status",
}
_GIT_WRITE_SUBCOMMANDS = {
    "add", "commit",
}
_BUILD_SUBCOMMANDS = {
    "build", "check", "clippy", "compile", "fmt", "format", "lint",
    "package", "test",
}
_DANGEROUS_GIT_FLAGS = {"--ext-diff", "--textconv"}
_ADVANCED_TOKEN_MARKERS = ("$", "`", "*", "?")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class CommandPrefix:
    """A token prefix that can be remembered after explicit user approval."""

    tokens: tuple[str, ...]

    @property
    def label(self) -> str:
        return shlex.join(self.tokens)

    def matches(self, command: tuple[str, ...]) -> bool:
        return len(command) >= len(self.tokens) and command[: len(self.tokens)] == self.tokens


@dataclass(frozen=True)
class CommandAnalysis:
    """Conservative parse result used by the approval UI and policy matcher."""

    commands: tuple[tuple[str, ...], ...] = ()
    suggestions: tuple[CommandPrefix, ...] = ()
    reusable: bool = False
    reason: str = ""

    @property
    def suggestion_label(self) -> str:
        return " + ".join(rule.label for rule in self.suggestions)


def _tokenize_linear_script(command: str) -> tuple[tuple[str, ...], ...] | None:
    """Split a small, linear shell subset into commands.

    Like Codex execpolicy, compound commands are evaluated segment by segment.
    We deliberately decline reusable approval for syntax that would require a
    full Bash or PowerShell parser.
    """
    if not command.strip() or "\n" in command or "\r" in command:
        return None
    try:
        lexer = shlex.shlex(
            command,
            posix=os.name != "nt",
            punctuation_chars=_SHELL_PUNCTUATION,
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None

    commands: list[tuple[str, ...]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SAFE_OPERATORS:
            if not current:
                return None
            commands.append(tuple(current))
            current = []
            continue
        if token and all(char in _SHELL_PUNCTUATION for char in token):
            return None
        current.append(token)
    if not current:
        return None
    commands.append(tuple(current))
    return tuple(commands)


def _contains_advanced_syntax(tokens: Iterable[str]) -> bool:
    for index, token in enumerate(tokens):
        lowered = token.lower().strip('"\'')
        if any(marker in token for marker in _ADVANCED_TOKEN_MARKERS):
            return True
        if index == 0 and "=" in token and not token.startswith("-"):
            return True
        if index == 0 and lowered in _CONTROL_WORDS:
            return True
    return False


def _program_name(token: str) -> str:
    """Normalize POSIX and Windows executable spellings for rule matching."""
    name = re.split(r"[\\/]", token.strip('"\''))[-1].lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _outside_workspace_argument(
    command: tuple[str, ...],
    *,
    cwd: Path,
    workspace: Path,
) -> bool:
    """Reject obvious path arguments that escape the workspace.

    This is defense in depth, not shell containment. The executable token is
    skipped because system binaries normally live outside the workspace.
    """
    for raw in command[1:]:
        token = raw.strip('"\'')
        if not token or token.startswith("-") or "://" in token:
            if "=" in token:
                token = token.split("=", 1)[1]
            else:
                continue
        if token in {".", ".."} or token.startswith(("./", "../", "~/", "/")) \
                or _WINDOWS_DRIVE.match(token):
            candidate = Path(token).expanduser()
            if not candidate.is_absolute():
                candidate = cwd / candidate
            try:
                candidate.resolve(strict=False).relative_to(workspace)
            except (OSError, ValueError):
                return True
    return False


def _suggest_prefix(command: tuple[str, ...]) -> CommandPrefix | None:
    program = _program_name(command[0])
    args = tuple(token.strip('"\'') for token in command[1:])
    lowered_args = tuple(token.lower() for token in args)

    if program in _NO_REUSABLE_PREFIX or not program:
        return None
    if program in _SIMPLE_READ_COMMANDS:
        if program == "sed":
            return None
        if program in {"rg", "grep"} and any(
            token in {"--pre", "--pre-glob"} or token.startswith("--pre=")
            for token in lowered_args
        ):
            return None
        if program == "nl" and args and args[0].startswith("-"):
            return CommandPrefix((program, args[0]))
        return CommandPrefix((program,))
    if program == "sed":
        if not args or not any(token == "-n" or token.startswith("-n") for token in args):
            return None
        if any(token == "-i" or token.startswith("-i") or token == "--in-place"
               for token in lowered_args):
            return None
        return CommandPrefix((program, "-n"))
    if program == "git" and args:
        subcommand = lowered_args[0]
        if subcommand in _GIT_READ_SUBCOMMANDS:
            if any(token in _DANGEROUS_GIT_FLAGS for token in lowered_args[1:]):
                return None
            return CommandPrefix((program, subcommand))
        if subcommand in _GIT_WRITE_SUBCOMMANDS:
            return CommandPrefix((program, subcommand))
        return None
    if program in {"npm", "pnpm", "yarn"} and args:
        subcommand = lowered_args[0]
        if subcommand == "run" and len(args) >= 2:
            return CommandPrefix((program, "run", args[1]))
        if subcommand in _BUILD_SUBCOMMANDS:
            return CommandPrefix((program, subcommand))
        return None
    if program in {"cargo", "dotnet", "gradle", "gradlew", "mvn", "mvnw"} and args:
        subcommand = lowered_args[0]
        if subcommand in _BUILD_SUBCOMMANDS:
            return CommandPrefix((program, subcommand))
        return None
    if program in {"pytest", "ruff"}:
        return CommandPrefix((program,))
    if program in {"cmake", "make", "ninja"}:
        if args:
            return CommandPrefix((program, args[0]))
        return CommandPrefix((program,))
    return None


def analyze_command(
    command: str,
    *,
    cwd: str | Path,
    workspace: str | Path,
) -> CommandAnalysis:
    """Return reusable prefix suggestions for a conservative shell subset."""
    commands = _tokenize_linear_script(command)
    if not commands:
        return CommandAnalysis(reason="命令包含复杂 shell 语法，不能复用授权")
    if any(_contains_advanced_syntax(segment) for segment in commands):
        return CommandAnalysis(commands=commands, reason="变量、替换或通配符不能复用授权")

    commands = tuple(
        (_program_name(segment[0]), *segment[1:])
        for segment in commands
    )

    root = Path(workspace).expanduser().resolve(strict=False)
    working = Path(cwd).expanduser()
    if not working.is_absolute():
        working = root / working
    working = working.resolve(strict=False)
    try:
        working.relative_to(root)
    except ValueError:
        return CommandAnalysis(commands=commands, reason="工作目录越界，不能复用授权")
    if any(
        _outside_workspace_argument(segment, cwd=working, workspace=root)
        for segment in commands
    ):
        return CommandAnalysis(commands=commands, reason="命令参数包含工作区外路径")

    suggestions: list[CommandPrefix] = []
    for segment in commands:
        suggestion = _suggest_prefix(segment)
        if suggestion is None:
            return CommandAnalysis(commands=commands, reason="命令族不支持可复用授权")
        if suggestion not in suggestions:
            suggestions.append(suggestion)
    return CommandAnalysis(
        commands=commands,
        suggestions=tuple(suggestions),
        reusable=True,
    )


class SessionExecPolicy:
    """In-memory prefix rules, isolated by canonical workspace path."""

    def __init__(self) -> None:
        self._rules: dict[str, set[CommandPrefix]] = {}

    @staticmethod
    def _scope(workspace: str | Path) -> str:
        return str(Path(workspace).expanduser().resolve(strict=False))

    def allows(self, analysis: CommandAnalysis, *, workspace: str | Path) -> bool:
        if not analysis.reusable or not analysis.commands:
            return False
        rules = self._rules.get(self._scope(workspace), set())
        return all(any(rule.matches(command) for rule in rules) for command in analysis.commands)

    def remember(self, analysis: CommandAnalysis, *, workspace: str | Path) -> None:
        if not analysis.reusable:
            return
        self._rules.setdefault(self._scope(workspace), set()).update(analysis.suggestions)
