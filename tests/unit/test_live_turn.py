"""Exercise real prompt_toolkit input alongside a background turn, offline."""
import asyncio
import threading
import time

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles import Style

from app.agent.cancel import CancelToken, Cancelled
from app.models.router import call_create_message
from app.util.input_arbiter import foreground_stdin
from app.util.live_turn import LiveTurnInput


@pytest.mark.parametrize("key", ["\x1b", "\x03"])
def test_live_prompt_stops_blocked_model_and_preserves_draft(key):
    release = threading.Event()
    started = threading.Event()
    late = threading.Event()
    callbacks = []
    history = [{"role": "user", "content": "original"}]

    class BlockedModel:
        def create_message(self, messages, on_text_delta=None, **kwargs):
            started.set()
            release.wait(2)
            messages.append({"role": "assistant", "content": "late mutation"})
            on_text_delta("late text")
            late.set()
            return "late result"

    with create_pipe_input() as pipe:
        prompt = PromptSession(input=pipe, output=DummyOutput())
        live = LiveTurnInput(prompt, message="> ", style=Style([]), prepare_input=lambda s: s)
        token = CancelToken()

        def work():
            try:
                return call_create_message(
                    BlockedModel(), history, cancel=token, on_text_delta=callbacks.append,
                )
            except Cancelled:
                return "stopped"

        def type_keys():
            assert started.wait(1)
            pipe.send_text("unfinished draft" + key)

        typer = threading.Thread(target=type_keys)
        typer.start()
        start = time.monotonic()
        try:
            assert live.run(work, token) == "stopped"
            assert time.monotonic() - start < 0.8
            assert live.draft == "unfinished draft"
        finally:
            release.set()
            typer.join(1)
        assert late.wait(1)
        assert callbacks == []
        assert history == [{"role": "user", "content": "original"}]


def test_enter_steers_running_turn_without_cancelling():
    started = threading.Event()
    with create_pipe_input() as pipe:
        prompt = PromptSession(input=pipe, output=DummyOutput())
        live = LiveTurnInput(prompt, message="> ", style=Style([]), prepare_input=lambda s: s)
        token = CancelToken()

        def work():
            started.set()
            deadline = time.monotonic() + 2
            while not token.has_steering and time.monotonic() < deadline:
                token.wait(0.01)
            return token.drain_steering()

        def type_keys():
            assert started.wait(1)
            pipe.send_text("focus on tests\r")

        typer = threading.Thread(target=type_keys)
        typer.start()
        assert live.run(work, token) == ["focus on tests"]
        typer.join(1)
        assert not token.cancelled


def test_approval_prompt_exclusively_reads_input_then_restores_composer():
    waiting = threading.Event()
    with create_pipe_input() as pipe:
        prompt = PromptSession(input=pipe, output=DummyOutput())
        live = LiveTurnInput(prompt, message="> ", style=Style([]), prepare_input=lambda s: s)
        token = CancelToken()

        def work():
            with foreground_stdin():
                menu = PromptSession(input=pipe, output=DummyOutput())
                answer = menu.prompt("approve? ", pre_run=waiting.set)
            deadline = time.monotonic() + 2
            while not token.has_steering and time.monotonic() < deadline:
                token.wait(0.01)
            return answer, token.drain_steering()

        async def scenario():
            async def keys():
                while not waiting.is_set():
                    await asyncio.sleep(0.01)
                pipe.send_text("yes\r")
                while live._resume is None or not live._resume.done():
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
                pipe.send_text("then verify\r")
            typing = asyncio.create_task(keys())
            try:
                return await asyncio.wait_for(live._run(work, token), 3)
            finally:
                await typing

        assert asyncio.run(scenario()) == ("yes", ["then verify"])


def test_model_progress_callbacks_stay_on_execution_thread():
    caller = threading.get_ident()
    seen = []

    class Model:
        def create_message(self, messages, on_text_delta=None, **kwargs):
            on_text_delta("text")
            return "ok"

    assert call_create_message(
        Model(), [], cancel=CancelToken(),
        on_text_delta=lambda text: seen.append((threading.get_ident(), text)),
    ) == "ok"
    assert seen == [(caller, "text")]


