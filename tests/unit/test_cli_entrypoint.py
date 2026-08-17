"""CLI 全局入口与 workspace 参数测试。"""
from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import cli


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_exposes_agentlab_command():
    config = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert config["project"]["scripts"]["agentlab"] == "app.cli:main"
    assert config["project"]["requires-python"] == ">=3.11"


def test_main_workspace_overrides_environment_before_session_build(
    monkeypatch,
    tmp_path,
):
    previous = tmp_path / "previous"
    target = tmp_path / "target"
    previous.mkdir()
    target.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(previous))
    monkeypatch.chdir(tmp_path)

    router = MagicMock()

    def fake_build_session(*, auto_approve, profile):
        assert auto_approve is False
        assert profile is None
        assert Path(cli.os.environ["WORKSPACE_ROOT"]) == target.resolve()
        return router

    monkeypatch.setattr(cli, "_build_session", fake_build_session)
    monkeypatch.setattr(cli, "_repl", lambda current_router: 0)

    assert cli.main(["--workspace", "target"]) == 0
    router.close.assert_called_once()


def test_main_rejects_missing_workspace(monkeypatch, tmp_path):
    build_session = MagicMock()
    monkeypatch.setattr(cli, "_build_session", build_session)

    with pytest.raises(SystemExit) as exc:
        cli.main(["--workspace", str(tmp_path / "missing")])

    assert exc.value.code == 2
    build_session.assert_not_called()

