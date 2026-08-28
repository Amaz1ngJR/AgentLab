"""Runtime Submission Queue：把同步 send_message 隔离到后台 worker。"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable


class SubmissionQueueOverloaded(RuntimeError):
    """Runtime 来不及消费新的 Turn 请求。"""


@dataclass(frozen=True)
class TurnSubmission:
    turn_id: str
    thread_id: str
    input_text: str
    images: tuple[Any, ...] = field(default_factory=tuple)
    resume: bool = False


class SubmissionQueue:
    """有界 FIFO 队列和后台 worker；同一 Thread 的互斥由 RuntimeService 保证。"""

    def __init__(
        self,
        handler: Callable[[TurnSubmission], None],
        *,
        max_queue_size: int = 64,
        worker_count: int = 2,
    ):
        if max_queue_size <= 0 or worker_count <= 0:
            raise ValueError("queue size and worker count must be positive")
        self._handler = handler
        self._queue: queue.Queue[TurnSubmission | None] = queue.Queue(max_queue_size)
        self._closed = False
        self._lock = threading.RLock()
        self._workers = [
            threading.Thread(target=self._run, name=f"agentlab-turn-{index}", daemon=True)
            for index in range(worker_count)
        ]
        for worker in self._workers:
            worker.start()

    def submit(self, submission: TurnSubmission) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("submission queue is closed")
        try:
            self._queue.put_nowait(submission)
        except queue.Full as exc:
            raise SubmissionQueueOverloaded("runtime submission queue is full") from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=1.0)

    def _run(self) -> None:
        while True:
            submission = self._queue.get()
            try:
                if submission is None:
                    return
                self._handler(submission)
            finally:
                self._queue.task_done()


__all__ = ["SubmissionQueue", "SubmissionQueueOverloaded", "TurnSubmission"]
