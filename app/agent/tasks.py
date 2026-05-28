"""Agent 任务清单 —— 模型用 todo_write 工具维护的 in-memory 任务状态。

使用场景:
  模型在拿到一个复杂任务时(例如"重构这三个模块"),会先用 todo_write 列出
  自己要做的子任务,然后边执行边更新状态。CLI 把这个清单实时显示在 spinner
  上方,让用户能看到 Agent 当前的进度规划。

数据是 in-memory,会话结束就丢(P1 后续可以接 SQLite 持久化)。
"""
from __future__ import annotations

from dataclasses import dataclass

# 合法的任务状态。其他值会在 TaskStore.replace_all 里被规范化为 pending。
PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
_VALID_STATUS = {PENDING, IN_PROGRESS, COMPLETED}


@dataclass
class Task:
    """单个任务的轻量记录。

    id      - 模型自定的稳定标识(通常是字符串 "1" / "2" 或描述短码),
              更新时按 id 比对,允许保持顺序但内容变化
    content - 任务文字描述,简短一句话
    status  - "pending" / "in_progress" / "completed"
    """
    id: str
    content: str
    status: str = PENDING


class TaskStore:
    """会话级任务清单。AgentSession 持有一个,todo_write 工具更新它,CLI 读它渲染。

    本类是简单包装,所有方法在主线程调用(CLI 渲染线程只读快照,无并发写)。
    如果未来引入异步执行需要并发,再加锁。
    """

    def __init__(self) -> None:
        self._tasks: list[Task] = []

    def replace_all(self, tasks: list[Task]) -> None:
        """把整个任务列表替换为新列表。todo_write 调它来同步状态。"""
        self._tasks = list(tasks)

    def all(self) -> list[Task]:
        """返回任务的快照副本(避免外部修改影响内部状态)。"""
        return list(self._tasks)

    def clear(self) -> None:
        """清空。CLI 的 /reset 命令会调它。"""
        self._tasks = []

    def summary(self) -> dict[str, int]:
        """统计各状态任务数,供 CLI 渲染汇总行使用。

        返回 {"total", "pending", "in_progress", "completed"}。
        """
        out = {"total": len(self._tasks), "pending": 0, "in_progress": 0, "completed": 0}
        for t in self._tasks:
            if t.status in out:
                out[t.status] += 1
        return out

    def is_empty(self) -> bool:
        return not self._tasks
