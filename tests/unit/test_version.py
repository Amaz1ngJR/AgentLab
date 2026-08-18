"""版本单一来源、包元数据和 CLI 输出测试。"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from app import __version__
from app import cli
from app.version import version_text


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_version_has_semantic_shape():
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)
    assert version_text() == f"AgentLab {__version__}"


def test_pyproject_reads_version_from_single_source():
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    assert "version" in data["project"]["dynamic"]
    assert data["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "app.version.__version__"
    }


def test_cli_version_option(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == version_text()
