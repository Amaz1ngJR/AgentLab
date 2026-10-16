"""Serialized child approvals serviced exclusively by the owner execution thread."""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from app.agent.approval import ApprovalResult, request_tool_approval
from app.agent.cancel import CancelToken


@dataclass
class PendingChildApproval:
    policy: object
    tool: object
    action: str
    arguments: dict
    cancel: CancelToken
    workspace: Path | None = None
    child_run_id: str = ""
    waiting: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: ApprovalResult | None = None


class ChildApprovalPolicy:
    def __init__(self, policy, requests: queue.Queue, cancel: CancelToken, *,
                 workspace: Path | None = None, child_run_id: str = "", waiting=None):
        self.policy, self.requests, self.cancel = policy, requests, cancel
        self.workspace, self.child_run_id = workspace, child_run_id
        self.waiting = waiting if waiting is not None else threading.Event()

    def request_tool(self, tool, action, tool_input):
        request = PendingChildApproval(self.policy, tool, action, dict(tool_input), self.cancel,
                                       self.workspace, self.child_run_id, self.waiting)
        self.waiting.set()
        self.requests.put(request)
        try:
            while not request.done.wait(0.02):
                self.cancel.raise_if_cancelled()
            self.cancel.raise_if_cancelled()
            return request.result
        finally:
            self.waiting.clear()

    def request(self, action, tool_input):
        return False  # structured metadata is required

    def fork_for_child(self):
        return self


def service_child_approvals(requests: queue.Queue):
    while True:
        try:
            item = requests.get_nowait()
        except queue.Empty:
            return
        if item.cancel.cancelled:
            item.result = ApprovalResult(False, cancelled=True)
        else:
            try:
                from app.config.loader import use_workspace_root, workspace_root
                # The owner thread must evaluate relative paths and shell grants in
                # the requesting child's workspace, not the parent's ContextVar.
                tool = replace(item.tool, scope=f"child:{item.child_run_id}; workspace:{item.workspace}",
                               description=(
                    f"[child {item.child_run_id}; workspace {item.workspace}] {item.tool.description}"
                )) if item.child_run_id else item.tool
                with use_workspace_root(item.workspace or workspace_root()):
                    item.result = request_tool_approval(item.policy, tool, item.action, item.arguments)
                # A child cannot turn a single approval into a run-wide auto grant.
                if item.result.feedback == "__enable_auto_execute__":
                    item.result.feedback = None
            except Exception as exc:
                item.result = ApprovalResult(False, feedback=str(exc))
        item.done.set()
