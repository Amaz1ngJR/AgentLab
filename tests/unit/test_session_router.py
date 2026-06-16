"""离线测试：SessionRouter 命令解析与 session 生命周期。"""
from app.agent.profiles import AgentProfile
from app.agent.runtime import AgentSession
from app.agent.session_router import SessionRouter
from app.agent.approval import AutoApprove
from app.storage import Storage
from app.tools.registry import ToolRegistry
from app.models.protocol import ModelResponse


class _FakeRouter:
    model = "fake"
    provider = "fake"
    def create_message(self, messages, **kw):
        return ModelResponse(text="ok", tool_calls=[], usage={}, provider_payload=[])
    def format_tool_results(self, results):
        return []


def _make_router(storage, profiles):
    def factory(profile: AgentProfile, session_id: str) -> AgentSession:
        return AgentSession(
            llm=_FakeRouter(),
            tools=ToolRegistry(),
            approval=AutoApprove(),
            system_prompt="test",
        )
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


def test_new_creates_and_switches(tmp_path):
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
