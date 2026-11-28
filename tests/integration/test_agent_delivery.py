"""Real isolated edit -> verification -> explicit patch approval -> integration."""
import json
import threading

import pytest

from app.agent.agent_manager import AgentManager
from app.agent.approval import ApprovalResult, AutoApprove, DenyAll
from app.agent.cancel import CancelToken
from app.models.protocol import ModelResponse, ToolCall
from app.tools.builtin.files import default_tools
from app.tools.builtin.shell import SHELL
from app.tools.registry import ToolRegistry, ToolExecutionError
from tests.support.delegation import FakeRouter, response


class WriterRouter(FakeRouter):
    def fork(self):
        return FakeRouter([
            ModelResponse("", [ToolCall("w", "write_file", {"path": "child.txt", "content": "hello"})], {}, []),
            response("created"),
        ])


def make_manager(git_repo):
    tools = ToolRegistry()
    for tool in [*default_tools(), SHELL]:
        tools.register(tool)
    return AgentManager(llm=WriterRouter([]), tools=tools, approval=AutoApprove(),
                        workspace=git_repo, cancel=CancelToken(), allowed=True)


def spawn_isolated(manager, *args, **kwargs):
    return manager.spawn(*args, workspace_mode="worktree", **kwargs)


@pytest.mark.parametrize(("phase", "external"), [
    ("verification", False), ("integration", False), ("verification", True),
])
def test_integration_cancellation_reaches_root_and_closes_tool_item(git_repo, tmp_path, monkeypatch,
                                                                  phase, external):
    from app.agent.profiles import AgentProfile
    from app.agent.runtime import AgentSession
    from app.agent.service import RuntimeService
    from app.agent.session_router import SessionRouter
    from app.storage import Storage

    monkeypatch.setenv("WORKSPACE_ROOT", str(git_repo))
    child_manager = make_manager(git_repo)
    try:
        sid = spawn_isolated(child_manager, "create", role="executor", file_scope=["child.txt"])["child_run_id"]
        assert child_manager.wait([sid], 3)["agents"][0]["status"] == "succeeded"
        records = child_manager.records()
    finally:
        child_manager.finish()

    root_token = CancelToken()

    class CancelApproval:
        def request_tool(self, tool, action, args):
            if action == ("shell" if phase == "verification" else "integrate_agent"):
                if external:
                    root_token.cancel("keyboard_escape")
                return ApprovalResult(False, cancelled=True)
            return ApprovalResult(True)

    llm = FakeRouter([ModelResponse("", [ToolCall("delivery-call", "integrate_agent", {
        "sid": sid, "checks": ["test -f child.txt"],
    })], {}, [])])
    storage = Storage(tmp_path / "delivery.db")
    router = SessionRouter(
        storage, lambda profile, session_id: AgentSession(llm, child_manager.tools, approval=CancelApproval()),
        {"default": AgentProfile("default", "Default", "fake")},
    )
    thread_id = router.new("default")
    router.current.child_records = records
    service = RuntimeService(router)
    try:
        assert service.send_message("继续验证并交付", run_id="delivery", cancel=root_token) == "已中断。"
        phase_reason = f"{phase}_approval_cancelled"
        assert root_token.reason == ("keyboard_escape" if external else phase_reason)
        rows = storage.list_runtime_events(thread_id)
        started = next(row for row in rows if row["kind"] == "item.started"
                       and row["payload"]["item"]["kind"] == "tool.call")
        interrupted = next(row for row in rows if row["kind"] == "item.cancelled")
        assert started["payload"]["item"]["payload"]["tool_call_id"] == "delivery-call"
        assert interrupted["payload"]["item"]["payload"] == {
            "tool_name": "integrate_agent", "tool_call_id": "delivery-call", "reason": phase_reason,
        }
        assert rows[-1]["kind"] == "turn.cancelled" and rows[-1]["payload"]["reason"] == root_token.reason
        assert storage.get_runtime_turn("delivery")["status"] == "cancelled"
        assert not (git_repo / "child.txt").exists()
        assert len(llm.calls) == 1
    finally:
        service.close()
        storage.close()


def test_verification_denial_does_not_cancel_and_approval_feedback_is_accepted():
    from app.agent.agent_delivery import _approve_delivery

    token = CancelToken()
    with pytest.raises(ToolExecutionError, match="verification approval denied"):
        _approve_delivery(DenyAll(), SHELL, "shell", {"command": "true"}, token, phase="verification")
    assert not token.cancelled

    class ApprovedWithFeedback:
        def request_tool(self, tool, action, args):
            return ApprovalResult(True, feedback="__enable_auto_execute__")

    _approve_delivery(ApprovedWithFeedback(), SHELL, "shell", {"command": "true"}, token, phase="verification")
    assert not token.cancelled


