"""Bounded read batches; writes and approval/stateful tools are ordering barriers."""
from __future__ import annotations

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context

from app.agent.cancel import Cancelled
from app.models.protocol import ToolResult
from app.tools.output_budget import TOOL_OUTPUT_MAX_CHARS, fits_output, truncate_tool_output
from app.tools.registry import _deferred_audits

EXPLORATION_TOOLS = frozenset({"code_search", "list_dir", "read_file", "read_files"})
EXPLORATION_BATCH_CHARS = 12_000


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
        exploration_count = sum(call.name in EXPLORATION_TOOLS for call in calls)
        self.exploration_allowance = (EXPLORATION_BATCH_CHARS // exploration_count
                                      if exploration_count > 1 else None)
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

    def limit_exploration_output(self, call, output, is_error):
        allowance = self.exploration_allowance
        if (allowance is None or call.name not in EXPLORATION_TOOLS or is_error
                or len(output) <= allowance):
            return output
        notice = "[本轮探索输出达到共享上限；需要时缩小范围重读]"
        if self.tools.get("read_tool_output") is not None:
            from app.tools.output_archive import archive_output, output_reference
            output_id = archive_output(output)
            if output_id:
                notice = "[本轮探索输出达到共享上限] " + output_reference(output_id)
        if call.name in {"code_search", "read_files"}:
            return self._limit_exploration_json(call.name, output, allowance, notice)
        preview = output[:max(0, allowance - len(notice) - 1)]
        # Keep the preview on whole lines when possible.
        preview = preview.rsplit("\n", 1)[0] if "\n" in preview else preview
        bounded = f"{preview}\n{notice}" if preview else notice[:allowance]
        return bounded if fits_output(bounded, allowance) else truncate_tool_output(output, allowance, reference=notice)

    def limit_output(self, call, output, is_error):
        output = self.limit_exploration_output(call, output, is_error)
        if fits_output(output):
            return output
        from app.tools.output_archive import archive_output, output_reference
        output_id = archive_output(output) if self.tools.get("read_tool_output") is not None else None
        reference = output_reference(output_id) if output_id else ""
        if not is_error and call.name in {"code_search", "read_files"}:
            return self._limit_exploration_json(call.name, output, TOOL_OUTPUT_MAX_CHARS,
                                                reference or "[工具输出超过历史预算，请缩小读取范围]")
        return truncate_tool_output(output, reference=reference)

    @staticmethod
    def _limit_exploration_json(name, output, allowance, notice):
        try:
            data = json.loads(output)
        except ValueError:
            return notice[:allowance]
        entries_key = "matches" if name == "code_search" else "files"
        if not isinstance(data, dict) or not isinstance(data.get(entries_key), list):
            return notice[:allowance]
        data["truncated"] = True
        data["batch_notice"] = notice
        entries = data[entries_key]
        while True:
            if name == "code_search":
                data["count"] = len(entries)
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if fits_output(encoded, allowance):
                return encoded
            if not entries:
                return notice[:allowance]
            omitted = entries.pop()
            if name == "read_files":
                data["pending_indices"][:0] = [omitted["index"], *omitted.get("covered_indices", [])]

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
