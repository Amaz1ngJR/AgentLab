"""离线测试：SessionRouter 命令解析与 session 生命周期。"""
from app.agent.profiles import AgentProfile
from app.agent.runtime import AgentSession
from app.agent.session_router import SessionRouter
from app.agent.approval import AutoApprove
from app.storage import Storage
from app.tools.registry import ToolRegistry
from app.models.protocol import ModelResponse, ToolResult


class _FakeRouter:
    model = "fake"
    provider = "fake"
    def create_message(self, messages, **kw):
        return ModelResponse(text="ok", tool_calls=[], usage={}, provider_payload=[])
    def format_tool_results(self, results):
        return []


def _make_router(storage, profiles, memory_policy=None):
    def factory(profile: AgentProfile, session_id: str) -> AgentSession:
        session = AgentSession(
            llm=_FakeRouter(),
            tools=ToolRegistry(),
            approval=AutoApprove(),
            system_prompt="test",
        )
        if memory_policy is not None:
            session.mem_policy = memory_policy
            session.agent_profile = profile
        return session
    return SessionRouter(
        storage=storage,
        session_factory=factory,
        profiles=profiles,
        default_profile_id="default",
    )


def _profiles():
    return {
        "default": AgentProfile("default", "默认助手", "cloud_claude"),
        "coder":   AgentProfile("coder",   "代码助手", "cloud_claude"),
    }


def test_lifecycle_saves_read_write_summary_once(tmp_path, monkeypatch):
    from app.memory import ReadWriteMemory
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    db = Storage(tmp_path / "db")
    r = _make_router(db, _profiles(), ReadWriteMemory(db))
    sid = r.new("default")
    r.current.messages = [{"role": "user", "content": "hello"}]
    second = r.new("default")
    assert len(db.search_memories("", agent_id="default", workspace=str(tmp_path / "workspace"))) == 1
    r.switch(sid)
    r.current.messages.append({"role": "assistant", "content": "updated"})
    r.switch(second)
    rows = db.search_memories("", agent_id="default", workspace=str(tmp_path / "workspace"))
    assert len(rows) == 1
    assert "updated" in rows[0]["content"]
    r.close_all()
    assert len(db.search_memories("", agent_id="default", workspace=str(tmp_path / "workspace"))) == 1


def test_archive_and_delete_save_summary_before_removal(tmp_path, monkeypatch):
    from app.memory import ReadWriteMemory
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    db = Storage(tmp_path / "db")
    r = _make_router(db, _profiles(), ReadWriteMemory(db))
    sid = r.new("default")
    r.current.messages = [{"role": "user", "content": "archive me"}]
    r.archive()
    assert len(db.search_memories("", agent_id="default", workspace=str(tmp_path / "workspace"))) == 1

    sid = r.new("default")
    r.current.messages = [{"role": "user", "content": "delete me"}]
    r.delete(sid)
    rows = db.search_memories("delete me", agent_id="default", workspace=str(tmp_path / "workspace"))
    assert len(rows) == 1



    r = _make_router(Storage(tmp_path / "db"), _profiles())
    sid = r.new(agent_id="default", title="测试会话")
    assert r.current_id == sid
    assert r.current is not None


def test_two_sessions_are_isolated(tmp_path):
    r = _make_router(Storage(tmp_path / "db"), _profiles())
    s1 = r.new("default", "会话1")
    r.current.messages.append({"role": "user", "content": "hello"})
    s2 = r.new("coder", "会话2")
    # 切换到 s2，消息不串
    assert r.current.messages == []
    # 切回 s1 消息还在
    r.switch(s1)
    assert len(r.current.messages) == 1


def test_switch_restores_from_sqlite(tmp_path):
    db = Storage(tmp_path / "db")
    r = _make_router(db, _profiles())
    sid = r.new("default")
    r.current.messages = [{"role": "user", "content": "saved"}]
    r.persist_current()
    # 从内存删掉，模拟"重新加载"
    del r._sessions[sid]
    r.current_id = None
    assert r.switch(sid)
    assert r.current.messages[0]["content"] == "saved"


def test_switch_restores_task_snapshot(tmp_path):
    """persist 会存任务快照;switch 从 SQLite 恢复时把任务也接回 task_store。"""
    from app.agent.tasks import COMPLETED, PENDING, Task
    db = Storage(tmp_path / "db")
    r = _make_router(db, _profiles())
    sid = r.new("default")
    r.current.task_store.extend([
        Task("t1", "第一步", COMPLETED, evidence="done"),
        Task("t2", "第二步", PENDING, dependencies=["t1"]),
    ])
    r.persist_current()
    # 模拟重启:换内存,从库恢复
    del r._sessions[sid]
    r.current_id = None
    assert r.switch(sid)
    snap = r.current.task_store.snapshot()
    assert [t["id"] for t in snap] == ["t1", "t2"]
    assert snap[0]["status"] == COMPLETED
    assert snap[0]["evidence"] == "done"
    assert snap[1]["dependencies"] == ["t1"]


