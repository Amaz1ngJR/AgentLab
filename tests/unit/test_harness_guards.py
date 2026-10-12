"""弱模型不遵循提示词时，harness 的离线回归测试。"""
import pytest

from app.agent.approval import DenyAll
from app.agent.executor import Executor
from app.agent.planner import Planner
from app.agent.runtime import AgentSession
from app.agent.tasks import COMPLETED, FAILED, Task
from app.tools.builtin.files import EDIT_FILE, LIST_DIR, READ_FILE
from app.tools.registry import Tool, ToolRegistry
from tests.unit.test_orchestrator import (
    FakeRouter, _assert_tools_paired, _echo_tool, _multi_tool_resp,
    _registry, _resp_text, _resp_tool,
)


@pytest.mark.parametrize("claim", ["已完成修改", "无需修改", "already done", "successfully completed"])
def test_completion_words_are_not_execution_evidence(claim):
    router = FakeRouter([_resp_text(claim), _resp_text(claim)])
    out = Executor(router, _registry()).run_task(
        Task("t1", "修改配置文件"), [], system="", max_steps=8,
    )
    assert out.status == FAILED
    assert "执行证据" in out.error
    assert len(router.calls) == 2  # 只允许一次纠正，而不是耗尽全部预算


@pytest.mark.parametrize("name", ["todo_write", "load_skill"])
@pytest.mark.parametrize("mode", ["direct", "task"])
def test_meta_tools_do_not_count_as_execution(name, mode):
    reg = _registry(Tool(name, "meta", {"type": "object"}, lambda _: "ok"))
    router = FakeRouter([
        _resp_tool("c1", name, {}), _resp_text("已修改"), _resp_text("已修改"),
    ])
    if mode == "task":
        out = Executor(router, reg).run_task(
            Task("t1", "修改代码"), [], system="", max_steps=5,
        )
        assert out.status == FAILED
    else:
        session = AgentSession(router, reg, max_steps=5)
        with pytest.raises(RuntimeError, match="未进行结构化工具调用"):
            session.chat("请修改代码")
        assert session.last_run_status == "failed"


def test_verified_no_change_can_complete():
    router = FakeRouter([
        _resp_tool("c1", "echo", {"msg": "checked"}), _resp_text("已核实，无需修改"),
    ])
    out = Executor(router, _registry(_echo_tool())).run_task(
        Task("t1", "检查配置文件"), [], system="", max_steps=3,
    )
    assert out.status == COMPLETED


def test_duplicate_call_early_return_keeps_history_paired():
    router = FakeRouter([
        _resp_tool("c1", "echo", {"msg": "x"}),
        _multi_tool_resp(("c2", "echo", {"msg": "y"}),
                         ("c3", "echo", {"msg": "x"}),
                         ("c4", "echo", {"msg": "z"})),
    ])
    messages = []
    out = Executor(router, _registry(_echo_tool())).run_task(
        Task("t1", "读取数据"), messages, system="", max_steps=5,
    )
    assert out.status == FAILED
    assert "重复请求" in out.error
    _assert_tools_paired(messages)


@pytest.mark.parametrize("mode", ["direct", "task"])
def test_invalid_arguments_can_be_corrected_without_approval(mode):
    executed = []
    approvals = []
    reg = _registry(Tool(
        "save", "save", {"type": "object", "properties": {"value": {"type": "string"}},
                         "required": ["value"]},
        lambda args: executed.append(args) or "ok", requires_approval=True,
    ))

    class Approval:
        def request(self, name, args):
            approvals.append((name, args))
            return True

    router = FakeRouter([
        _resp_tool("c1", "save", {}),
        _resp_tool("c2", "save", {"value": "safe"}), _resp_text("done"),
    ])
    messages = []
    if mode == "task":
        out = Executor(router, reg, Approval()).run_task(
            Task("t1", "修改文件"), messages, system="", max_steps=4,
        )
        assert out.status == COMPLETED
    else:
        session = AgentSession(router, reg, Approval(), max_steps=4)
        assert session.chat("请修改代码") == "done"
        messages = session.messages
    assert executed == [{"value": "safe"}]
    assert approvals == [("save", {"value": "safe"})]
    _assert_tools_paired(messages)


@pytest.mark.parametrize("args", [None, [], {"path": 1},
    {"path": "x", "old_str": "original"},
    {"path": "x", "new_str": "appended"},
    {"path": "x", "old_str": "original", "new_str": None}])
