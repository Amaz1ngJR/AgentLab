"""Agent 任务清单 —— 任务状态的唯一可信来源(TaskStore)。

两个使用路径,共用同一份 Task / TaskStore:

1. 轻量路径(已有):模型用 `todo_write` 工具维护一个扁平清单,CLI 实时渲染。
   只用到 pending / in_progress / completed 三态和 replace_all / summary。

2. 编排路径(Planner / Executor / Replanner):把复杂目标拆成带依赖的任务,
   Executor 按依赖 claim 下一个可执行任务,Replanner 根据结果回写状态、证据、
   失败原因,并可追加任务。用到 dependencies / blocked / failed / evidence /
   history / snapshot。

数据是 in-memory,会话结束就丢(后续可接 SQLite 持久化 runs/tasks 表)。
所有方法在主线程调用,无并发写(引入异步执行时再加锁)。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

# ── 任务状态 ────────────────────────────────────────────────────────────────
# pending / in_progress / completed 是模型(todo_write)也能写的"简单三态";
# blocked / failed 是编排路径(Replanner)才会写的状态,不开放给 todo_write,
# 所以 _VALID_STATUS(给 todo_write 规范化用)只含简单三态,未知值落 pending。
PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
BLOCKED = "blocked"
FAILED = "failed"

# todo_write 允许模型直接写入的状态(其余规范化为 pending)
_VALID_STATUS = {PENDING, IN_PROGRESS, COMPLETED}
# 所有合法状态(含编排路径)
_ALL_STATUS = {PENDING, IN_PROGRESS, COMPLETED, BLOCKED, FAILED}
# 终态:不会再被 claim、也不计入"还有活儿没干"
_TERMINAL = {COMPLETED, FAILED}


@dataclass
class Task:
    """单个任务记录。

    轻量路径只用 id / content / status;编排路径额外用下面这些:

    id           - 稳定标识(字符串)。todo_write 用 "1"/"2";Planner 用 "t1"/"t2"。
    content      - 任务文字描述,简短一句话。
    status       - pending / in_progress / completed / blocked / failed。
    dependencies - 前置任务 id 列表;全部 completed 后本任务才可被 claim。
    evidence     - 执行后留下的证据/产出摘要(给后续任务和审计看)。
    error        - 失败或阻塞原因(failed / blocked 时填)。
    history      - 状态变更轨迹,每项是一句话,便于审计回放。
    """
    id: str
    content: str
    status: str = PENDING
    dependencies: list[str] = field(default_factory=list)
    evidence: str = ""
    error: str = ""
    history: list[str] = field(default_factory=list)


class TaskStore:
    """会话级任务清单 —— 任务状态的唯一可信来源。

    轻量路径:`replace_all` 整表替换 + `summary` 统计 + CLI 读 `all` 渲染。
    编排路径:`add` 追加 + `claim_next` 取下一个可执行任务 + `update_status`
    回写状态/证据/失败原因 + `is_done` 判断是否收工 + `snapshot` 给 UI/审计。
    """

    def __init__(self) -> None:
        self._tasks: list[Task] = []

    # ── 轻量路径(todo_write / CLI)───────────────────────────────────────────

    def replace_all(self, tasks: list[Task]) -> None:
        """把整个任务列表替换为新列表。todo_write 调它来同步状态。"""
        self._tasks = list(tasks)

    def all(self) -> list[Task]:
        """返回任务的深拷贝快照(避免外部修改影响内部状态)。"""
        return copy.deepcopy(self._tasks)

    def clear(self) -> None:
        """清空。CLI 的 /reset 命令会调它。"""
        self._tasks = []

    def reset_failed(self) -> int:
        """把所有 failed 任务重置为 pending,返回重置数量。供 /resume 用。"""
        count = 0
        for t in self._tasks:
            if t.status == FAILED:
                t.status = PENDING
                count += 1
        return count

    def is_empty(self) -> bool:
        return not self._tasks

    def summary(self) -> dict[str, int]:
        """统计各状态任务数,供 CLI 渲染汇总行使用。

        始终包含 total / pending / in_progress / completed 四个键(向后兼容
        旧 CLI 与测试),并额外带上 blocked / failed(编排路径用)。
        """
        out = {
            "total": len(self._tasks),
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "blocked": 0,
            "failed": 0,
        }
        for t in self._tasks:
            if t.status in out:
                out[t.status] += 1
        return out

    # ── 编排路径(Planner / Executor / Replanner)─────────────────────────────

    def add(self, task: Task) -> None:
        """追加一个任务；重试任务应带有限次数，不重复创建相同补救工作。"""
        if self.get(task.id) is None:
            self._tasks.append(task)

    def open_tasks(self) -> list[Task]:
        """返回当前仍需处理的任务快照。"""
        return [copy.deepcopy(t) for t in self._tasks if t.status not in _TERMINAL]

    def extend(self, tasks: list[Task]) -> None:
        """批量追加。Planner 写初始计划、Replanner 追加新任务时用。"""
        for t in tasks:
            self.add(t)

    def get(self, task_id: str) -> Task | None:
        """按 id 取任务(返回内部引用,供编排逻辑就地更新)。"""
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None

    def update_status(
        self,
        task_id: str,
        status: str,
        *,
        evidence: str | None = None,
        error: str | None = None,
        note: str | None = None,
    ) -> Task | None:
        """更新任务状态并追加一条 history。未知状态规范化为 pending。

        evidence / error 传 None 表示"不改";传字符串(含空串)表示覆盖。
        note 是给 history 的可读说明;不传则用状态名兜底。
        """
        task = self.get(task_id)
        if task is None:
            return None
        if status not in _ALL_STATUS:
            status = PENDING
        task.status = status
        if evidence is not None:
            task.evidence = evidence
        if error is not None:
            task.error = error
        task.history.append(note or f"-> {status}")
        return task

    def claim_next(self) -> Task | None:
        """取下一个可执行任务:pending 且依赖全部 completed。

        把它置为 in_progress 后返回(返回内部引用)。没有可执行任务时返回 None
        —— 可能是全部做完,也可能是剩下的都被未满足的依赖卡住(见 is_blocked)。
        """
        for t in self._tasks:
            if t.status != PENDING:
                continue
            if self._deps_satisfied(t):
                t.status = IN_PROGRESS
                t.history.append("-> in_progress (claimed)")
                return t
        return None

    def _deps_satisfied(self, task: Task) -> bool:
        for dep_id in task.dependencies:
            dep = self.get(dep_id)
            # 依赖不存在视为已满足(容忍模型写错依赖,不至于永久卡死)
            if dep is not None and dep.status != COMPLETED:
                return False
        return True

    def has_runnable(self) -> bool:
        """是否还有"现在就能跑"的任务(pending 且依赖满足)。"""
        return any(
            t.status == PENDING and self._deps_satisfied(t) for t in self._tasks
        )

    def has_open(self) -> bool:
        """是否还有未到终态的任务(pending / in_progress / blocked)。"""
        return any(t.status not in _TERMINAL for t in self._tasks)

    def is_done(self) -> bool:
        """是否收工:没有任何任务,或全部任务都到终态(completed/failed)。"""
        return not self.has_open()

    def is_stalled(self) -> bool:
        """卡死:还有未完成任务,但没有一个现在能跑(依赖被 failed/blocked 卡住)。"""
        return self.has_open() and not self.has_runnable()

    def snapshot(self) -> list[dict]:
        """导出 UI / 审计用的纯数据快照(不含内部引用)。"""
        return [
            {
                "id": t.id,
                "content": t.content,
                "status": t.status,
                "dependencies": list(t.dependencies),
                "evidence": t.evidence,
                "error": t.error,
                "history": list(t.history),
            }
            for t in self._tasks
        ]

    def restore(self, snapshot: list[dict]) -> None:
        """从 snapshot()(或 SQLite 读回的同构 dict 列表)重建任务列表。

        就地替换内部列表(不重建 TaskStore 实例),这样持有本 store 引用的
        AgentSession / spinner 不会拿到失效引用。非法项跳过,容忍脏数据。
        """
        tasks: list[Task] = []
        for item in snapshot or []:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("id") or "").strip()
            content = str(item.get("content") or "")
            if not tid:
                continue
            status = item.get("status") or PENDING
            if status not in _ALL_STATUS:
                status = PENDING
            deps_raw = item.get("dependencies") or []
            deps = [str(d) for d in deps_raw] if isinstance(deps_raw, list) else []
            hist_raw = item.get("history") or []
            hist = [str(h) for h in hist_raw] if isinstance(hist_raw, list) else []
            tasks.append(Task(
                id=tid, content=content, status=status, dependencies=deps,
                evidence=str(item.get("evidence") or ""),
                error=str(item.get("error") or ""),
                history=hist,
            ))
        self._tasks = tasks
