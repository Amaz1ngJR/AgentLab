"""Keep directory selection and pytest marker selection equivalent."""
from pathlib import Path

import pytest


_TESTS_ROOT = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items):
    for item in items:
        path = Path(item.path).resolve()
        if not path.is_relative_to(_TESTS_ROOT):
            continue
        relative = path.relative_to(_TESTS_ROOT)
        layer = relative.parts[0]
        if layer in {"unit", "integration"}:
            item.add_marker(getattr(pytest.mark, layer))