@pytest.mark.parametrize("mode", ["direct", "task"])
def test_steering_skips_obsolete_tools_and_preserves_result_pairs(mode):
    from app.agent.approval import AutoApprove
    from app.agent.executor import Executor
    from app.agent.runtime import AgentSession
    from app.agent.tasks import Task
    from app.models.protocol import ModelResponse, ToolCall
    from app.tools.registry import Tool, ToolRegistry

    token = CancelToken()
    calls = []
    seen = []
    tool_calls = [ToolCall(id="one", name="first", arguments={}), ToolCall(id="two", name="obsolete", arguments={})]

    class Model:
        model = "fake"
        provider = "fake"

        def create_message(self, messages, **kwargs):
            seen.append(list(messages))
            if len(seen) == 1:
                return ModelResponse(
                    text="", tool_calls=tool_calls, usage={},
                    provider_payload=[{"role": "assistant", "content": [
                        {"type": "tool_use", "id": c.id, "name": c.name, "input": {}}
                        for c in tool_calls
                    ]}],
                )
            return ModelResponse(text="adjusted", tool_calls=[], usage={}, provider_payload=[])

        def format_tool_results(self, results):
            return [{"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r.tool_call_id, "content": r.output}
                for r in results
            ]}]

    def first(args):
        calls.append("first")
        token.steer("skip the remaining action and explain")
        return "done"

    registry = ToolRegistry()
    for name, execute in [("first", first), ("obsolete", lambda a: calls.append("obsolete") or "bad")]:
        registry.register(Tool(name=name, description=name, input_schema={"type": "object"}, executor=execute))
    model = Model()
    if mode == "direct":
        session = AgentSession(model, registry, AutoApprove(), mode="direct")
        assert session.chat("hello", cancel=token) == "adjusted"
    else:
        executor = Executor(model, registry, AutoApprove())
        assert executor.run_task(
            Task(id="t", content="hello"), [], system="test", max_steps=5, cancel=token,
        ).text == "adjusted"
    assert calls == ["first"]
    results = seen[1][-2]["content"]
    assert [r["tool_use_id"] for r in results] == ["one", "two"]
    assert seen[1][-1] == {"role": "user", "content": "skip the remaining action and explain"}


def test_cancelled_builtin_read_does_not_wait_for_network_timeout():
    from app.tools.registry import Tool, ToolRegistry

    started, release = threading.Event(), threading.Event()
    token = CancelToken()
    stopped = threading.Event()
    audits = []
    registry = ToolRegistry(audit_sink=audits.append)

    def read(args):
        started.set()
        release.wait(2)
        return "late page"

    registry.register(Tool(
        name="web_fetch", description="fetch", input_schema={"type": "object"},
        executor=read, risk="network", requires_approval=False,
    ))

    def execute():
        try:
            registry.execute("web_fetch", {}, cancel=token)
        except Cancelled:
            stopped.set()

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        assert started.wait(1)
        token.cancel()
        assert stopped.wait(0.3)
        assert audits[-1].outcome == "cancelled"
    finally:
        release.set()
        worker.join(1)


def test_input_remains_available_during_cancellation_cleanup():
    started, release = threading.Event(), threading.Event()
    with create_pipe_input() as pipe:
        prompt = PromptSession(input=pipe, output=DummyOutput())
        original_bindings = prompt.key_bindings
        live = LiveTurnInput(prompt, message="> ", style=Style([]), prepare_input=lambda s: s)
        token = CancelToken()

        def work():
            started.set()
            release.wait(2)
            return "stopped"

        async def scenario():
            async def keys():
                while not started.is_set():
                    await asyncio.sleep(0.01)
                pipe.send_text("\x03next direction\r")
                deadline = time.monotonic() + 1
                while live.next_input is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                release.set()
            typing = asyncio.create_task(keys())
            try:
                return await live._run(work, token)
            finally:
                release.set()
                await typing

        assert asyncio.run(scenario()) == "stopped"
        assert token.cancelled
        assert live.next_input == "next direction"
        assert prompt.key_bindings is original_bindings
        assert prompt.bottom_toolbar is None


def test_streaming_spinner_does_not_erase_the_live_composer(monkeypatch):
    from app import cli
    from prompt_toolkit.application import create_app_session

    class RecordingOutput(DummyOutput):
        def __init__(self):
            self.parts = []

        def write(self, text):
            self.parts.append(text)

        def write_raw(self, text):
            self.parts.append(text)

    started = threading.Event()
    with create_pipe_input() as pipe:
        output = RecordingOutput()
        prompt = PromptSession(input=pipe, output=output)
        live = LiveTurnInput(prompt, message="> ", style=Style([]), prepare_input=lambda s: s)
        monkeypatch.setattr(cli, "_LIVE_INPUT", live)
        token = CancelToken()

        def work():
            with cli._Spinner("thinking") as spinner:
                spinner.on_text("streamed answer\n")
                spinner.update({"input_tokens": 100, "output_tokens": 5})
                started.set()
                token.wait(2)
            return "stopped"

        def type_keys():
            assert started.wait(1)
            pipe.send_text("draft\x03")

        typer = threading.Thread(target=type_keys)
        typer.start()
        with create_app_session(input=pipe, output=output):
            assert live.run(work, token) == "stopped"
        typer.join(1)
        assert live.draft == "draft"
        text = "".join(output.parts)
        assert "streamed answer" in text
        assert "\033[J" not in text


