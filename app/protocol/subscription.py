"""有界 Protocol 事件订阅队列。"""
from __future__ import annotations

import queue
from dataclasses import dataclass
from typing import Iterator

from app.protocol.envelopes import EventEnvelope


class EventQueueOverloaded(RuntimeError):
    """订阅者消费过慢，事件队列已满。"""


@dataclass
class EventSubscription:
    client_id: str
    max_queue_size: int = 256

    def __post_init__(self) -> None:
        if self.max_queue_size <= 0:
            raise ValueError("max_queue_size must be positive")
        self._queue: queue.Queue[EventEnvelope] = queue.Queue(self.max_queue_size)
        self._closed = False
        self._overflowed = False

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    def put(self, event: EventEnvelope) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full as exc:
            self._overflowed = True
            self._closed = True
            raise EventQueueOverloaded(
                f"subscriber {self.client_id} is too slow; reconnect with after_sequence"
            ) from exc

    def get(self, timeout: float | None = None) -> EventEnvelope:
        if self._closed and self._queue.empty():
            raise StopIteration
        return self._queue.get(timeout=timeout)

    def drain(self) -> list[EventEnvelope]:
        events: list[EventEnvelope] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events

    def close(self) -> None:
        self._closed = True

    def __iter__(self) -> Iterator[EventEnvelope]:
        while not (self._closed and self._queue.empty()):
            try:
                yield self.get(timeout=0.1)
            except queue.Empty:
                continue