def test_resume_or_new_creates_when_empty(tmp_path):
    """首次启动:没有历史 session → 新建。"""
    r = _make_router(Storage(tmp_path / "db"), _profiles())
    sid, resumed = r.resume_or_new(agent_id="default")
    assert resumed is False
    assert r.current_id == sid
    assert len(r.list_sessions()) == 1


def test_resume_or_new_resumes_latest(tmp_path):
    """再次启动:有历史 session → 恢复最近一个,不新建。"""
    db = Storage(tmp_path / "db")
    r = _make_router(db, _profiles())
    sid = r.new("default")
    r.current.messages = [{"role": "user", "content": "earlier"}]
    r.persist_current()
    # 模拟重启:新 router 接同一个库
    r2 = _make_router(db, _profiles())
    resumed_id, resumed = r2.resume_or_new(agent_id="default")
    assert resumed is True
    assert resumed_id == sid                       # 恢复的是同一个 session
    assert len(r2.list_sessions()) == 1            # 没堆出新会话
    assert r2.current.messages[0]["content"] == "earlier"  # 历史消息回来了


def test_resume_or_new_skips_archived(tmp_path):
    """归档过的不该被恢复 → 新建。"""
    db = Storage(tmp_path / "db")
    r = _make_router(db, _profiles())
    r.new("default")
    r.archive()                                    # 归档后列表为空
    r2 = _make_router(db, _profiles())
    _, resumed = r2.resume_or_new(agent_id="default")
    assert resumed is False                        # 归档的不恢复,新建


def test_resume_or_new_skips_empty_sessions(tmp_path):
    """有多个 session,最近的是空壳(0 消息)→ 跳过空壳,恢复有历史的那个。"""
    db = Storage(tmp_path / "db")
    r = _make_router(db, _profiles())
    # 第一个 session:有历史
    sid1 = r.new("default")
    r.current.messages = [{"role": "user", "content": "first"}]
    r.persist_current()
    # 第二个 session:新建但没对话,0 消息(模拟空壳)
    sid2 = r.new("default")
    r.persist_current()  # persist 了但 messages 空,updated_at 更新但不抢占"最近"
    # 手动让空壳的 updated_at 比有历史的更晚(模拟旧 bug 场景:空壳被 rename 过)
    import time
    time.sleep(0.01)  # 确保时间戳不同
    db.update_session_title(sid2, "empty-but-recent")
    # 模拟重启:新 router 应恢复 sid1(有消息),而非 sid2(空壳)
    r2 = _make_router(db, _profiles())
    resumed_id, resumed = r2.resume_or_new(agent_id="default")
    assert resumed is True
    assert resumed_id == sid1  # 跳过空壳,恢复有历史的
    assert r2.current.messages[0]["content"] == "first"


def test_persist_current_touches_updated_at(tmp_path):
    """persist_current 应更新 session 的 updated_at,标记最后活跃时间。"""
    db = Storage(tmp_path / "db")
    r = _make_router(db, _profiles())
    sid = r.new("default")
    sess_before = db.get_session(sid)
    updated_before = sess_before["updated_at"]
    # 对话并 persist
    import time
    time.sleep(0.01)  # 确保时间戳变化
    r.current.messages = [{"role": "user", "content": "new msg"}]
    r.persist_current()
    sess_after = db.get_session(sid)
    updated_after = sess_after["updated_at"]
    assert updated_after > updated_before  # updated_at 被 touch 了


def test_archive_removes_session(tmp_path):
    r = _make_router(Storage(tmp_path / "db"), _profiles())
    r.new("default")
    r.archive()
    assert r.current_id is None
    assert r.list_sessions() == []


def test_delete_current_session(tmp_path):
    db = Storage(tmp_path / "db")
    r = _make_router(db, _profiles())
    sid = r.new("default")
    assert r.delete(sid) is True
    assert r.current_id is None
    assert db.get_session(sid) is None       # 硬删,库里也没了


def test_delete_other_session_keeps_current(tmp_path):
    r = _make_router(Storage(tmp_path / "db"), _profiles())
    s1 = r.new("default")
    s2 = r.new("coder")          # 当前是 s2
    assert r.delete(s1) is True  # 删的是非当前 session
    assert r.current_id == s2    # 当前不受影响


def test_delete_unknown_returns_false(tmp_path):
    r = _make_router(Storage(tmp_path / "db"), _profiles())
    assert r.delete("nope") is False


def test_handle_command_delete(tmp_path):
    r = _make_router(Storage(tmp_path / "db"), _profiles())
    sid = r.new("default")
    out = r.handle_command(f"/session delete {sid}")
    assert "已彻底删除" in out
    assert r.list_sessions() == []


