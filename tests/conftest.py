"""Keep directory selection and pytest marker selection equivalent."""
from pathlib import Path

import pytest


_TESTS_ROOT = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def isolated_output_cache(tmp_path, monkeypatch):
    """Tests must never populate or evict the real user's output cache."""
    monkeypatch.setenv("AGENTLAB_OUTPUT_CACHE_DIR", str(tmp_path / "output-cache"))


def pytest_collection_modifyitems(items):
    for item in items:
        path = Path(item.path).resolve()
        if not path.is_relative_to(_TESTS_ROOT):
            continue
        relative = path.relative_to(_TESTS_ROOT)
        layer = relative.parts[0]
        if layer in {"unit", "integration"}:
            item.add_marker(getattr(pytest.mark, layer))
