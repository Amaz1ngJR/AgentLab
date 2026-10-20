"""A tool deadline must not cancel the owning Agent turn or read its input."""
import subprocess
import sys

import pytest

from app.agent.cancel import CancelToken
from app.util.process import run_process


def test_command_timeout_preserves_output_and_parent_token(tmp_path):
    token = CancelToken()
    argv = [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(30)"]
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        run_process(argv, cwd=str(tmp_path), timeout=0.5, cancel=token)
    assert b"partial" in exc.value.output
    assert not token.cancelled
    assert token.reason is None
    result = run_process([sys.executable, "-c", "print('continued')"],
                         cwd=str(tmp_path), timeout=5, cancel=token)
    assert result.stdout.strip() == "continued"


@pytest.mark.parametrize("cancellable", [False, True])
def test_command_cannot_consume_composer_stdin(tmp_path, cancellable):
    result = run_process([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
                         cwd=str(tmp_path), timeout=5,
                         cancel=CancelToken() if cancellable else None)
    assert result.stdout.strip() == "''"
