"""离线测试：TaskStore 与 todo_write 工具。"""
from app.agent.tasks import COMPLETED, IN_PROGRESS, PENDING, Task, TaskStore
from app.tools.builtin.todo import make_todo_write_tool


def test_task_store_replace_all_overwrites():
    store = TaskStore()
    store.replace_all([Task("1", "a", PENDING), Task("2", "b", IN_PROGRESS)])
    store.replace_all([Task("3", "c", COMPLETED)])
    assert [t.id for t in store.all()] == ["3"]


def test_task_store_summary():
    store = TaskStore()
    store.replace_all([
        Task("1", "a", COMPLETED),
        Task("2", "b", IN_PROGRESS),
        Task("3", "c", PENDING),
        Task("4", "d", PENDING),
    ])
    s = store.summary()
    assert s == {"total": 4, "completed": 1, "in_progress": 1, "pending": 2}


def test_task_store_clear():
    store = TaskStore()
    store.replace_all([Task("1", "a", PENDING)])
    store.clear()
    assert store.is_empty()


def test_task_store_all_returns_copy():
    """all() 返回副本,外部修改不应影响内部状态。"""
    store = TaskStore()
    store.replace_all([Task("1", "a", PENDING)])
    snapshot = store.all()
    snapshot.clear()
    assert len(store.all()) == 1


def test_todo_write_replaces_tasks():
    store = TaskStore()
    tool = make_todo_write_tool(store)
    out = tool.executor({"tasks": [
        {"id": "1", "content": "task one", "status": "pending"},
        {"id": "2", "content": "task two", "status": "in_progress"},
    ]})
    assert "updated 2 tasks" in out
    assert "1 in progress" in out
    assert [t.id for t in store.all()] == ["1", "2"]
    assert store.all()[1].status == "in_progress"


def test_todo_write_normalizes_unknown_status():
    """模型偶尔会写 'done' / 'wip' 等变体,规范化为 pending(避免崩溃)。"""
    store = TaskStore()
    tool = make_todo_write_tool(store)
    tool.executor({"tasks": [
        {"id": "1", "content": "x", "status": "done"},
    ]})
    assert store.all()[0].status == "pending"


def test_todo_write_rejects_missing_content():
    store = TaskStore()
    tool = make_todo_write_tool(store)
    out = tool.executor({"tasks": [{"id": "1", "content": "", "status": "pending"}]})
    assert out.startswith("refused:")
    assert store.is_empty()


def test_todo_write_rejects_non_list_input():
    store = TaskStore()
    tool = make_todo_write_tool(store)
    out = tool.executor({"tasks": "not a list"})
    assert out.startswith("refused:")
    assert store.is_empty()


def test_todo_write_does_not_require_approval():
    """更新任务清单是无副作用操作,不应触发审批。"""
    tool = make_todo_write_tool(TaskStore())
    assert tool.requires_approval is False
    assert tool.name == "todo_write"