def test_rename(tmp_path):
    db = Storage(tmp_path / "db")
    r = _make_router(db, _profiles())
    sid = r.new("default", "旧标题")
    r.rename("新标题")
    assert db.get_session(sid)["title"] == "新标题"


def test_handle_command_new(tmp_path):
    r = _make_router(Storage(tmp_path / "db"), _profiles())
    out = r.handle_command("/session new default 我的会话")
    assert "已创建" in out
    assert r.current_id is not None


def test_handle_command_list(tmp_path):
    r = _make_router(Storage(tmp_path / "db"), _profiles())
    r.new("default", "会话A")
    out = r.handle_command("/session list")
    assert "会话A" in out


def test_handle_command_agents(tmp_path):
    r = _make_router(Storage(tmp_path / "db"), _profiles())
    out = r.handle_command("/session agents")
    assert "coder" in out


def test_handle_command_switch(tmp_path):
    r = _make_router(Storage(tmp_path / "db"), _profiles())
    s1 = r.new("default")
    r.new("coder")
    out = r.handle_command(f"/session switch {s1}")
    assert r.current_id == s1
    assert "已切换" in out


def test_handle_unknown_subcommand(tmp_path):
    r = _make_router(Storage(tmp_path / "db"), _profiles())
    out = r.handle_command("/session bogus")
    assert "未知" in out


def test_non_session_command_returns_none(tmp_path):
    r = _make_router(Storage(tmp_path / "db"), _profiles())
    assert r.handle_command("/reset") is None
    assert r.handle_command("hello world") is None


def test_checkpoint_before_child_start_restores_dag_history_and_association(tmp_path, monkeypatch):
    import json
    from app.agent.subagent_runtime import SubagentRuntimeFactory
    from tests.unit.test_execution_plan import FakeRouter, registry, response
    from app.config.loader import use_workspace_root
    db_path = tmp_path / "crash.db"
    db = Storage(db_path)
    observed = []
    class Parent(FakeRouter):
        def fork(self):
            return FakeRouter([response("child finding")])
    plan = {"version": 1, "tasks": [
        {"id": "prepare", "content": "prepare"},
        {"id": "research", "content": "research", "dependencies": ["prepare"],
         "executor": "subagent", "subagent": {"id": "researcher", "instruction": "总结已给材料",
         "role": "research", "read_only": True, "max_steps": 2, "max_tool_calls": 2}},
    ]}
    def factory(profile, session_id):
        prepared = response("PREPARED_CONTEXT")
        prepared.provider_payload = [{"role": "assistant", "content": "PREPARED_CONTEXT"}]
        return AgentSession(Parent([response(json.dumps(plan)), prepared,
                                    response("parent synthesis")]), registry(), orchestrate=True, max_steps=16)
    router = SessionRouter(storage=db, session_factory=factory, profiles=_profiles(), default_profile_id="default")
    sid = router.new("default")
    original_delegate = SubagentRuntimeFactory.delegate
    def probe(self, spec, workspace, cancel):
        # Independent connection before any child model or tool runs; no persist_current().
        disk = Storage(db_path)
        tasks = {task["id"]: task for task in disk.load_tasks(sid)}
        records = disk.load_child_runs(sid)
        history = disk.load_messages(sid)
        assert len(records) == 1
        assert tasks["research"]["execution"]["child_run_id"] == spec.subagent_id
        assert tasks["research"]["status"] == "in_progress"
        assert tasks["research"]["dependencies"] == ["prepare"]
        assert tasks["prepare"]["status"] == "completed"
        assert "PREPARED_CONTEXT" in json.dumps(history)
        restored = SessionRouter(storage=disk, session_factory=factory, profiles=_profiles(), default_profile_id="default")
        assert restored.switch(sid)
        assert restored.current.child_records[0]["execution_status"] == "interrupted"
        assert restored.current.task_store.get("research").execution["child_run_id"] == spec.subagent_id
        assert restored.current.messages
        observed.append(True)
        disk.close()
        return original_delegate(self, spec, workspace, cancel)
    monkeypatch.setattr(SubagentRuntimeFactory, "delegate", probe)
    with use_workspace_root(tmp_path):
        answer = router.current.chat("多个 agent 调查")
        assert answer == "parent synthesis", str([(r["status"], r["error"]) for r in router.current.child_records])
    assert observed == [True]
    db.close()


def test_delegation_checkpoint_rolls_back_all_parts_on_failure(tmp_path, monkeypatch):
    import pytest
    db_path = tmp_path / "atomic.db"
    db = Storage(db_path)
    db.create_session("s", "default", "fake")
    db.save_messages("s", [{"role": "user", "content": "previous"}])
    def fail(*args):
        raise RuntimeError("disk failure")
    monkeypatch.setattr(db, "save_child_runs", fail)
    with pytest.raises(RuntimeError, match="disk failure"):
        db.save_delegation_checkpoint("s", messages=[{"role": "user", "content": "new"}],
                                      tasks=[{"id": "task"}], records=[])
    disk = Storage(db_path)
    assert disk.load_messages("s") == [{"role": "user", "content": "previous"}]
    assert disk.load_tasks("s") == []
    disk.close()
    db.close()


