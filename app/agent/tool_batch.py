"""Bounded read batches; writes and approval/stateful tools are ordering barriers."""
from __future__ import annotations

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context

from app.agent.cancel import Cancelled
from app.models.protocol import ToolResult
from app.tools.registry import _deferred_audits


def signature(call):
    return call.name, json.dumps(call.arguments, sort_keys=True, default=repr)


class ToolCallBatch:
    def __init__(self, tools, calls, *, cancel=None, denied=None, seen=None, on_start=None):
        self.tools, self.calls, self.cancel = tools, calls, cancel
        self.denied = denied if denied is not None else set()
        self.seen = seen if seen is not None else set()
        self.on_start = on_start or (lambda call: None)
        self.announced = set()
        self.futures = {}
        self.published = set()
        self.elapsed_seconds = {}
        self.limit = getattr(tools, "max_parallel_tools", 4)
        if len({call.id for call in calls}) != len(calls):
            self.limit = 1

    def announce(self, call):
        if call.id not in self.announced:
            self.announced.add(call.id)
            self.on_start(call)

    def eligible(self, call):
        tool = self.tools.get(call.name)
        return (tool is not None and getattr(tool, "parallel_safe", False) and tool.risk == "read"
                and getattr(self.tools, "parallel_allowed", lambda name, args: True)(call.name, call.arguments)
                and call.name not in self.denied
                and not tool.validate_arguments(call.arguments)
                and tool.approval_action(call.arguments) is None)

    @staticmethod
    def _run(runner):
        audits = []
        token = _deferred_audits.set(audits)
        started = time.monotonic()
        try:
            try:
                result, error = runner(), None
            except BaseException as exc:
                result, error = None, exc
            return result, audits, time.monotonic() - started, error
        finally:
            _deferred_audits.reset(token)

    def _get(self, call):
        result, audits, elapsed, error = self.futures[call.id].result()
        self.elapsed_seconds[call.id] = elapsed
        if call.id not in self.published:
            self.published.add(call.id)
            for sink, event in audits:
                try:
                    sink(event)
                except Exception:
                    pass
        if error is not None:
            raise error
        return result

    def elapsed(self, call, fallback):
        return self.elapsed_seconds.get(call.id, fallback)

    def execute(self, call, *, approved_action=None, cancel=None):
        cancel = cancel if cancel is not None else self.cancel
        if call.id in self.futures:
            return self._get(call)
        capacity = min(self.limit, getattr(self.tools, "parallel_capacity", lambda: self.limit)())
        group = [call]
        if capacity > 1 and self.eligible(call):
            local_seen = {signature(call)}
            index = next(i for i, item in enumerate(self.calls) if item is call)
            for candidate in self.calls[index + 1:]:
                key = signature(candidate)
                if (len(group) >= capacity or not self.eligible(candidate)
                        or key in self.seen or key in local_seen):
                    break
                local_seen.add(key)
                group.append(candidate)
        prepare = getattr(self.tools, "prepare_parallel", None)
        if len(group) < 2 or prepare is None:
            return self.tools.execute(call.name, call.arguments,
                                      approved_action=approved_action, cancel=cancel)
        # Preflight all mutable registry/manager state on the owning thread.
        runners = [prepare(item.name, copy.deepcopy(item.arguments), cancel=cancel) for item in group]
        if any(runner is None for runner in runners):
            return self.tools.execute(call.name, call.arguments,
                                      approved_action=approved_action, cancel=cancel)
        for item in group:
            self.announce(item)
        pool = ThreadPoolExecutor(max_workers=self.limit, thread_name_prefix="agentlab-read-batch")
        try:
            for item, runner in zip(group, runners):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                self.futures[item.id] = pool.submit(copy_context().run, self._run, runner)
            pending = set(self.futures[item.id] for item in group)
            while pending:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                _, pending = wait(pending, timeout=0.02)
            return self._get(call)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def pending_result(self, call):
        """Early exit must retain results of siblings that actually ran."""
        future = self.futures.get(call.id)
        if future is None or not future.done() or future.cancelled():
            return None
        try:
            output, is_error = self._get(call)
            return ToolResult(call.id, output, is_error)
        except Cancelled:
            return None