def test_ordinary_followup_queries_and_integrates_previous_delivery(git_repo, monkeypatch):
    from app.agent.runtime import AgentSession
    monkeypatch.setenv("WORKSPACE_ROOT", str(git_repo))
    original = make_manager(git_repo)
    sid = spawn_isolated(original, "创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    original.wait([sid], 3)
    records = original.records()
    original.finish()
    # Expired previous-turn budgets must not prevent a new user turn from querying an artifact.
    records[-1]["budget"]["deadline_at"] = 0
    records[-1]["budget"]["model_rounds_left"] = 0
    router = FakeRouter([
        ModelResponse("", [ToolCall("query", "wait_agent", {"ids": [sid], "timeout": 0})], {}, []),
        response("仍待交付"),
        ModelResponse("", [ToolCall("deliver", "integrate_agent", {"sid": sid, "checks": ["test -f child.txt"]})], {}, []),
        response("验证并交付完成"),
    ])
    session = AgentSession(router, original.tools, approval=AutoApprove(), max_steps=12)
    session.child_records = records
    first = session.chat("你完成了？")
    assert "unknown child" not in first and "尚未交付" in first
    assert session.last_run_status == "blocked"
    names = {t["name"] for t in router.calls[0]["tools"]}
    assert "wait_agent" in names and "integrate_agent" in names and "spawn_agent" not in names
    assert session.chat("继续验证并交付") == "验证并交付完成"
    assert session.last_run_status == "completed"
    assert (git_repo / "child.txt").read_text() == "hello"


def test_failed_verification_tool_is_error_and_retry_can_deliver(git_repo):
    from app.agent.agent_manager import DelegationRegistry
    m = make_manager(git_repo)
    try:
        sid = spawn_isolated(m, "创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
        m.wait([sid], 3)
        tools = DelegationRegistry(m.tools, m)
        result, error = tools.execute("integrate_agent", {"sid": sid, "checks": ["exit 1"]})
        assert error and "未交付" in result and "verification_failed" in result
        assert sid in tools.delivery_errors
        assert not (git_repo / "child.txt").exists()
        result, error = tools.execute("integrate_agent", {"sid": sid, "checks": ["test -f child.txt"]})
        assert not error and not tools.delivery_errors
    finally:
        m.finish()


@pytest.mark.parametrize("policy,prompt", [
    ("off", "继续交付"), ("explicit", "不要使用子 agent，查看当前状态"),
])
def test_followup_recovery_respects_delegation_veto(git_repo, monkeypatch, policy, prompt):
    from app.agent.runtime import AgentSession
    monkeypatch.setenv("WORKSPACE_ROOT", str(git_repo))
    original = make_manager(git_repo)
    try:
        sid = spawn_isolated(original, "创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
        original.wait([sid], 3)
        records = original.records()
    finally:
        original.finish()
    router = FakeRouter([response("状态说明")])
    session = AgentSession(router, original.tools, delegation_policy=policy)
    session.child_records = records
    session.chat(prompt)
    assert session.last_run_status == "blocked"
    names = {t["name"] for t in router.calls[0]["tools"]}
    assert not names.intersection({"spawn_agent", "integrate_agent", "resume_agent"})
    assert not (git_repo / "child.txt").exists()


def test_writer_validates_and_integrates_once_without_parent_commit(git_repo):
    m = make_manager(git_repo)
    sid = spawn_isolated(m, "创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    assert m.wait([sid], 3)["agents"][0]["status"] == "succeeded"
    assert not (git_repo / "child.txt").exists()
    result = m.integrate(sid, ["test -f child.txt"])
    assert result["status"] == "integrated"
    assert (git_repo / "child.txt").read_text() == "hello"
    assert m.integrate(sid, ["test -f child.txt"])["already_integrated"]
    m.finish()


def test_integration_denial_or_failed_verification_never_applies_patch(git_repo):
    m = make_manager(git_repo)
    sid = spawn_isolated(m, "创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    assert m.integrate(sid, ["exit 1"])["status"] == "verification_failed"
    assert not (git_repo / "child.txt").exists()
    m.approval = DenyAll()
    with pytest.raises(ToolExecutionError, match="denied"):
        m.integrate(sid, ["test -f child.txt"])
    assert not (git_repo / "child.txt").exists()
    m.finish()


def test_writer_accepts_dirty_baseline_but_rejects_overlapping_scopes(git_repo):
    m = make_manager(git_repo)
    (git_repo / "dirty.txt").write_text("user changes")
    sid = spawn_isolated(m, "创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    with pytest.raises(ToolExecutionError, match="conflicts"):
        spawn_isolated(m, "again", role="executor", file_scope=["child.txt"])
    m.finish()


def test_validation_cannot_change_the_artifact_it_claimed_to_validate(git_repo):
    m = make_manager(git_repo)
    sid = spawn_isolated(m, "创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    with pytest.raises(ToolExecutionError, match="verification changed"):
        m.integrate(sid, ["printf changed > child.txt"])
    assert not (git_repo / "child.txt").exists()
    assert m.unresolved()
    m.finish()


def test_delivery_record_contains_artifact_and_revision(git_repo):
    m = make_manager(git_repo)
    sid = spawn_isolated(m, "创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    assert m.unresolved()
    assert m.snapshot(sid)["changed_files"] == ["child.txt"]
    m.integrate(sid, ["test -f child.txt"])
    record = m.records()[0]
    assert record["revision"] == 2
    assert record["verification"]["patch_sha256"]
    assert record["workspace"] != record["parent_workspace"]
    assert not m.unresolved()
    m.finish()


def test_git_metadata_is_not_a_permitted_write_scope(git_repo):
    m = make_manager(git_repo)
    with pytest.raises(ToolExecutionError, match="metadata"):
        spawn_isolated(m, "overwrite", role="executor", file_scope=[".git"])
    m.finish()


def test_resume_can_integrate_saved_artifact_without_reexecuting_writer(git_repo):
    m = make_manager(git_repo)
    sid = spawn_isolated(m, "创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    saved = m.records()
    m.finish()
    restored = make_manager(git_repo)
    restored.restore_results(saved)
    assert restored.usage == {"input_tokens": 0, "output_tokens": 0}
    assert restored.wait([sid], 0)["agents"][0]["status"] == "succeeded"
    assert restored.integrate(sid, ["test -f child.txt"])["status"] == "integrated"
    assert (git_repo / "child.txt").read_text() == "hello"
    with pytest.raises(ToolExecutionError, match="immutable"):
        restored.send_input(sid, "retry", "interrupt")
    restored.finish()


def test_interrupted_recovery_does_not_claim_worker_is_alive(git_repo):
    m = make_manager(git_repo)
    sid = spawn_isolated(m, "创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    saved = m.records()
    saved[0]["status"] = saved[0]["execution_status"] = "cancelling"
    m.finish()
    restored = make_manager(git_repo)
    restored.restore_results(saved)
    assert restored.snapshot(sid)["execution_status"] == "interrupted"
    with pytest.raises(ToolExecutionError, match="successful"):
        restored.integrate(sid, ["test -f child.txt"])
    assert not (git_repo / "child.txt").exists()
    restored.finish()


@pytest.mark.parametrize("restart", [False, True])
def test_two_disjoint_writers_can_integrate_sequentially_without_baseline_false_conflict(git_repo, restart):
    class Writers(WriterRouter):
        def __init__(self):
            super().__init__([])
            self.count = 0
        def fork(self):
            self.count += 1
            name = f"file{self.count}.txt"
            return FakeRouter([
                ModelResponse("", [ToolCall("w", "write_file", {"path": name, "content": name})], {}, []),
                response("created"),
            ])
    m = make_manager(git_repo)
    m.llm = Writers()
    a = spawn_isolated(m, "create a", role="executor", file_scope=["file1.txt"])["child_run_id"]
    b = spawn_isolated(m, "create b", role="executor", file_scope=["file2.txt"])["child_run_id"]
    for sid in [a, b]:
        m.wait([sid], 3)
    assert m.integrate(a, ["test -f file1.txt"])["status"] == "integrated"
    if restart:
        from app.storage import Storage
        db_path = git_repo.parent / (git_repo.name + "-delivery.db")
        db = Storage(db_path)
        db.create_session("s", "default", "fake")
        db.save_child_runs("s", m.records())
        m.finish()
        db.close()
        db = Storage(db_path)
        m = make_manager(git_repo)
        m.restore_results(db.load_child_runs("s"))
        db.close()
        assert m.snapshot(a)["delivery_status"] == "integrated"
    assert m.integrate(b, ["test -f file2.txt"])["status"] == "integrated"
    assert (git_repo / "file1.txt").exists() and (git_repo / "file2.txt").exists()
    m.finish()


def test_task_write_delivery_runs_parent_verification_before_releasing_dependents(git_repo):
    from app.agent.runtime import AgentSession
    from app.config.loader import use_workspace_root
    class Parent(WriterRouter):
        def create_message(self, messages, **kwargs):
            names = [tool["name"] for tool in kwargs.get("tools") or []]
            if not names:
                return response(json.dumps({"version": 1, "tasks": [
                    {"id": "write", "content": "create", "executor": "subagent", "subagent": {
                        "id": "writer", "instruction": "create", "role": "executor", "read_only": False,
                        "file_scope": ["child.txt"], "max_steps": 3, "max_tool_calls": 3}},
                    {"id": "finish", "content": "verify integrated", "dependencies": ["write"]},
                ]}))
            text = json.dumps(messages, ensure_ascii=False)
            if "patch_sha256" in text or "artifact_sha256" in text:
                return response("integrated and verified")
            import re
            sid = re.search(r'[a-f0-9]{32}-[a-f0-9]{12}', text)[0]
            return ModelResponse("", [ToolCall("integrate", "integrate_agent", {
                "sid": sid, "checks": ["test -f child.txt"]})], {}, [])
    m = make_manager(git_repo)
    session = AgentSession(Parent([]), m.tools, approval=AutoApprove(), orchestrate=True, max_steps=16)
    with use_workspace_root(git_repo):
        assert session.chat("多个 agent 实现并验证") == "integrated and verified", (session.last_run_status, session.task_store.snapshot())
    assert session.last_run_status == "completed"
    assert (git_repo / "child.txt").read_text() == "hello"
    assert all(t["status"] == "completed" for t in session.task_store.snapshot())
    m.finish()


def test_stopped_writer_allows_parent_verification_but_retains_file_scope(git_repo):
    from app.agent.agent_manager import DelegationRegistry
    from app.config.loader import use_workspace_root
    m = make_manager(git_repo)
    sid = spawn_isolated(m, "create", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    tools = DelegationRegistry(m.tools, m)
    assert tools.execute("write_file", {"path": "child.txt", "content": "oops"})[1]
    with use_workspace_root(git_repo):
        output, error = tools.execute("shell", {"command": "git status --short"},
                                      approved_action="shell")
    assert not error, output
    assert not (git_repo / "child.txt").exists()
    m.finish()


def test_unrelated_parent_changes_do_not_block_verified_integration(git_repo):
    import subprocess
    m = make_manager(git_repo)
    try:
        sid = spawn_isolated(m, "create", role="executor", file_scope=["child.txt"])["child_run_id"]
        m.wait([sid], 3)
        (git_repo / "parent.txt").write_text("parent work")
        subprocess.run(["git", "add", "parent.txt"], cwd=git_repo, check=True)
        (git_repo / "README.md").write_text("unstaged parent work")
        (git_repo / "notes.txt").write_text("untracked notes")
        assert m.integrate(sid, ["test -f child.txt"])["status"] == "integrated"
        assert (git_repo / "parent.txt").read_text() == "parent work"
        assert (git_repo / "README.md").read_text() == "unstaged parent work"
        assert (git_repo / "notes.txt").read_text() == "untracked notes"
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=git_repo,
                                capture_output=True, text=True, check=True).stdout
        assert staged.strip() == "parent.txt"
    finally:
        m.finish()


def test_parent_file_collision_is_never_overwritten(git_repo):
    m = make_manager(git_repo)
    try:
        sid = spawn_isolated(m, "create", role="executor", file_scope=["child.txt"])["child_run_id"]
        m.wait([sid], 3)
        (git_repo / "child.txt").write_text("user changes")
        with pytest.raises(ToolExecutionError, match="workspace drifted"):
            m.integrate(sid, ["test -f child.txt"])
        assert (git_repo / "child.txt").read_text() == "user changes"
        assert m.snapshot(sid)["delivery_status"] == "pending_review"
    finally:
        m.finish()


@pytest.mark.parametrize("state", ["unstaged", "staged", "index_only"])
def test_tracked_parent_changes_in_artifact_paths_require_review(git_repo, state):
    import subprocess
    class Editor(WriterRouter):
        def fork(self):
            return FakeRouter([
                ModelResponse("", [ToolCall("edit", "write_file", {
                    "path": "README.md", "content": "child version\n"})], {}, []),
                response("edited"),
            ])
    m = make_manager(git_repo)
    m.llm = Editor([])
    try:
        sid = spawn_isolated(m, "edit", role="executor", file_scope=["README.md"])["child_run_id"]
        assert m.wait([sid], 3)["agents"][0]["status"] == "succeeded"
        (git_repo / "README.md").write_text("user version\n")
        if state != "unstaged":
            subprocess.run(["git", "add", "README.md"], cwd=git_repo, check=True)
        if state == "index_only":
            (git_repo / "README.md").write_text("base\n")
        expected = (git_repo / "README.md").read_bytes()
        with pytest.raises(ToolExecutionError, match="drifted in child paths"):
            m.integrate(sid, ["test -f README.md"])
        assert (git_repo / "README.md").read_bytes() == expected
        if state != "unstaged":
            indexed = subprocess.run(["git", "show", ":README.md"], cwd=git_repo,
                                     capture_output=True, check=True).stdout
            assert indexed == b"user version\n"
    finally:
        m.finish()


def test_parent_change_during_approval_still_requires_revalidation(git_repo):
    from app.agent.approval import ApprovalResult
    m = make_manager(git_repo)
    class ChangingApproval(AutoApprove):
        def request_tool(self, tool, action, args):
            if action == "integrate_agent":
                (git_repo / "late.txt").write_text("changed during approval")
            return ApprovalResult(True)
    try:
        sid = spawn_isolated(m, "create", role="executor", file_scope=["child.txt"])["child_run_id"]
        m.wait([sid], 3)
        m.approval = ChangingApproval()
        with pytest.raises(ToolExecutionError, match="revalidation"):
            m.integrate(sid, ["test -f child.txt"])
        assert not (git_repo / "child.txt").exists()
    finally:
        m.finish()


def test_pending_delivery_does_not_block_approved_external_reads(git_repo):
    from app.agent.agent_manager import DelegationRegistry
    from app.config.loader import use_workspace_root
    external = git_repo.parent / (git_repo.name + "-other")
    external.mkdir()
    (external / "README.md").write_text("other project")
    m = make_manager(git_repo)
    try:
        sid = spawn_isolated(m, "create", role="executor", file_scope=["child.txt"])["child_run_id"]
        m.wait([sid], 3)
        tools = DelegationRegistry(m.tools, m)
        with use_workspace_root(git_repo):
            args = {"path": str(external / "README.md")}
            assert tools.execute("read_file", args)[1]
            assert tools.execute("read_file", args,
                                 approved_action="read_file_outside_workspace") == ("other project", False)
            output, error = tools.execute("shell", {"command": "git status --short", "cwd": str(external)},
                                          approved_action="shell_outside_workspace")
            # The other directory is not a Git repo: the tool executes and reports
            # Git's error, not an unrelated child-delivery lock.
            # 不能断言 git 的文案：git 会按系统 locale 本地化（中文环境下是
            # "致命错误：不是 git 仓库"），CI/本机 locale 不同就会假失败。
            # 这里只关心"确实执行了 git 并失败"，以及在 shell 未获批准时会出现的
            # 投递锁提示没有出现。
            assert error
            lowered = output.lower()
            assert "not a git repository" in lowered or "不是 git 仓库" in output
            assert "pending delivery" not in lowered and "待投递" not in output
    finally:
        m.finish()


def test_running_and_cancelling_writer_still_blocks_shell_but_not_external_reads(git_repo, monkeypatch):
    from app.agent.agent_manager import DelegationRegistry
    from app.agent.subagent_runtime import SubagentRuntimeFactory
    from app.agent.subagents import SubagentResult
    from app.config.loader import use_workspace_root
    entered, release = threading.Event(), threading.Event()

    def wait_for_release(self, spec, workspace, cancel):
        entered.set()
        assert release.wait(5)
        return SubagentResult(spec.subagent_id, spec.role, "cancelled")

    monkeypatch.setattr(SubagentRuntimeFactory, "delegate", wait_for_release)
    external = git_repo.parent / (git_repo.name + "-external.txt")
    external.write_text("outside evidence")
    m = make_manager(git_repo)
    try:
        sid = spawn_isolated(m, "create", role="executor", file_scope=["child.txt"])["child_run_id"]
        assert entered.wait(2)
        tools = DelegationRegistry(m.tools, m)
        with use_workspace_root(git_repo):
            for cancelling in (False, True):
                if cancelling:
                    m.close(sid)
                output, error = tools.execute("shell", {"command": "git status --short"},
                                              approved_action="shell")
                assert error and "still running or cancelling" in output
                output, error = tools.execute("write_file", {"path": "child.txt", "content": "unsafe"},
                                              approved_action="write_file")
                assert error and "reserved" in output
                with pytest.raises(ToolExecutionError, match="conflicts"):
                    spawn_isolated(m, "overlap", role="executor", file_scope=["child.txt"])
                assert tools.execute("read_file", {"path": str(external)},
                                     approved_action="read_file_outside_workspace") == ("outside evidence", False)
            release.set()
            m.wait([sid], 3)
            output, error = tools.execute("shell", {"command": "git status --short"},
                                          approved_action="shell")
            assert not error, output
            assert not tools.execute("write_file", {"path": "child.txt", "content": "after stop"},
                                     approved_action="write_file")[1]
    finally:
        release.set()
        m.finish()


@pytest.mark.parametrize("policy,goal,completed", [
    ("explicit", "继续", True),
    ("off", "继续", False),
    ("auto", "不要使用子 agent，继续", False),
])
def test_task_resume_saved_writer_uses_authorization_and_never_replays(git_repo, policy, goal, completed):
    from dataclasses import asdict
    from app.agent.runtime import AgentSession
    from app.agent.tasks import Task, BLOCKED
    from app.config.loader import use_workspace_root
    from app.storage import Storage
    m = make_manager(git_repo)
    sid = spawn_isolated(m, "创建文件", role="executor", file_scope=["child.txt"], max_steps=3)["child_run_id"]
    m.wait([sid], 3)
    db = Storage(git_repo.parent / (git_repo.name + "-resume.db"))
    db.create_session("s", "default", "fake")
    db.save_child_runs("s", m.records())
    task = Task("write", "创建文件", BLOCKED, execution={"executor": "subagent",
        "subagent": asdict(m.children[sid].spec), "child_run_id": sid})
    from app.agent.tasks import TaskStore
    tasks = TaskStore()
    tasks.add(task)
    db.save_tasks("s", tasks.snapshot())
    m.finish()
    class Parent(WriterRouter):
        def fork(self):
            raise AssertionError("recovery must not replay writer")
        def create_message(self, messages, **kwargs):
            if any("patch_sha256" in json.dumps(message) for message in messages):
                return response("已集成")
            return ModelResponse("", [ToolCall("merge", "integrate_agent", {
                "sid": sid, "checks": ["test -f child.txt"]})], {}, [])
    session = AgentSession(Parent([]), m.tools, approval=AutoApprove(), orchestrate=True,
                           max_steps=16, delegation_policy=policy)
    session.child_records = db.load_child_runs("s")
    session.task_store.restore(db.load_tasks("s"))
    with use_workspace_root(git_repo):
        session.chat(goal, resume=True)
    assert (session.last_run_status == "completed") is completed
    assert (git_repo / "child.txt").exists() is completed
    assert (session.task_store.get("write").status == "completed") is completed
    db.close()


def test_integrated_retry_is_free_even_after_parent_budget_exhaustion(git_repo):
    m = make_manager(git_repo)
    sid = spawn_isolated(m, "创建文件", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    m.integrate(sid, ["test -f child.txt"])
    revision = m.snapshot(sid)["revision"]
    m.tools_left = 0
    assert m.integrate(sid, ["exit 1"])["already_integrated"]
    assert m.snapshot(sid)["revision"] == revision
    m.finish()


def test_unstopped_writer_blocks_next_session_turn_until_real_exit(git_repo, monkeypatch):
    from app.agent.runtime import AgentSession
    from app.agent.subagent_runtime import SubagentRuntimeFactory
    from app.agent.subagents import SubagentResult
    from app.config.loader import use_workspace_root
    entered, release = threading.Event(), threading.Event()
    def stuck(self, spec, workspace, cancel):
        entered.set()
        release.wait(4)
        return SubagentResult(spec.subagent_id, spec.role, "cancelled")
    monkeypatch.setattr(SubagentRuntimeFactory, "delegate", stuck)
    from app.agent.cancel import CancelToken
    cancel = CancelToken()
    class Parent(WriterRouter):
        def create_message(self, messages, **kwargs):
            if entered.is_set():
                cancel.cancel()
                return response("stopping")
            return ModelResponse("", [ToolCall("spawn", "spawn_agent", {
                "instruction": "create", "role": "executor", "file_scope": ["child.txt"],
                "max_steps": 1})], {}, [])
    m = make_manager(git_repo)
    session = AgentSession(Parent([]), m.tools, approval=AutoApprove(), orchestrate=False, max_steps=4)
    try:
        with use_workspace_root(git_repo):
            answer = session.chat("多个 agent 创建文件", cancel=cancel)
            assert entered.is_set(), (answer, session.messages)
            assert session._retiring_managers
            assert "仍在停止中" in session.chat("下一步修改文件")
            assert session.last_run_status == "blocked"
            record = session.child_records[-1]
            assert record["execution_status"] == "cancelling"
    finally:
        release.set()
        for manager in session._retiring_managers:
            for child in manager.children.values():
                child.done.wait(2)
            manager.pump()
        m.finish()
    assert session.child_records[-1]["execution_status"] == "cancelled"


def test_child_deadline_includes_queued_approval_wait(git_repo):
    m = make_manager(git_repo)
    sid = spawn_isolated(m, "create", role="executor", file_scope=["child.txt"], timeout_seconds=0.1)["child_run_id"]
    # Deliberately don't pump the owner approval queue.
    assert m.children[sid].done.wait(2)
    assert m.snapshot(sid)["execution_status"] == "timed_out"
    m.pump()
    assert not (m.children[sid].worktree.path / "child.txt").exists()
    assert not (git_repo / "child.txt").exists()
    m.finish()


@pytest.mark.parametrize("expire_at", ["verification", "approval"])
def test_integration_deadline_prevents_late_patch_application(git_repo, expire_at):
    import time
    from app.agent.cancel import Cancelled
    from app.agent.approval import ApprovalResult
    from app.tools.registry import Tool, current_cancel_token
    m = make_manager(git_repo)
    sid = spawn_isolated(m, "create", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    if expire_at == "verification":
        def verify(args):
            token = current_cancel_token()
            assert token.wait(2)
            token.raise_if_cancelled()
        m.tools.register(Tool("shell", "verify", {}, verify))
    else:
        class SlowApproval(AutoApprove):
            def request_tool(self, tool, action, args):
                if action == "integrate_agent":
                    time.sleep(0.2)
                return ApprovalResult(True)
        m.approval = SlowApproval()
    m.deadline = time.monotonic() + 0.1
    try:
        with pytest.raises(Cancelled):
            m.integrate(sid, ["test -f child.txt"])
        assert not (git_repo / "child.txt").exists()
        assert m.snapshot(sid)["delivery_status"] == "pending_review"
        assert m.children[sid].worktree.path.exists()
    finally:
        m.finish()


def test_accepted_late_writer_steering_continues_same_worktree_and_budget(git_repo, monkeypatch):
    from app.agent.executor import Executor
    gate, release = threading.Event(), threading.Event()
    original = Executor.run_task
    first = True
    def finish_race(self, *args, **kwargs):
        nonlocal first
        outcome = original(self, *args, **kwargs)
        if first:
            first = False
            gate.set()
            assert release.wait(2)
        return outcome
    monkeypatch.setattr(Executor, "run_task", finish_race)
    class Writer(WriterRouter):
        def fork(self):
            return FakeRouter([
                ModelResponse("", [ToolCall("w1", "write_file", {"path": "child.txt", "content": "first"})], {}, []),
                response("first done"),
                ModelResponse("", [ToolCall("w2", "write_file", {"path": "child.txt", "content": "steered"})], {}, []),
                response("late change done"),
            ])
    m = make_manager(git_repo)
    m.llm = Writer([])
    try:
        sid = spawn_isolated(m, "create", role="executor", file_scope=["child.txt"], max_steps=4, max_tool_calls=2)["child_run_id"]
        # Service the first write approval, then stop at Executor's completion boundary.
        import time
        deadline = time.monotonic() + 2
        while not gate.is_set() and time.monotonic() < deadline:
            m.pump()
            time.sleep(0.01)
        assert gate.is_set()
        assert m.send_input(sid, "write child.txt as steered")["accepted"]
        release.set()
        result = m.wait([sid], 3)["agents"][0]
        assert result["status"] == "succeeded"
        assert m.children[sid].result.model_rounds == 4
        assert m.children[sid].result.tool_calls == 2
        assert (m.children[sid].worktree.path / "child.txt").read_text() == "steered"
        assert not (git_repo / "child.txt").exists()
        m.integrate(sid, ["test -f child.txt"])
        assert (git_repo / "child.txt").read_text() == "steered"
    finally:
        release.set()
        m.finish()


def test_process_death_during_writer_retains_parent_checkpoint(git_repo):
    import subprocess
    import sys
    from app.storage import Storage
    db_path = git_repo.parent / (git_repo.name + "-crash.db")
    script = r'''
import json, os, sys
from app.agent.approval import AutoApprove
from app.agent.profiles import AgentProfile
from app.agent.runtime import AgentSession
from app.agent.session_router import SessionRouter
from app.config.loader import use_workspace_root
from app.models.protocol import ModelResponse
from app.storage import Storage
from app.tools.builtin.files import default_tools
from app.tools.registry import ToolRegistry
from tests.support.delegation import FakeRouter, response
class CrashChild(FakeRouter):
    def create_message(self, messages, **kwargs):
        os._exit(0)  # No finally/persist_current; kill parent and child threads together.
class Parent(FakeRouter):
    def fork(self):
        return CrashChild([])
plan = {"version": 1, "tasks": [
    {"id": "prep", "content": "总结已给材料"},
    {"id": "write", "content": "create", "dependencies": ["prep"], "executor": "subagent",
     "subagent": {"id": "writer", "instruction": "create", "role": "executor", "read_only": False,
                   "file_scope": ["child.txt"], "max_steps": 2, "max_tool_calls": 2}}]}
tools = ToolRegistry()
for tool in default_tools():
    tools.register(tool)
prepared = ModelResponse("PREPARED", [], {}, [{"role": "assistant", "content": "PREPARED"}])
def factory(profile, sid):
    return AgentSession(Parent([response(json.dumps(plan)), prepared]), tools,
                        approval=AutoApprove(), orchestrate=True, max_steps=16)
db = Storage(__import__("pathlib").Path(sys.argv[1]))
profile = AgentProfile("default", "default", "fake")
router = SessionRouter(storage=db, session_factory=factory, profiles={"default": profile}, default_profile_id="default")
with use_workspace_root(__import__("pathlib").Path(sys.argv[2])):
    router.new("default")
    router.current.chat("多个 agent 创建文件")
raise SystemExit(3)  # The child must have terminated the process before return.
'''
    completed = subprocess.run([sys.executable, "-c", script, str(db_path), str(git_repo)],
                               capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    db = Storage(db_path)
    try:
        sid = db.list_sessions()[0]["id"]
        records = db.load_child_runs(sid)
        tasks = {task["id"]: task for task in db.load_tasks(sid)}
        assert len(records) == 1 and records[0]["execution_status"] == "interrupted"
        assert tasks["write"]["execution"]["child_run_id"] == records[0]["child_run_id"]
        assert tasks["write"]["dependencies"] == ["prep"]
        assert tasks["prep"]["status"] == "completed"
        assert "PREPARED" in json.dumps(db.load_messages(sid))
        assert not (git_repo / "child.txt").exists()
    finally:
        db.close()


@pytest.mark.parametrize("status", ["succeeded", "failed", "budget_exhausted", "cancelled"])
def test_closed_stopped_writer_releases_scope_without_discarding_artifact(git_repo, monkeypatch, status):
    from app.agent.agent_manager import DelegationRegistry
    from app.agent.subagent_runtime import SubagentRuntimeFactory
    from app.agent.subagents import SubagentResult
    from app.config.loader import use_workspace_root

    def create_artifact(self, spec, workspace, cancel):
        (workspace / "child.txt").write_text("partial artifact")
        return SubagentResult(spec.subagent_id, spec.role, status)

    monkeypatch.setattr(SubagentRuntimeFactory, "delegate", create_artifact)
    m = make_manager(git_repo)
    try:
        sid = spawn_isolated(m, "create", role="executor", file_scope=["child.txt"])["child_run_id"]
        m.wait([sid], 3)
        tools = DelegationRegistry(m.tools, m)
        with use_workspace_root(git_repo):
            args = {"path": "child.txt", "content": "parent replacement"}
            assert "reserved" in tools.execute("write_file", args, approved_action="write_file")[0]
            with pytest.raises(ToolExecutionError, match="conflicts"):
                spawn_isolated(m, "replacement", role="executor", file_scope=["child.txt"])
            m.close(sid)
            m.close(sid)  # Release is idempotent; artifacts and status remain intact.
            assert m.snapshot(sid)["execution_status"] == status
            assert m.snapshot(sid)["delivery_status"] == "pending_review"
            assert (m.children[sid].worktree.path / "child.txt").read_text() == "partial artifact"
            if status != "succeeded":
                with pytest.raises(ToolExecutionError, match="only successful"):
                    m.integrate(sid, ["test -f child.txt"])
            replacement = spawn_isolated(m, "replacement", role="executor", file_scope=["child.txt"])["child_run_id"]
            m.wait([replacement], 3)
            m.close(replacement)
            assert not tools.execute("write_file", args, approved_action="write_file")[1]
            assert not tools.execute("edit_file", {"path": "child.txt", "old_str": "parent", "new_str": "reviewed"},
                                     approved_action="edit_file")[1]
            assert (git_repo / "child.txt").read_text() == "reviewed replacement"
    finally:
        m.finish()


def test_integrated_writer_releases_scope_for_next_writer(git_repo):
    from app.agent.agent_manager import DelegationRegistry
    from app.config.loader import use_workspace_root
    import subprocess

    m = make_manager(git_repo)
    try:
        sid = spawn_isolated(m, "create", role="executor", file_scope=["child.txt"])["child_run_id"]
        m.wait([sid], 3)
        m.integrate(sid, ["test -f child.txt"])
        # A new writer still requires a clean baseline, independently of scope ownership.
        subprocess.run(["git", "add", "child.txt"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "accept child"], cwd=git_repo, check=True, capture_output=True)
        next_id = spawn_isolated(m, "next", role="executor", file_scope=["child.txt"])["child_run_id"]
        m.wait([next_id], 3)
        m.close(next_id)
        tools = DelegationRegistry(m.tools, m)
        with use_workspace_root(git_repo):
            assert not tools.execute("edit_file", {"path": "child.txt", "old_str": "hello", "new_str": "updated"},
                                     approved_action="edit_file")[1]
    finally:
        m.finish()


def test_restored_failed_artifact_does_not_reacquire_write_scope(git_repo, monkeypatch):
    from app.agent.agent_manager import DelegationRegistry
    from app.agent.subagent_runtime import SubagentRuntimeFactory
    from app.agent.subagents import SubagentResult
    from app.config.loader import use_workspace_root

    def fail(self, spec, workspace, cancel):
        (workspace / "child.txt").write_text("unfinished")
        return SubagentResult(spec.subagent_id, spec.role, "failed")

    monkeypatch.setattr(SubagentRuntimeFactory, "delegate", fail)
    original = make_manager(git_repo)
    restored = make_manager(git_repo)
    try:
        sid = spawn_isolated(original, "create", role="executor", file_scope=["child.txt"])["child_run_id"]
        original.wait([sid], 3)
        original.close(sid)
        restored.restore_results(original.records())
        assert restored.snapshot(sid)["closed"]
        with use_workspace_root(git_repo):
            assert not DelegationRegistry(restored.tools, restored).execute(
                "write_file", {"path": "child.txt", "content": "manual recovery"},
                approved_action="write_file")[1]
        assert (restored.children[sid].worktree.path / "child.txt").read_text() == "unfinished"
    finally:
        original.finish()
        restored.finish()



def test_default_shared_writer_works_with_dirty_non_git_workspace(tmp_path):
    from app.config.loader import use_workspace_root
    (tmp_path / "user.txt").write_text("uncommitted user work")
    m = make_manager(tmp_path)
    try:
        sid = m.spawn("create", role="executor", file_scope=["child.txt"])["child_run_id"]
        result = m.wait([sid], 3)["agents"][0]
        assert result["status"] == "succeeded"
        assert m.snapshot(sid)["workspace_mode"] == "shared"
        assert m.children[sid].worktree is None
        assert (tmp_path / "child.txt").read_text() == "hello"
        assert result["changed_files"] == ["child.txt"]
        assert result["delivery_status"] == "applied_unverified"
        assert (tmp_path / "user.txt").read_text() == "uncommitted user work"
        assert m.integrate(sid, ["test -f child.txt"])["status"] == "integrated"
        assert m.integrate(sid, ["exit 1"])["already_integrated"]
    finally:
        m.finish()


def test_shared_artifact_drift_and_verification_mutation_are_detected(tmp_path):
    m = make_manager(tmp_path)
    try:
        sid = m.spawn("create", role="executor", file_scope=["child.txt"])["child_run_id"]
        m.wait([sid], 3)
        (tmp_path / "child.txt").write_text("user correction")
        with pytest.raises(ToolExecutionError, match="artifact changed"):
            m.integrate(sid, ["test -f child.txt"])
        assert (tmp_path / "child.txt").read_text() == "user correction"
        (tmp_path / "child.txt").write_text("hello")
        with pytest.raises(ToolExecutionError, match="verification changed"):
            m.integrate(sid, ["printf changed > child.txt"])
        assert m.snapshot(sid)["delivery_status"] == "applied_unverified"
    finally:
        m.finish()


def test_shared_results_survive_recovery_without_reapplying_files(tmp_path):
    m = make_manager(tmp_path)
    sid = m.spawn("create", role="executor", file_scope=["child.txt"])["child_run_id"]
    m.wait([sid], 3)
    saved = m.records()
    m.finish()
    restored = make_manager(tmp_path)
    try:
        restored.restore_results(saved)
        assert restored.integrate(sid, ["test -f child.txt"])["status"] == "integrated"
        assert (tmp_path / "child.txt").read_text() == "hello"
    finally:
        restored.finish()


@pytest.mark.parametrize("delivery_status", ["applied_unverified", "no_changes"])
def test_parent_can_verify_current_shared_scope_without_rewriting_child_failure(tmp_path, delivery_status):
    from app.agent.agent_manager import DelegationRegistry

    original = make_manager(tmp_path)
    sid = original.spawn("create", role="executor", file_scope=["child.txt"])["child_run_id"]
    original.wait([sid], 3)
    records = original.records()
    original.finish()
    records[0].update(status="failed", execution_status="failed", delivery_status=delivery_status,
                      error="old patch was ambiguous")
    (tmp_path / "child.txt").write_text("parent correction")

    class Review:
        approved = False

        def request_tool(self, tool, action, args):
            if action == "integrate_agent":
                assert args["adopt_current"] and args["file_scope"] == ["child.txt"]
                assert args["changed_since_child"] == ["child.txt"]
                return ApprovalResult(self.approved)
            return ApprovalResult(True)

    restored = make_manager(tmp_path)
    restored.approval = review = Review()
    try:
        restored.restore_results(records)
        tools = DelegationRegistry(restored.tools, restored, continuation_only=True)
        with pytest.raises(ToolExecutionError, match="only successful"):
            restored.integrate(sid, ["test -f child.txt"])
        arguments = {"sid": sid, "checks": ["grep -qx 'parent correction' child.txt"], "adopt_current": True}
        assert tools.execute("integrate_agent", arguments)[1]
        assert restored.unresolved() == [f"{sid}: failed/{delivery_status}"]
        review.approved = True
        output, error = tools.execute("integrate_agent", arguments)
        assert not error and json.loads(output)["status"] == "parent_verified"
        assert not restored.unresolved() and not tools.delivery_errors
        record = restored.records()[0]
        assert record["execution_status"] == "failed" and record["error"] == "old patch was ambiguous"
        assert record["verification"]["verification"][0]["error"] is False
        assert (tmp_path / "child.txt").read_text() == "parent correction"
    finally:
        restored.finish()

    reopened = make_manager(tmp_path)
    try:
        reopened.restore_results([record])
        assert not reopened.unresolved()
        assert reopened.integrate(sid, ["exit 1"], adopt_current=True)["already_integrated"]
    finally:
        reopened.finish()


def _pending_shared_adoptions(tmp_path):
    from dataclasses import asdict
    from app.agent.subagents import SubagentSpec

    records, checks = [], []
    for sid, status in [("manifest", "budget_exhausted"), ("origin", "failed")]:
        path = f"{sid}.txt"
        records.append({"child_run_id": sid, "parent_run_id": "old", "parent_workspace": str(tmp_path),
                        "delegation_allowed": True, "execution_status": status, "delivery_status": "no_changes",
                        "spec": asdict(SubagentSpec(sid, sid, workspace_mode="shared", file_scope=(path,))),
                        "metrics": {"shared_artifact": {}}, "changed_files": []})
        (tmp_path / path).write_text("parent implementation")
        checks.append(ToolCall(sid, "integrate_agent", {"sid": sid, "adopt_current": True,
                                                        "checks": [f"grep -qx 'parent implementation' {path}"]}))
    return records, checks


def _reply_with_payload(text):
    return ModelResponse(text, [], {}, [{"role": "assistant", "content": text}])


@pytest.mark.parametrize("adopt_both", [True, False])
def test_final_answer_waits_for_both_parent_adoptions(tmp_path, monkeypatch, adopt_both):
    from app.agent.runtime import AgentSession
    from tests.support.delegation import SnapshotRouter

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    records, checks = _pending_shared_adoptions(tmp_path)
    llm = SnapshotRouter([_reply_with_payload("未经核实的完成声明"),
                          ModelResponse("", checks if adopt_both else checks[:1], {}, []),
                          _reply_with_payload("全部验收完成")])
    notifications = []
    session = AgentSession(llm, make_manager(tmp_path).tools, approval=AutoApprove(), on_event=notifications.append)
    session.child_records = records
    answer = session.chat("请接管并验证旧子任务的当前改动")
    assert len(llm.calls) == 3
    prompt = llm.calls[1]["messages"][-1]["content"]
    assert all(value in prompt for value in ("manifest.txt", "origin.txt", "integrate_agent", "adopt_current"))
    assert not any(e.text == "未经核实的完成声明" for e in notifications)
    assert all(m.get("content") != "未经核实的完成声明"
               for history in [*(call["messages"] for call in llm.calls), session.messages] for m in history)
    by_id = {r["child_run_id"]: (r["execution_status"], r["delivery_status"]) for r in session.child_records}
    assert by_id["manifest"] == ("budget_exhausted", "parent_verified")
    if adopt_both:
        assert by_id["origin"] == ("failed", "parent_verified")
        assert session.last_run_status == "completed" and answer == "全部验收完成"
    else:
        assert by_id["origin"] == ("failed", "no_changes")
        assert session.last_run_status == "blocked" and "origin: failed/no_changes" in answer
        assert "manifest" not in answer and "全部验收完成" not in answer
        assert not any(e.text == "全部验收完成" for e in notifications)
        assert not any(m.get("content") == "全部验收完成" for m in session.messages)


def test_planned_delivery_tasks_settle_only_their_own_child(tmp_path, monkeypatch):
    from app.agent.runtime import AgentSession
    from app.agent.tasks import BLOCKED, COMPLETED, Task
    from tests.support.delegation import SnapshotRouter

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    records, checks = _pending_shared_adoptions(tmp_path)
    premature = "尚未验收时声明完成"
    llm = SnapshotRouter([
        _reply_with_payload(premature),
        ModelResponse("", checks[:1], {}, []),
        _reply_with_payload("manifest 已验收"),
        ModelResponse("", checks[1:], {}, []),
        _reply_with_payload("origin 已验收"),
        _reply_with_payload("两项均已验收"),
    ])
    notifications = []
    session = AgentSession(llm, make_manager(tmp_path).tools, approval=AutoApprove(),
                           on_event=notifications.append, orchestrate=True)
    session.child_records = records
    for record in records:
        sid = record["child_run_id"]
        session.task_store.add(Task(id=sid, content=sid, status=BLOCKED, execution={
            "executor": "subagent", "child_run_id": sid, "subagent": record["spec"],
        }))

    assert session.chat("继续验收子任务", resume=True) == "两项均已验收"
    assert session.last_run_status == "completed"
    assert {task.id: task.status for task in session.task_store.all()} == dict.fromkeys(
        ["manifest", "origin", "manifest-delivery", "origin-delivery", "run1-summary"], COMPLETED)
    prompt = llm.calls[1]["messages"][-1]["content"]
    assert "manifest.txt" in prompt and "origin.txt" not in prompt
    assert not any(e.text == premature for e in notifications)
    assert all(m.get("content") != premature
               for history in [*(call["messages"] for call in llm.calls), session.messages] for m in history)


def test_resume_requeues_blocked_delivery_without_rerunning_writer(tmp_path, monkeypatch):
    from app.agent.runtime import AgentSession
    from app.agent.tasks import BLOCKED, COMPLETED, Task
    from tests.support.delegation import SnapshotRouter

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    records, checks = _pending_shared_adoptions(tmp_path)
    llm = SnapshotRouter([
        _reply_with_payload("暂不验收"),
        _reply_with_payload("仍未验收"),
        ModelResponse("", checks[:1], {}, []),
        _reply_with_payload("manifest 已验收"),
        _reply_with_payload("全部交付完成"),
    ])
    session = AgentSession(llm, make_manager(tmp_path).tools, approval=AutoApprove(), orchestrate=True)
    session.child_records = records[:1]
    session.task_store.add(Task(id="manifest", content="原写入任务", status=BLOCKED, execution={
        "executor": "subagent", "child_run_id": "manifest", "subagent": records[0]["spec"],
    }))

    assert "manifest: budget_exhausted/no_changes" in session.chat("继续验收子任务", resume=True)
    delivery = session.task_store.get("manifest-delivery")
    assert delivery.status == BLOCKED and len(llm.calls) == 2

    assert session.chat("继续验收子任务", resume=True) == "全部交付完成"
    assert session.last_run_status == "completed"
    assert session.task_store.get("manifest-delivery") is delivery and delivery.status == COMPLETED
    assert session.task_store.get("manifest").status == COMPLETED
    assert len(session.child_records) == 1
    assert session.child_records[0]["execution_status"] == "budget_exhausted"
    assert session.child_records[0]["delivery_status"] == "parent_verified"


@pytest.mark.parametrize("stale_completed", [False, True])
def test_resume_requires_integration_of_successful_writer(tmp_path, monkeypatch, stale_completed):
    from app.agent.runtime import AgentSession
    from app.agent.tasks import BLOCKED, COMPLETED, Task
    from tests.support.delegation import SnapshotRouter

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    original = make_manager(tmp_path)
    try:
        sid = original.spawn("create", role="executor", file_scope=["child.txt"])["child_run_id"]
        result = original.wait([sid], 3)["agents"][0]
        assert (result["status"], result["delivery_status"]) == ("succeeded", "applied_unverified")
        records = original.records()
    finally:
        original.finish()

    premature = "只读检查完成，声明已交付"
    responses = [] if stale_completed else [
        ModelResponse("", [ToolCall("read", "read_file", {"path": "child.txt"})], {}, []),
        _reply_with_payload(premature),
    ]
    llm = SnapshotRouter(responses + [
        ModelResponse("", [ToolCall("integrate", "integrate_agent", {
            "sid": sid, "checks": ["test -f child.txt"],
        })], {}, []),
        _reply_with_payload("已验收"),
        _reply_with_payload("全部交付完成"),
    ])
    session = AgentSession(llm, original.tools, approval=AutoApprove(), orchestrate=True)
    session.child_records = records
    session.task_store.add(Task(id="writer", content="原写入任务", status=BLOCKED, execution={
        "executor": "subagent", "child_run_id": sid, "subagent": records[0]["spec"],
    }))
    if stale_completed:
        session.task_store.add(Task(id="writer-delivery", content="旧验收任务", status=COMPLETED,
                                    execution={"executor": "main_agent", "delivery_for": "writer"}))
    else:
        assert f"{sid}: succeeded/applied_unverified" in session.chat("继续验收子任务", resume=True)
        assert session.task_store.get("writer").status == BLOCKED
        assert session.task_store.get("writer-delivery").status == BLOCKED
        assert all(message.get("content") != premature for message in session.messages)

    assert session.chat("继续验收子任务", resume=True) == "全部交付完成"
    assert session.last_run_status == "completed"
    assert session.task_store.get("writer").status == COMPLETED
    assert session.task_store.get("writer-delivery").status == COMPLETED
    assert [(r["child_run_id"], r["delivery_status"]) for r in session.child_records] == [(sid, "integrated")]


def test_parent_verified_delivery_completes_linked_task_and_survives_session_restart(tmp_path, monkeypatch):
    from app.agent.profiles import AgentProfile
    from app.agent.runtime import AgentSession
    from app.agent.session_router import SessionRouter
    from app.storage import Storage

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    original = make_manager(tmp_path)
    sid = original.spawn("create", role="executor", file_scope=["child.txt"])["child_run_id"]
    original.wait([sid], 3)
    records = original.records()
    original.finish()
    records[0].update(status="failed", execution_status="failed", error="old patch was ambiguous")
    llm = FakeRouter([
        ModelResponse("", [ToolCall("adopt", "integrate_agent", {
            "sid": sid, "adopt_current": True, "checks": ["test -f child.txt"],
            "check_timeout_seconds": 300,
        })], {}, []),
        response("已验证当前改动"),
        response("下一轮答复"),
    ])
    storage = Storage(tmp_path / "state.db")

    def factory(profile, session_id):
        return AgentSession(llm, original.tools, approval=AutoApprove())

    profiles = {"default": AgentProfile("default", "Default", "fake")}
    router = SessionRouter(storage, factory, profiles)
    thread_id = router.new("default")
    session = router.current
    session.child_records = records
    session.task_store.restore([{"id": "task", "content": "交付写入", "status": "blocked",
                                "execution": {"child_run_id": sid}}])
    try:
        assert session.chat("请接管并验证旧子任务的当前改动") == "已验证当前改动"
        assert session.last_run_status == "completed"
        assert session.task_store.get("task").status == "completed"
        assert session.task_store.get("task").execution["result"]["execution_status"] == "failed"
        router.close_all()
        reopened = SessionRouter(storage, factory, profiles)
        assert reopened.switch(thread_id)
        assert reopened.current.child_records[0]["delivery_status"] == "parent_verified"
        assert reopened.current.chat("继续开发下一步") == "下一轮答复"
        assert reopened.current.last_run_status == "completed"
        reopened.close_all()
    finally:
        storage.close()


def test_isolated_dirty_snapshot_keeps_parent_index_and_branches_unchanged(git_repo):
    import subprocess
    def git(*args):
        return subprocess.run(["git", *args], cwd=git_repo, check=True, capture_output=True).stdout
    (git_repo / "README.md").write_text("staged parent version")
    git("add", "README.md")
    (git_repo / "README.md").write_text("unstaged parent version")
    (git_repo / "new-parent.txt").write_text("new parent file")
    before = (git("rev-parse", "HEAD"), git("write-tree"), git("branch", "--format=%(refname)"))
    m = make_manager(git_repo)
    try:
        sid = spawn_isolated(m, "create", role="executor", file_scope=["child.txt"])["child_run_id"]
        assert m.wait([sid], 3)["agents"][0]["status"] == "succeeded"
        wt = m.children[sid].worktree
        assert wt.detached
        assert (wt.path / "README.md").read_text() == "unstaged parent version"
        assert (wt.path / "new-parent.txt").read_text() == "new parent file"
        assert (git("rev-parse", "HEAD"), git("write-tree"), git("branch", "--format=%(refname)")) == before
        assert not (git_repo / "child.txt").exists()
        assert m.snapshot(sid)["changed_files"] == ["child.txt"]
        assert m.integrate(sid, ["test -f child.txt"])["status"] == "integrated"
        assert git("show", ":README.md") == b"staged parent version"
        assert (git_repo / "README.md").read_text() == "unstaged parent version"
        assert (git_repo / "new-parent.txt").read_text() == "new parent file"
    finally:
        m.finish()



def test_detached_cleanup_does_not_delete_an_unowned_named_branch(git_repo):
    from app.workspace.worktree import WorktreeManager
    manager = WorktreeManager(git_repo)
    wt = manager.create("detached-cleanup", detached=True, include_uncommitted=True)
    manager._run_git("branch", "worktree/detached-cleanup")
    manager.remove(wt)
    assert manager._run_git("rev-parse", "--verify", "refs/heads/worktree/detached-cleanup").returncode == 0


def test_isolated_child_can_edit_parent_dirty_file_without_touching_parent_index(git_repo):
    import subprocess
    (git_repo / "README.md").write_text("parent staged")
    subprocess.run(["git", "add", "README.md"], cwd=git_repo, check=True)
    (git_repo / "README.md").write_text("parent unstaged")
    class Editor(WriterRouter):
        def fork(self):
            return FakeRouter([
                ModelResponse("", [ToolCall("edit", "write_file", {
                    "path": "README.md", "content": "parent unstaged plus child"})], {}, []),
                response("edited"),
            ])
    m = make_manager(git_repo)
    m.llm = Editor([])
    try:
        sid = spawn_isolated(m, "edit", role="executor", file_scope=["README.md"])["child_run_id"]
        assert m.wait([sid], 3)["agents"][0]["status"] == "succeeded"
        assert m.integrate(sid, ["test -f README.md"])["status"] == "integrated"
        assert (git_repo / "README.md").read_text() == "parent unstaged plus child"
        indexed = subprocess.run(["git", "show", ":README.md"], cwd=git_repo,
                                 capture_output=True, check=True).stdout
        assert indexed == b"parent staged"
    finally:
        m.finish()
