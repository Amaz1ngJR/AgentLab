"""Host-enforced policies for AgentLab's local command tools."""
from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
import platform

from app.config.loader import workspace_root


class ProcessSandboxError(ValueError):
    pass


def sandbox_mode() -> str:
    mode = os.getenv("AGENTLAB_PROCESS_SANDBOX", "approval-only").strip().lower()
    if mode not in {"approval-only", "read-only", "workspace"}:
        raise ProcessSandboxError("AGENTLAB_PROCESS_SANDBOX must be approval-only, read-only, or workspace")
    return mode


def _wsl_path(path: PureWindowsPath) -> str:
    if len(path.drive) != 2 or not path.is_absolute():
        raise ProcessSandboxError("WSL sandbox requires an absolute drive-letter workspace")
    return f"/mnt/{path.drive[0].lower()}/" + "/".join(path.parts[1:])


def _windows_sandbox(argv, root, cwd, protected, mode):
    distro = os.getenv("AGENTLAB_WSL_DISTRO", "").strip()
    launcher = Path(os.getenv("SystemRoot", r"C:\Windows")) / "System32" / "wsl.exe"
    if not distro or not launcher.is_file():
        raise ProcessSandboxError(
            "Windows process sandbox requires AGENTLAB_WSL_DISTRO and WSL2 with bubblewrap; command not started"
        )
    if len(argv) != 4 or argv[:3] != ["powershell", "-NoProfile", "-Command"]:
        raise ProcessSandboxError("Windows sandbox accepts only commands from the unified shell launcher")
    workspace = PureWindowsPath(root)
    try:
        relative = PureWindowsPath(cwd or root).relative_to(workspace)
    except ValueError as exc:
        raise ProcessSandboxError("Windows sandbox cwd must be inside the workspace") from exc
    # A fixed bootstrap refuses Windows executable interop before entering the
    # namespace. Only the selected workspace is mounted from the Windows host.
    guard = (
        'case "$(/usr/bin/uname -r)" in *WSL2*|*microsoft-standard*) ;; '
        '*) echo "AgentLab requires WSL2 for process isolation" >&2; exit 125;; esac; '
        '/usr/bin/awk \'/^ *\\[/ { section = ($0 ~ /^ *\\[interop\\] *$/) } '
        'section && /^ *enabled *=/ { disabled = ($0 ~ /^ *enabled *= *false *$/) } '
        'END { exit !disabled }\' /etc/wsl.conf || '
        '{ echo "Set [interop] enabled=false in /etc/wsl.conf and restart the distribution" >&2; exit 125; }; '
        'if [ ! -r /proc/sys/fs/binfmt_misc/status ]; then '
        'echo "Cannot verify WSL Windows interop status" >&2; exit 125; fi; '
        'interop=/proc/sys/fs/binfmt_misc/WSLInterop; '
        'if [ -e "$interop" ] && ! /usr/bin/grep -qx disabled "$interop"; then '
        'echo "Disable WSL interop in /etc/wsl.conf before using this sandbox" >&2; exit 125; fi; '
        'exec /usr/bin/bwrap "$@"'
    )
    mount = "--bind" if mode == "workspace" else "--ro-bind"
    command = [
        str(launcher), "--distribution", distro, "--cd", "/", "--exec",
        "/usr/bin/bash", "-c", guard, "agentlab-sandbox",
        "--unshare-all", "--disable-userns", "--die-with-parent", "--cap-drop", "ALL",
        "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/tmp",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    ]
    for directory in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
        command.extend(["--ro-bind-try", directory, directory])
    command.extend([mount, _wsl_path(workspace), "/workspace"])
    for path in protected:
        command.extend(["--ro-bind", _wsl_path(PureWindowsPath(path)), f"/workspace/{path.name}"])
    return [*command, "--chdir", f"/workspace/{relative.as_posix()}",
            "--", "/usr/bin/bash", "-c", argv[-1]]


def sandbox_command(argv: list[str], *, cwd: str | Path | None = None) -> list[str]:
    mode = sandbox_mode()
    if mode == "approval-only":
        return argv

    root = workspace_root().resolve()
    protected = []
    if mode == "workspace":
        reserved = {".git", ".agentlab", ".codex", ".aws", ".ssh", ".env", "credentials.json"}
        protected = sorted(path for path in root.iterdir()
                           if path.name in reserved or path.name.startswith(".env."))
        for path in protected:
            if path.is_symlink():
                raise ProcessSandboxError(f"cannot sandbox symlinked protected path: {path.name}")
    system = platform.system()
    if system == "Windows":
        return _windows_sandbox(argv, root, cwd, protected, mode)
    if system == "Darwin" and Path("/usr/bin/sandbox-exec").is_file():
        rules = [
            "(version 1)", "(allow default)", "(deny network*)", "(deny file-write*)",
            "(allow file-write-data (literal \"/dev/null\") (regex #\"^/dev/(ttys|pty)[a-zA-Z0-9]+$\"))",
        ]
        if mode == "workspace":
            rules.append(f"(allow file-write* (subpath {json.dumps(str(root), ensure_ascii=False)}))")
            for path in protected:
                quoted = json.dumps(str(path), ensure_ascii=False)
                rules.append(f"(deny file-write* (literal {quoted}) (subpath {quoted}))")
        return ["/usr/bin/sandbox-exec", "-p", "\n".join(rules), *argv]
    if system == "Linux" and (bwrap := next(
        (path for path in ("/usr/bin/bwrap", "/bin/bwrap") if Path(path).is_file()), None,
    )):
        command = [bwrap, "--unshare-all", "--disable-userns", "--die-with-parent", "--ro-bind", "/", "/",
                   "--dev", "/dev", "--proc", "/proc"]
        if mode == "workspace":
            command.extend(["--bind", str(root), str(root)])
            for path in protected:
                command.extend(["--ro-bind", str(path), str(path)])
        return [*command, "--", *argv]
    raise ProcessSandboxError(f"process sandbox {mode!r} is unavailable on {system}; command not started")