def test_loop_command_verification_is_cancelled_before_timeout(tmp_path):
    import sys
    from app.agent.approval import AutoApprove
    from app.agent.goals import VerificationCheck
    from app.agent.verifier import Verifier

    token = CancelToken()
    verifier = Verifier(str(tmp_path), approval=AutoApprove())
    timer = threading.Timer(0.15, token.cancel)
    timer.start()
    start = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            verifier.verify([VerificationCheck(
                type="command", command=f'"{sys.executable}" -c "import time; time.sleep(30)"',
                timeout=30,
            )], cancel=token)
        assert time.monotonic() - start < 2
    finally:
        timer.cancel()
        timer.join(1)


def test_windows_command_cancel_requests_process_tree_termination(monkeypatch, tmp_path):
    import subprocess
    from app.util import process

    token = CancelToken()
    killed = threading.Event()
    commands = []
    options = []

    class Child:
        pid = 12345
        returncode = None

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            token.cancel()
            return "", ""

        def kill(self):
            self.returncode = -9

    child = Child()

    def spawn(argv, **kwargs):
        options.append(kwargs)
        return child

    def taskkill(argv, **kwargs):
        commands.append(argv)
        child.returncode = -9
        killed.set()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(process.platform, "system", lambda: "Windows")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 134217728, raising=False)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(subprocess, "run", taskkill)
    with pytest.raises(Cancelled):
        process.run_process(["powershell", "-Command", "wait"], cwd=str(tmp_path), timeout=30, cancel=token)
    assert killed.wait(1)
    assert commands == [["taskkill", "/PID", "12345", "/T", "/F"]]
    assert options[0]["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP


@pytest.mark.parametrize("columns", [32, 60, 100])
def test_terminal_repaint_preserves_partial_output_and_final_line(columns):
    import io
    import sys
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.output.vt100 import Vt100_Output

    pyte = pytest.importorskip("pyte")
    captured = io.StringIO()
    output = Vt100_Output(captured, lambda: Size(rows=24, columns=columns), enable_cpr=False)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        prompt = PromptSession(input=pipe, output=output)
        live = LiveTurnInput(prompt, message="[player] > ", style=Style([]), prepare_input=lambda s: s)

        def work():
            for chunk in ["开头：已有的 ", "Enhanced FLV 支持。\n", "DLL 或声音 ", "Windows 播放已修复。"]:
                sys.stdout.write(chunk)
                sys.stdout.flush()
                time.sleep(0.25)
            return "done"

        assert live.run(work, CancelToken()) == "done"
        pipe.send_text("exit\r")
        assert prompt.prompt("NEXT> ") == "exit"
    screen = pyte.HistoryScreen(columns, 24, history=1000)
    # A real terminal's output processing returns to column zero on newline.
    screen.set_mode(pyte.modes.LNM)
    pyte.Stream(screen).feed(captured.getvalue())
    rows = ["".join(line[x].data if x in line else " " for x in range(columns)) for line in screen.history.top]
    rows += screen.display
    visible = "\n".join(rows)
    compact = "".join(visible.split())
    assert "开头：已有的EnhancedFLV支持。" in compact
    assert "DLL或声音Windows播放已修复。" in compact
    assert not any("已修复。" in row and "NEXT>" in row for row in rows)


def test_line_buffer_flush_preserves_prefix_and_final_tail():
    import io
    from app.util.live_turn import _LineBufferedStream

    output = io.StringIO()
    stream = _LineBufferedStream(output)
    stream.write("已有的 ")
    stream.flush()
    assert output.getvalue() == ""
    stream.write("Enhanced FLV 支持。\nDLL 或声音 ")
    stream.flush()
    assert output.getvalue() == "已有的 Enhanced FLV 支持。\n"
    stream.write("Windows 播放已修复。")
    stream.flush()
    stream.finish()
    stream.finish()
    assert output.getvalue() == "已有的 Enhanced FLV 支持。\nDLL 或声音 Windows 播放已修复。\n"


def test_line_buffer_preserves_ansi_split_between_writes():
    import io
    from app.util.live_turn import _LineBufferedStream

    output = io.StringIO()
    stream = _LineBufferedStream(output)
    for chunk in ["\033[", "2m思考", "\033[0m", "\n", "正文"]:
        stream.write(chunk)
        stream.flush()
    stream.finish()
    assert output.getvalue() == "\033[2m思考\033[0m\n正文\n"
