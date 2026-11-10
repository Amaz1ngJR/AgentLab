"""Context-window resolution without model-name heuristics or shared credential caches."""
from dataclasses import dataclass
import threading
import time
from typing import Any

DEFAULT_CONTEXT_LIMIT = 64_000


def positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


@dataclass(frozen=True)
class ContextWindow:
    tokens: int
    source: str  # config / provider / default


class ContextWindowResolver:
    """Per-router cache: successes last 10 minutes, missing/failed metadata 1 minute.

    Adapter fetch hooks must use a short timeout and disable automatic retries.
    Explicit configuration bypasses the network and cache entirely.
    """
    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._lock = threading.Lock()
        self._cached: ContextWindow | None = None
        self._expires = 0.0

    def resolve(self) -> ContextWindow:
        configured = positive_int(getattr(getattr(self.adapter, "_cfg", None), "context_size", None))
        if configured is not None:
            return ContextWindow(configured, "config")
        with self._lock:
            if self._cached is not None and time.monotonic() < self._expires:
                return self._cached
            fetch = getattr(self.adapter, "fetch_context_size", None)
            try:
                size = positive_int(fetch()) if callable(fetch) else None
            except Exception:
                size = None  # Missing endpoints, old SDKs, malformed responses, offline providers.
            self._cached = ContextWindow(size, "provider") if size else ContextWindow(DEFAULT_CONTEXT_LIMIT, "default")
            self._expires = time.monotonic() + (600 if size else 60)
            return self._cached
