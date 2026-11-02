"""Native terminal selection/scrollback and unified progress status."""
import io
import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from app.util.composer import configure_composer, composer_output


def test_composer_does_not_take_over_terminal_mouse_or_alternate_screen():
    with create_pipe_input() as pipe:
        prompt = PromptSession(input=pipe, output=DummyOutput())
        view = configure_composer(prompt)
        assert not prompt.mouse_support
        assert not prompt.app.full_screen
        assert not prompt.app.renderer.full_screen
        assert not prompt.erase_when_done
        assert configure_composer(prompt) is view


def test_all_output_is_kept_in_native_stream_without_history_truncation(monkeypatch):
    sink = io.StringIO()
    monkeypatch.setattr('sys.stdout', sink)
    with create_pipe_input() as pipe:
        view = configure_composer(PromptSession(input=pipe, output=DummyOutput()))
        for index in range(3000):
            view.append_output(f"message {index}\n")
    text = sink.getvalue()
    assert text.startswith("message 0\n") and text.endswith("message 2999\n")
    assert len(text.splitlines()) == 3000


def test_output_proxy_flushes_final_tail_to_terminal(monkeypatch):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.output.vt100 import Vt100_Output
    from prompt_toolkit.data_structures import Size
    sink = io.StringIO()
    output = Vt100_Output(sink, lambda: Size(rows=24, columns=80), enable_cpr=False)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        view = configure_composer(PromptSession(input=pipe, output=output))
        with composer_output(view):
            print("tool output")
            print("final tail", end="")
    assert "tool output\nfinal tail\n" in sink.getvalue()


@pytest.mark.parametrize("cancelled,failed,title,phase", [
    (False, False, "Done", "已完成"),
    (True, False, "Stopped", "已停止"),
    (True, True, "Failed", "执行失败"),
])
def test_terminal_status_replaces_active_phase_and_preserves_duration_usage(
    monkeypatch, cancelled, failed, title, phase,
):
    from app.util import composer
    with create_pipe_input() as pipe:
        view = configure_composer(PromptSession(input=pipe, output=DummyOutput()))
        view.started = 100.0
        view.running = True
        view.status = "Thinking… (in 73.8k tokens · out 417 tokens · thinking)"
        view.activity = "执行 read_file"
        view.usage_summary = "in 73.8k tokens · out 417 tokens"
        monkeypatch.setattr(composer.time, "monotonic", lambda: 117.3)
        active = view.status_fragments()[0][1]
        assert active.split(" ", 1)[1] == "Thinking… (17.3s · in 73.8k tokens · out 417 tokens · 执行 read_file)"
        view.finish_turn(cancelled=cancelled, failed=failed)
        assert view.status == f"{title} (17.3s · in 73.8k tokens · out 417 tokens · {phase})"
        assert "thinking" not in view.status.lower()
        assert view.started is None and not view.running and not view.activity