def test_invalid_edit_never_mutates_file(args, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "x"
    target.write_text("original")
    output, error = _registry(EDIT_FILE).execute("edit_file", args, approved_action="edit_file")
    assert error
    assert "invalid arguments" in output
    assert target.read_text() == "original"


@pytest.mark.parametrize("args", [{"value": True}, {"value": "1"}, {"value": 0}])
def test_registry_enforces_schema_constraints_before_resolver(args):
    touched = []
    audit = []
    reg = ToolRegistry(audit_sink=audit.append)
    reg.register(Tool(
        "bounded", "bounded", {"type": "object", "properties": {
            "value": {"type": "integer", "minimum": 1}}, "required": ["value"]},
        lambda _: touched.append("execute") or "ok",
        approval_resolver=lambda _: touched.append("resolve"),
    ))
    output, error = reg.execute("bounded", args)
    assert error and output.startswith("invalid arguments")
    assert not touched
    assert audit[0].outcome == "invalid_arguments"


@pytest.mark.parametrize("tool,args", [
    (READ_FILE, {"path": "missing"}),
    (LIST_DIR, {"path": "missing"}),
    (EDIT_FILE, {"path": "missing", "old_str": "x", "new_str": "y"}),
    (EDIT_FILE, {"path": "x", "old_str": "absent", "new_str": "y"}),
    (EDIT_FILE, {"path": "x", "old_str": "repeated", "new_str": "y"}),
])
def test_file_failures_are_real_errors(tool, args, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "x"
    target.write_text("repeated repeated")
    output, error = _registry(tool).execute(tool.name, args, approved_action=tool.name)
    assert error, output
    assert target.read_text() == "repeated repeated"


@pytest.mark.parametrize("changed_args", [{}, {"alternate": True}])
def test_direct_does_not_reask_or_execute_denied_tool(changed_args):
    approvals = []
    executed = []

    class Approval(DenyAll):
        def request_tool(self, tool, action, args):
            approvals.append(args)
            return super().request_tool(tool, action, args)

    router = FakeRouter([
        _resp_tool("c1", "danger", {}), _resp_tool("c2", "danger", changed_args),
        _resp_text("已停止"),
    ])
    reg = _registry(Tool("danger", "danger", {"type": "object"},
                         lambda _: executed.append(True) or "ok", requires_approval=True))
    session = AgentSession(router, reg, Approval())
    assert session.chat("请执行命令") == "已停止"
    assert approvals == [{}]
    assert not executed
    assert session.last_run_status == "blocked"
    _assert_tools_paired(session.messages)


def test_fallback_plan_preserves_full_goal():
    goal = "读取项目并修改相关文件。" * 20 + "禁止删除文件，最后运行相关测试。"
    plan = Planner(FakeRouter([_resp_text("not json")])).create_plan(goal)
    assert plan.tasks[0].content == goal


@pytest.mark.parametrize("returncode", [1, 127])
def test_shell_exit_failure_is_error_even_after_truncation(returncode, monkeypatch, tmp_path):
    import subprocess
    from app.tools.builtin.shell import SHELL

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTLAB_RTK_ENABLED", "false")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], returncode, stdout="x" * 20_000, stderr="test failed",
    ))
    output, error = _registry(SHELL).execute("shell", {"command": "pytest"}, approved_action="shell")
    assert error
    assert f"[exit code: {returncode}]" in output
    assert "truncated" in output


def test_shell_timeout_is_error(monkeypatch, tmp_path):
    import subprocess
    from app.tools.builtin.shell import SHELL

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired(a[0], 30)

    monkeypatch.setattr(subprocess, "run", timeout)
    output, error = _registry(SHELL).execute("shell", {"command": "pytest"}, approved_action="shell")
    assert error and output.startswith("timeout:")


def test_failed_shell_cannot_be_used_as_direct_completion_evidence(monkeypatch, tmp_path):
    import subprocess
    from app.tools.builtin.shell import SHELL

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 1, stdout="", stderr="test failed",
    ))
    session = AgentSession(FakeRouter([
        _resp_tool("c1", "shell", {"command": "pytest"}),
        _resp_text("测试通过"), _resp_text("测试通过"),
    ]), _registry(SHELL), max_steps=4)
    with pytest.raises(RuntimeError):
        session.chat("运行测试")
    assert session.last_run_status == "failed"