def test_portable_checkpoint_receipt_survives_merged_user_messages(tmp_path):
    import json
    from tests.unit.test_agent_manager import manager
    db_path = tmp_path / "receipt.db"
    db = Storage(db_path)
    router = _make_router(db, _profiles())
    session_id = router.new("default")
    session = router.current
    session.messages.append({"role": "user", "content": "original goal"})
    m = manager(tmp_path, checkpoint=session.child_checkpoint)
    sid = m.spawn("调查")["child_run_id"]
    m.wait([sid], 2, consume=False)
    assert m.deliver(session.messages)
    records = db.load_child_runs(session_id)
    assert records[0]["delivery_receipt"] == {
        "parent_run_id": m.run_id, "result_id": sid, "revision": 1,
    }
    history = db.load_messages(session_id)
    assert len(history) == 1 and history[0]["content"].startswith("original goal")
    assert json.dumps(history).count("CHILD_FINDING") == 1
    m.finish()
    db.close()

    # Only checkpoint writes above; no end-of-turn persist_current().
    for _ in range(2):
        db = Storage(db_path)
        router = _make_router(db, _profiles())
        assert router.switch(session_id)
        restored = manager(tmp_path, checkpoint=router.current.child_checkpoint)
        restored.restore_results(router.current.child_records, messages=router.current.messages)
        assert restored.result_delivered(sid)
        assert not restored.deliver(router.current.messages)
        assert json.dumps(router.current.messages).count("CHILD_FINDING") == 1
        restored.finish()
        db.close()


def test_wait_receipt_is_committed_only_after_result_enters_history(tmp_path):
    import json
    from tests.unit.test_agent_manager import manager
    db = Storage(tmp_path / "wait.db")
    router = _make_router(db, _profiles())
    session_id = router.new("default")
    session = router.current
    session.messages.append({"role": "user", "content": "original goal"})
    m = manager(tmp_path, checkpoint=session.child_checkpoint)
    sid = m.spawn("调查")["child_run_id"]
    result = m.wait([sid], 2)
    records = db.load_child_runs(session_id)
    assert records[0]["delivered_revision"] is None
    assert records[0]["delivery_receipt"] is None
    recovered = manager(tmp_path)
    history = db.load_messages(session_id)
    recovered.restore_results(records, messages=history)
    assert recovered.deliver(history)  # Crash before tool result append must not lose it.
    recovered.finish()

    # 仿真父进程路径:先绑定(tool 执行完、截断后),再入历史,最后才确认。
    output = json.dumps(result)
    m.bind_wait_tool_result("call-wait", output, False)
    session.messages.append({"role": "user", "content": output})
    m.acknowledge_tool_results([
        ToolResult(tool_call_id="call-wait", output=output, is_error=False),
    ])
    records = db.load_child_runs(session_id)
    assert records[0]["delivery_receipt"]["revision"] == 1
    history = db.load_messages(session_id)
    assert len(history) == 1  # portable history merged goal and tool output
    recovered = manager(tmp_path)
    recovered.restore_results(records, messages=history)
    assert not recovered.deliver(history)
    assert json.dumps(history).count("CHILD_FINDING") == 1
    recovered.finish()
    m.finish()
    db.close()


def test_delivery_receipt_and_history_rollback_together(tmp_path, monkeypatch):
    import pytest
    from tests.unit.test_agent_manager import manager
    db = Storage(tmp_path / "rollback.db")
    router = _make_router(db, _profiles())
    session_id = router.new("default")
    session = router.current
    session.messages.append({"role": "user", "content": "original goal"})
    m = manager(tmp_path, checkpoint=session.child_checkpoint)
    sid = m.spawn("调查")["child_run_id"]
    m.wait([sid], 2, consume=False)
    save = db.save_child_runs
    def fail_after_write(*args):
        save(*args)
        raise RuntimeError("checkpoint failure after receipt write")
    with monkeypatch.context() as patch:
        patch.setattr(db, "save_child_runs", fail_after_write)
        with pytest.raises(RuntimeError, match="checkpoint failure"):
            m.deliver(session.messages)
    history = db.load_messages(session_id)
    records = db.load_child_runs(session_id)
    assert history == [{"role": "user", "content": "original goal"}]
    assert records[0]["delivery_receipt"] is None
    restored = manager(tmp_path)
    restored.restore_results(records, messages=history)
    assert restored.deliver(history)
    restored.finish()
    m.finish()
    db.close()