@pytest.mark.parametrize("path", ["repl", "steering"])
def test_submitted_input_appears_once_in_native_terminal_history(monkeypatch, path):
    import asyncio
    import threading
    from types import SimpleNamespace
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.output.vt100 import Vt100_Output
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
    from app import cli
    from app.agent.cancel import CancelToken
    from app.util.live_turn import LiveTurnInput

    pyte = pytest.importorskip("pyte")
    captured = io.StringIO()
    captured.isatty = lambda: True
    stdin = io.StringIO()
    stdin.isatty = lambda: True
    monkeypatch.setattr(cli.sys, "stdout", captured)
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    output = Vt100_Output(captured, lambda: Size(rows=24, columns=60), enable_cpr=False)
    text = "unique request: " + "完整字段说明" * 15

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        if path == "repl":
            monkeypatch.setattr(cli, "PromptSession", lambda **kwargs: PromptSession(input=pipe, output=output, **kwargs))
            monkeypatch.setattr(cli, "AttachmentStore", lambda: SimpleNamespace())
            monkeypatch.setattr(cli, "_clipboard_key_bindings", lambda *args: KeyBindings())
            monkeypatch.setattr(cli, "_print_stats", lambda session: None)
            def chat(session, submitted, **kwargs):
                assert submitted == text
                pipe.send_text("exit\r")
                return "done"
            monkeypatch.setattr(cli, "_chat_with_cancel", chat)
            session = SimpleNamespace(auto_execute_enabled=True, messages=[])
            router = SimpleNamespace(current=session, current_id="61fbbd123",
                                     current_thread_summary=lambda: {"thread_id": "61fbbd123", "title": "zlm_1"})
            pipe.send_text(text + "\r")
            assert cli._repl(router) == 0
        else:
            prompt = PromptSession(input=pipe, output=output)
            live = LiveTurnInput(prompt, message="❯ ", style=Style([]), prepare_input=lambda value: value)
            token = CancelToken()
            started = threading.Event()
            def work():
                started.set()
                for _ in range(200):
                    if token.has_steering:
                        return token.drain_steering()[0]
                    token.wait(.01)
                raise AssertionError("steering not received")
            def type_input():
                assert started.wait(1)
                pipe.send_text(text + "\r")
            typer = threading.Thread(target=type_input)
            typer.start()
            try:
                assert live.run(work, token) == text
            finally:
                typer.join(2)

    screen = pyte.HistoryScreen(60, 24, history=1000)
    screen.set_mode(pyte.modes.LNM)
    pyte.Stream(screen).feed(captured.getvalue())
    rows = ["".join(line[x].data if x in line else " " for x in range(60)) for line in screen.history.top]
    rows += screen.display
    visible = "".join("".join(rows).split())
    assert visible.count("".join(text.split())) == 1
    if path == "repl":
        assert "[61fbbd·zlm_1]🚀❯" in visible
        assert "▸" not in visible


def test_turn_usage_accumulates_calls_and_refreshes_do_not_double_count():
    with create_pipe_input() as pipe:
        view = configure_composer(PromptSession(input=pipe, output=DummyOutput()))
        first, second = object(), object()
        assert "等待统计" in view.usage_summary
        view.record_usage(first, {"input_tokens": 1000, "output_tokens": 80})
        for _ in range(20):
            view.record_usage(first, {"input_tokens": 1000, "output_tokens": 80})
        view.record_usage(first, {"input_tokens": 900, "output_tokens": 70}, final=True)
        view.record_usage(second, {"input_tokens": 0, "output_tokens": 0})
        assert view.usage_totals == {"input_tokens": 900, "output_tokens": 70}
        assert "累计 in 900 tokens" in view.usage_summary
        view.record_usage(second, {"input_tokens": 1200, "output_tokens": 30}, final=True)
        assert view.usage_totals == {"input_tokens": 2100, "output_tokens": 100}
        view.set_total_usage({"input_tokens": 2200, "output_tokens": 105})
        view.record_usage(second, {"input_tokens": 0, "output_tokens": 0})
        assert view.usage_totals == {"input_tokens": 2200, "output_tokens": 105}
        view.reset_usage()
        assert view.usage_totals == {"input_tokens": 0, "output_tokens": 0}
        assert "等待统计" in view.usage_summary


def test_waiting_usage_is_distinct_from_confirmed_zero():
    with create_pipe_input() as pipe:
        view = configure_composer(PromptSession(input=pipe, output=DummyOutput()))
        source = object()
        view.record_usage(source, {"input_tokens": 0, "output_tokens": 0})
        assert "等待统计" in view.usage_summary
        assert "0 tokens" not in view.usage_summary
        view.record_usage(source, {"input_tokens": 0, "output_tokens": 0}, final=True)
        assert view.usage_summary == "累计 in 0 tokens · 累计 out 0 tokens"


def test_spinner_steps_preserve_cumulative_usage_while_next_request_waits(monkeypatch):
    from types import SimpleNamespace
    from app import cli
    with create_pipe_input() as pipe:
        view = configure_composer(PromptSession(input=pipe, output=DummyOutput()))
        monkeypatch.setattr(cli, "_LIVE_INPUT", SimpleNamespace(composer=view, invalidate=lambda: None))
        planning = cli._Spinner("planning")
        planning.update({"input_tokens": 700, "output_tokens": 100, "final": True})
        next_step = cli._Spinner("thinking")
        next_step._draw_footer()
        assert view.usage_totals == {"input_tokens": 700, "output_tokens": 100}
        next_step.update({"input_tokens": 1200, "output_tokens": 50})
        next_step._draw_footer()
        assert view.usage_totals == {"input_tokens": 1900, "output_tokens": 150}
        next_step.update({"input_tokens": 1180, "output_tokens": 45, "final": True})
        assert view.usage_totals == {"input_tokens": 1880, "output_tokens": 145}


