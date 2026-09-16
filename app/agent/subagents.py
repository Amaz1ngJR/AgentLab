"""受控子 Agent 委托调度与冲突治理。

子 Agent 的并行边界：
- 只读任务可共享主工作区；
- 写任务各自使用独立 Git worktree；
- 子任务之间按 dependencies 调度，依赖失败则阻塞；
- 调度器只收集变更和生成合并计划，不自动合并主分支；
- 同一文件被多个子 Agent 修改、或主分支在派发后发生漂移时，结果进入 conflict。

模型调用和工具权限由调用方提供的 delegate 决定；本模块不授予额外权限。
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from app.agent.cancel import CancelToken, Cancelled
from app.agent.events import RunEvent, SUBAGENT_COMPLETED, SUBAGENT_STARTED
from app.config.loader import use_workspace_root
from app.workspace.worktree import WorktreeInfo, WorktreeManager


SUBAGENT_STATUSES = frozenset({"pending", "running", "succeeded", "failed", "blocked", "cancelled", "conflict"})


@dataclass(frozen=True)
class SubagentSpec:
    """一次子 Agent 委托定义。"""

    subagent_id: str
    instruction: str
    role: str = "executor"
    dependencies: tuple[str, ...] = ()
    read_only: bool = False
    worktree_id: str | None = None
    tools: tuple[str, ...] = ()
    max_steps: int = 8
    max_tool_calls: int = 30
    timeout_seconds: int = 300
    file_scope: tuple[str, ...] = ()
    retry_limit: int = 0
    output_limit: int = 20_000


@dataclass
class SubagentResult:
    """子 Agent 运行结果和交付前冲突信息。"""

    subagent_id: str
    role: str
    status: str = "pending"
    output: str = ""
    error: str = ""
    workspace: Path | None = None
    worktree: WorktreeInfo | None = None
    changed_files: list[str] = field(default_factory=list)
    conflict_files: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def mergeable(self) -> bool:
        """当前结果是否满足生成合并建议的条件。"""
        return (
            self.status == "succeeded"
            and not self.conflict_files
            and self.worktree is not None
            and bool(self.worktree and self.worktree.base_commit)
        )


Delegate = Callable[[SubagentSpec, Path, CancelToken], str | SubagentResult | None]
EventSink = Callable[[RunEvent], None]


class SubagentCoordinator:
    """并行调度独立子 Agent，并在交付前做保守冲突检查。"""

    def __init__(
        self,
        worktree_manager: WorktreeManager,
        delegate: Delegate,
        *,
        max_workers: int = 2,
        on_event: EventSink | None = None,
        storage: Any = None,
        loop_id: str | None = None,
    ):
        if max_workers <= 0:
            raise ValueError("max_workers 必须 > 0")
        self.worktree_manager = worktree_manager
        self.delegate = delegate
        self.max_workers = max_workers
        self.on_event = on_event or (lambda event: None)
        self.storage = storage
        self.loop_id = loop_id
        self._approval_lock = threading.Lock()

    def run(
        self,
        specs: list[SubagentSpec],
        *,
        cancel: CancelToken | None = None,
    ) -> dict[str, SubagentResult]:
        """按依赖并行执行子 Agent，返回稳定按 id 排序的结果。

        任务本身不会被自动合并；调用方应在检查 ``merge_plan`` 后另行审批。
        """
        cancel = cancel or CancelToken()
        by_id = self._validate_specs(specs)
        results: dict[str, SubagentResult] = {}
        pending = set(by_id)
        running: dict[Future[SubagentResult], SubagentSpec] = {}

        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="subagent") as pool:
            while pending or running:
                cancel.raise_if_cancelled()
                self._block_failed_dependencies(pending, by_id, results)
                pending.difference_update(results)

                for subagent_id in sorted(pending):
                    if len(running) >= self.max_workers:
                        break
                    spec = by_id[subagent_id]
                    if not self._dependencies_succeeded(spec, results):
                        continue
                    result = self._start_result(spec)
                    if spec.read_only:
                        workspace = self.worktree_manager.repo_root
                        result.workspace = workspace
                        result.status = "running"
                        self._record_started(result)
                        future = pool.submit(self._run_one, spec, workspace, result, cancel)
                    else:
                        worktree = self.worktree_manager.create(
                            spec.worktree_id or self._worktree_id(spec),
                        )
                        result.worktree = worktree
                        result.workspace = worktree.path
                        result.status = "running"
                        self._record_started(result)
                        future = pool.submit(self._run_one, spec, worktree.path, result, cancel)
                    running[future] = spec
                    pending.remove(subagent_id)

                if not running:
                    # 未完成但没有可运行任务，只可能是循环依赖或非法依赖。
                    for subagent_id in sorted(pending):
                        results[subagent_id] = self._blocked_result(
                            by_id[subagent_id], "依赖循环或依赖不存在"
                        )
                    pending.clear()
                    break

                done, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
                for future in done:
                    spec = running.pop(future)
                    try:
                        result = future.result()
                    except Cancelled:
                        result = self._cancelled_result(spec)
                    except Exception as exc:  # 子 Agent 故障不能吞掉其他任务结果
                        result = self._failed_result(spec, f"{type(exc).__name__}: {exc}")
                    results[spec.subagent_id] = result
                    self._record_finished(result)
                    self._emit_completed(result)

        self._apply_conflicts(results)
        return {subagent_id: results[subagent_id] for subagent_id in sorted(results)}

    def merge_plan(self, results: dict[str, SubagentResult]) -> list[dict[str, Any]]:
        """生成只读合并计划，不执行 git merge。

        ``conflict`` 结果永远不会出现在 ``mergeable=True`` 项中；主分支发生漂移
        时所有写入结果均需重新基于最新主分支验证。
        """
        plan: list[dict[str, Any]] = []
        current_commit = self.worktree_manager.current_commit()
        for subagent_id in sorted(results):
            result = results[subagent_id]
            item: dict[str, Any] = {
                "subagent_id": subagent_id,
                "status": result.status,
                "changed_files": list(result.changed_files),
                "conflict_files": list(result.conflict_files),
                "mergeable": False,
            }
            if result.worktree is None:
                item["reason"] = "只读子 Agent 没有待合并 worktree"
            elif result.status != "succeeded":
                item["reason"] = f"子 Agent 状态为 {result.status}"
            elif result.conflict_files:
                item["reason"] = "多个子 Agent 修改了相同文件"
            elif current_commit != result.worktree.base_commit:
                item["reason"] = "主分支在子 Agent 派发后发生变化，需要重新验证"
            elif not self.worktree_manager.has_commits(result.worktree):
                item["reason"] = "worktree 没有已提交改动，不能生成 merge 命令"
            else:
                item["mergeable"] = True
                item["branch"] = f"worktree/{result.worktree.worktree_id}"
                item["suggestion"] = self.worktree_manager.merge_suggestion(result.worktree)
            plan.append(item)
        return plan

    def _validate_specs(self, specs: list[SubagentSpec]) -> dict[str, SubagentSpec]:
        by_id: dict[str, SubagentSpec] = {}
        for spec in specs:
            if not spec.subagent_id.strip():
                raise ValueError("subagent_id 不能为空")
            if spec.subagent_id in by_id:
                raise ValueError(f"重复的 subagent_id: {spec.subagent_id}")
            if not spec.instruction.strip():
                raise ValueError(f"子 Agent 指令不能为空: {spec.subagent_id}")
            if any(dep == spec.subagent_id for dep in spec.dependencies):
                raise ValueError(f"子 Agent 不能依赖自身: {spec.subagent_id}")
            by_id[spec.subagent_id] = spec
        unknown = sorted({dep for spec in specs for dep in spec.dependencies if dep not in by_id})
        if unknown:
            raise ValueError(f"子 Agent 依赖不存在: {', '.join(unknown)}")
        return by_id

    @staticmethod
    def _dependencies_succeeded(spec: SubagentSpec, results: dict[str, SubagentResult]) -> bool:
        return all(
            dep in results and results[dep].status == "succeeded"
            for dep in spec.dependencies
        )

    @staticmethod
    def _block_failed_dependencies(
        pending: set[str], by_id: dict[str, SubagentSpec], results: dict[str, SubagentResult]
    ) -> None:
        for subagent_id in sorted(pending):
            spec = by_id[subagent_id]
            failed = [dep for dep in spec.dependencies if dep in results and results[dep].status != "succeeded"]
            if failed:
                results[subagent_id] = SubagentResult(
                    subagent_id=subagent_id,
                    role=spec.role,
                    status="blocked",
                    error=f"依赖未成功: {', '.join(failed)}",
                    started_at=datetime.utcnow().isoformat(),
                    finished_at=datetime.utcnow().isoformat(),
                )

    def _run_one(
        self,
        spec: SubagentSpec,
        workspace: Path,
        result: SubagentResult,
        cancel: CancelToken,
    ) -> SubagentResult:
        try:
            cancel.raise_if_cancelled()
            with use_workspace_root(workspace):
                delegated = self.delegate(spec, workspace, cancel)
            if isinstance(delegated, SubagentResult):
                delegated.subagent_id = spec.subagent_id
                delegated.role = spec.role
                delegated.workspace = workspace
                delegated.worktree = result.worktree
                delegated.started_at = result.started_at
                delegated.finished_at = datetime.utcnow().isoformat()
                if delegated.status == "pending":
                    delegated.status = "succeeded"
                return delegated
            result.output = "" if delegated is None else str(delegated)
            result.status = "succeeded"
        except Cancelled:
            raise
        except Exception as exc:
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
        result.finished_at = datetime.utcnow().isoformat()
        if result.worktree is not None:
            result.changed_files = self.worktree_manager.changed_files(result.worktree)
        return result

    def _apply_conflicts(self, results: dict[str, SubagentResult]) -> None:
        owners: dict[str, list[str]] = {}
        for subagent_id, result in results.items():
            if result.status != "succeeded":
                continue
            for path in result.changed_files:
                owners.setdefault(path, []).append(subagent_id)
        for path, subagent_ids in owners.items():
            if len(subagent_ids) < 2:
                continue
            for subagent_id in subagent_ids:
                result = results[subagent_id]
                result.status = "conflict"
                if path not in result.conflict_files:
                    result.conflict_files.append(path)
                result.error = f"与子 Agent {', '.join(x for x in subagent_ids if x != subagent_id)} 修改相同文件"
                self._emit_completed(result)

    def _worktree_id(self, spec: SubagentSpec) -> str:
        return f"subagent-{spec.subagent_id}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _start_result(spec: SubagentSpec) -> SubagentResult:
        return SubagentResult(
            subagent_id=spec.subagent_id,
            role=spec.role,
            started_at=datetime.utcnow().isoformat(),
        )

    @staticmethod
    def _failed_result(spec: SubagentSpec, error: str) -> SubagentResult:
        now = datetime.utcnow().isoformat()
        return SubagentResult(spec.subagent_id, spec.role, "failed", error=error, started_at=now, finished_at=now)

    @staticmethod
    def _blocked_result(spec: SubagentSpec, error: str) -> SubagentResult:
        now = datetime.utcnow().isoformat()
        return SubagentResult(spec.subagent_id, spec.role, "blocked", error=error, started_at=now, finished_at=now)

    @staticmethod
    def _cancelled_result(spec: SubagentSpec) -> SubagentResult:
        now = datetime.utcnow().isoformat()
        return SubagentResult(spec.subagent_id, spec.role, "cancelled", error="用户取消", started_at=now, finished_at=now)

    def _emit_started(self, result: SubagentResult) -> None:
        self.on_event(RunEvent(
            kind=SUBAGENT_STARTED,
            text=f"开始子 Agent: {result.subagent_id}",
            payload={"subagent_id": result.subagent_id, "role": result.role, "workspace": str(result.workspace or "")},
        ))

    def _emit_completed(self, result: SubagentResult) -> None:
        self.on_event(RunEvent(
            kind=SUBAGENT_COMPLETED,
            text=f"子 Agent {result.subagent_id}: {result.status}",
            payload={
                "subagent_id": result.subagent_id,
                "role": result.role,
                "status": result.status,
                "changed_files": list(result.changed_files),
                "conflict_files": list(result.conflict_files),
                "error": result.error,
            },
        ))

    def _record_started(self, result: SubagentResult) -> None:
        self._emit_started(result)
        if self.storage is not None and self.loop_id:
            from app.storage.loop_store import save_subagent_run
            save_subagent_run(self.storage.conn, {
                "id": f"subagent-{self.loop_id}-{result.subagent_id}",
                "loop_id": self.loop_id,
                "role": result.role,
                "status": "running",
                "input_summary": result.subagent_id,
                "started_at": result.started_at,
            })

    def _record_finished(self, result: SubagentResult) -> None:
        if self.storage is not None and self.loop_id:
            from app.storage.loop_store import finish_subagent_run
            finish_subagent_run(
                self.storage.conn,
                f"subagent-{self.loop_id}-{result.subagent_id}",
                status=result.status,
                output_summary=result.output or result.error,
                finished_at=result.finished_at,
            )
