"""Bounded, cancellable subprocess execution shared by tools and verifiers."""
from __future__ import annotations

import os
import platform
import signal
import subprocess
import threading
import time

from app.agent.cancel import CancelToken


def run_process(argv: list[str], *, cwd: str, timeout: float, cancel: CancelToken | None = None):
    if cancel is None:
        return subprocess.run(
            argv, cwd=cwd, timeout=timeout, capture_output=True, text=True, errors="replace",
        )
    cancel.raise_if_cancelled()
    options = dict(cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    windows = platform.system() == "Windows"
    if windows:
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    proc = subprocess.Popen(argv, **options)
    stop_lock = threading.RLock()
    stopping = False

    def kill_tree() -> None:
        if windows:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                if proc.poll() is None:
                    proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stop() -> None:
        nonlocal stopping
        with stop_lock:
            if stopping:
                return
            stopping = True
        # Windows tree termination can itself block; keep it off the input thread.
        if windows:
            threading.Thread(target=kill_tree, name="agentlab-process-stop", daemon=True).start()
        else:
            kill_tree()

    def reap() -> None:
        try:
            proc.communicate(timeout=2)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass

    deadline = time.monotonic() + timeout
    try:
        with cancel.callback(stop):
            while True:
                cancel.raise_if_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stop()
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    stdout, stderr = proc.communicate(timeout=min(0.05, remaining))
                    cancel.raise_if_cancelled()
                    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
    except BaseException:
        stop()
        raise
    finally:
        # Reaping never delays returning control to the composer.
        if proc.poll() is None:
            threading.Thread(target=reap, name="agentlab-process-reap", daemon=True).start()