def test_cli_final_usage_is_calibrated_from_runtime_turn_total(monkeypatch):
    from types import SimpleNamespace
    from app import cli
    with create_pipe_input() as pipe:
        view = configure_composer(PromptSession(input=pipe, output=DummyOutput()))
        class Live:
            composer = view
            def run(self, work, token):
                return work()
        def chat(*args, **kwargs):
            view.record_usage(object(), {"input_tokens": 100, "output_tokens": 10})
            return "done"
        session = SimpleNamespace(chat=chat, last_turn_usage={"input_tokens": 500, "output_tokens": 70})
        assert cli._chat_with_cancel(session, "request", live_input=Live()) == "done"
        assert view.usage_totals == {"input_tokens": 500, "output_tokens": 70}


@pytest.mark.parametrize("seconds,expected", [
    (59.9, "59.9s"), (60, "1m 0s"), (705.8, "11m 45s"), (3661, "1h 1m 1s"),
])
def test_elapsed_units_match_while_running_and_after_completion(monkeypatch, seconds, expected):
    from app.util import composer
    with create_pipe_input() as pipe:
        view = configure_composer(PromptSession(input=pipe, output=DummyOutput()))
        view.running = True
        view.started = 100
        monkeypatch.setattr(composer.time, "monotonic", lambda: 100 + seconds)
        assert f"({expected} ·" in view.status_fragments()[0][1]
        view.finish_turn()
        assert f"({expected} ·" in view.status


def test_thinking_icon_advances_with_clock_and_stops_on_completion(monkeypatch):
    from app.util import composer
    from app.util.progress_format import SPINNER_FRAMES
    clock = [100.0]
    monkeypatch.setattr(composer.time, "monotonic", lambda: clock[0])
    with create_pipe_input() as pipe:
        view = configure_composer(PromptSession(input=pipe, output=DummyOutput()))
        view.running = True
        view.started = 100
        first = view.status_fragments()[0][1][0]
        clock[0] += .15
        second = view.status_fragments()[0][1][0]
        assert first in SPINNER_FRAMES and second in SPINNER_FRAMES and first != second
        view.finish_turn()
        finished = view.status_fragments()
        clock[0] += 1
        assert view.status_fragments() == finished
        assert finished[0][1].startswith("✻ Done")


def test_compaction_clock_advances_between_events_and_across_chunks(monkeypatch):
    from types import SimpleNamespace
    from app import cli
    from app.agent import events
    from app.agent.events import RunEvent

    clock = [100.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    with create_pipe_input() as pipe:
        view = configure_composer(PromptSession(input=pipe, output=DummyOutput()))
        view.started = 90.0
        view.running = True
        monkeypatch.setattr(cli, "_LIVE_INPUT", SimpleNamespace(composer=view, invalidate=lambda: None))

        def progress(chunk, elapsed, remaining):
            cli._print_run_event(RunEvent(kind=events.CONTEXT_COMPACTION_PROGRESS, payload={
                "round": 1, "chunk": chunk, "chunks": 3, "attempt": 1,
                "elapsed_seconds": elapsed, "remaining_seconds": remaining,
            }))

        progress(1, 58, 62)
        clock[0] += 1
        assert "已用 59.0s · 剩余 1m 1s" in view.status_fragments()[0][1]
        clock[0] += 1
        assert "已用 1m 0s · 剩余 1m 0s" in view.status_fragments()[0][1]
        progress(2, 60, 60)
        clock[0] += 0.5
        text = view.status_fragments()[0][1]
        assert "分片 2/3" in text and "已用 1m 0s" in text
        clock[0] += 65
        assert "剩余 0.0s" in view.status_fragments()[0][1]
        cli._print_run_event(RunEvent(kind=events.CONTEXT_COMPACTION_FAILED, text="timeout"))
        assert view.activity == ""
        assert "压缩上下文" not in view.status_fragments()[0][1]
