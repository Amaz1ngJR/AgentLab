"""离线测试：TaskStore 与 todo_write 工具。"""
from app.agent.tasks import (
    BLOCKED,
    COMPLETED,
    FAILED,
    IN_PROGRESS,
    PENDING,
    Task,
    TaskStore,
)
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
    assert s == {"total": 4, "completed": 1, "in_progress": 1, "pending": 2,
                 "blocked": 0, "failed": 0}


def test_task_store_clear():
    store = TaskStore()
    store.replace_all([Task("1", "a", PENDING)])
    store.clear()
    assert store.is_empty()


def test_task_store_reset_failed():
    """reset_failed 把 failed 任务改回 pending,返回重置数量,不动其它状态。"""
    store = TaskStore()
    store.replace_all([
        Task("1", "a", COMPLETED),
        Task("2", "b", FAILED),
        Task("3", "c", FAILED),
        Task("4", "d", PENDING),
    ])
    count = store.reset_failed()
    assert count == 2
    by_id = {t.id: t.status for t in store.all()}
    assert by_id["1"] == COMPLETED   # 已完成不动
    assert by_id["2"] == PENDING     # failed -> pending
    assert by_id["3"] == PENDING     # failed -> pending
    assert by_id["4"] == PENDING     # 本来就 pending


def test_task_store_reset_failed_none():
    """没有 failed 任务时返回 0。"""
    store = TaskStore()
    store.replace_all([Task("1", "a", COMPLETED), Task("2", "b", PENDING)])
    assert store.reset_failed() == 0


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


# ── 编排路径:依赖 / claim / 状态回写 / 收工判定 ────────────────────────────────


def test_add_ignores_duplicate_id():
    store = TaskStore()
    store.add(Task("t1", "first"))
    store.add(Task("t1", "second"))  # 同 id 不覆盖
    assert len(store.all()) == 1
    assert store.get("t1").content == "first"


def test_claim_next_respects_dependencies():
    store = TaskStore()
    store.extend([
        Task("t1", "first"),
        Task("t2", "needs t1", dependencies=["t1"]),
    ])
    # t2 依赖 t1,未完成前只能 claim 到 t1
    first = store.claim_next()
    assert first.id == "t1"
    assert first.status == IN_PROGRESS
    # t1 还在进行,t2 依赖未满足,claim 不到东西
    assert store.claim_next() is None
    # t1 完成后,t2 才可被 claim
    store.update_status("t1", COMPLETED)
    second = store.claim_next()
    assert second.id == "t2"


def test_claim_next_missing_dependency_treated_satisfied():
    """依赖一个不存在的 id 不应永久卡死(容忍模型写错依赖)。"""
    store = TaskStore()
    store.add(Task("t1", "x", dependencies=["nope"]))
    assert store.claim_next().id == "t1"


def test_update_status_records_evidence_error_history():
    store = TaskStore()
    store.add(Task("t1", "x"))
    store.update_status("t1", FAILED, evidence="ran tool", error="boom", note="failed: boom")
    t = store.get("t1")
    assert t.status == FAILED
    assert t.evidence == "ran tool"
    assert t.error == "boom"
    assert t.history[-1] == "failed: boom"


def test_is_done_and_stalled():
    store = TaskStore()
    store.extend([
        Task("t1", "a"),
        Task("t2", "b", dependencies=["t1"]),
    ])
    assert not store.is_done()
    # t1 失败 -> t2 依赖永远满足不了 -> 卡死
    store.update_status("t1", FAILED)
    assert store.is_stalled()
    assert not store.is_done()
    assert not store.has_runnable()


def test_is_done_when_all_terminal():
    store = TaskStore()
    store.extend([Task("t1", "a"), Task("t2", "b")])
    store.update_status("t1", COMPLETED)
    store.update_status("t2", FAILED)
    assert store.is_done()  # completed + failed 都是终态
    assert not store.is_stalled()


def test_snapshot_is_plain_data():
    store = TaskStore()
    store.add(Task("t1", "x", dependencies=["d"], evidence="e", error="r"))
    snap = store.snapshot()
    assert snap == [{
        "id": "t1", "content": "x", "status": PENDING,
        "dependencies": ["d"], "evidence": "e", "error": "r", "history": [],
    }]


def test_summary_counts_blocked_and_failed():
    store = TaskStore()
    store.extend([Task("t1", "a"), Task("t2", "b"), Task("t3", "c")])
    store.update_status("t1", BLOCKED)
    store.update_status("t2", FAILED)
    s = store.summary()
    assert s["blocked"] == 1
    assert s["failed"] == 1
    assert s["pending"] == 1
