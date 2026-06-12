"""todo_write 工具 —— 让模型维护本会话的任务清单。

使用场景:
  模型在执行复杂多步任务时,主动调 todo_write 把自己要做的事分成子任务,
  并随着进度更新 status。CLI 实时把任务清单渲染在 spinner 上方,用户能
  直观看到 Agent 当前在哪个步骤、还有多少没做。

为什么是 make_todo_write_tool 工厂而不是模块级 Tool 实例:
  Tool.executor 需要持有 TaskStore 引用才能写入,store 是会话级状态,
  不同 AgentSession 应该有各自的 store(避免多会话相互覆盖)。所以用
  闭包工厂在创建 session 时把对应的 store 注入进去。

工具不需要审批(只是更新状态,没有副作用)。
"""
from __future__ import annotations

from app.agent.tasks import COMPLETED, IN_PROGRESS, PENDING, Task, TaskStore, _VALID_STATUS
from app.tools.registry import Tool


def make_todo_write_tool(store: TaskStore) -> Tool:
    """返回一个绑定到指定 TaskStore 的 todo_write 工具实例。"""

    def _todo_write(args: dict) -> str:
        raw = args.get("tasks") or []
        if not isinstance(raw, list):
            return "refused: 'tasks' must be a list"

        tasks: list[Task] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                return f"refused: tasks[{i}] is not an object"
            tid = str(item.get("id") or i + 1)
            content = str(item.get("content") or "").strip()
            if not content:
                return f"refused: tasks[{i}] missing content"
            status = str(item.get("status") or PENDING)
            if status not in _VALID_STATUS:
                # 未知状态规范化为 pending,而不是报错(模型偶尔写 done / wip 等变体)
                status = PENDING
            tasks.append(Task(id=tid, content=content, status=status))

        store.replace_all(tasks)
        s = store.summary()
        return (
            f"updated {s['total']} tasks "
            f"({s['completed']} done, {s['in_progress']} in progress, {s['pending']} pending)"
        )

    return Tool(
        name="todo_write",
        description=(
            "维护本次会话的任务清单。模型应在面对复杂多步任务时调用此工具:"
            "先列出子任务,再随着执行进度更新每项的 status。"
            "每次调用会用 tasks 参数完整替换整个清单(不是增量更新),"
            "所以要把所有任务一并传入,包括已完成的。"
            f"status 取值: {PENDING} / {IN_PROGRESS} / {COMPLETED}。"
            "CLI 会实时把清单显示在屏幕上,用户随时可看到进度。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "完整的任务列表,顺序即显示顺序",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "稳定标识(字符串),后续更新按 id 对应",
                            },
                            "content": {
                                "type": "string",
                                "description": "任务描述,简短一句话",
                            },
                            "status": {
                                "type": "string",
                                "enum": [PENDING, IN_PROGRESS, COMPLETED],
                                "description": f"{PENDING} / {IN_PROGRESS} / {COMPLETED} 之一",
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["tasks"],
        },
        executor=_todo_write,
        requires_approval=False,  # 只更新内存状态,不操作环境
    )
