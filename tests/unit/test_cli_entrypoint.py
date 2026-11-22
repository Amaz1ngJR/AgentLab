"""CLI 全局入口与 workspace 参数测试。"""
from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import cli


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_exposes_agentlab_command():
    config = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert config["project"]["scripts"]["agentlab"] == "app.cli:main"
    assert config["project"]["requires-python"] == ">=3.11"


def test_main_workspace_overrides_environment_before_session_build(
    monkeypatch,
    tmp_path,
):
    previous = tmp_path / "previous"
    target = tmp_path / "target"
    previous.mkdir()
    target.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(previous))
    monkeypatch.chdir(tmp_path)

    router = MagicMock()

    def fake_build_session(*, auto_approve, profile):
        assert auto_approve is False
        assert profile is None
        assert Path(cli.os.environ["WORKSPACE_ROOT"]) == target.resolve()
        return router

    monkeypatch.setattr(cli, "_build_session", fake_build_session)
    monkeypatch.setattr(cli, "_repl", lambda current_router: 0)

    assert cli.main(["--workspace", "target"]) == 0
    router.close.assert_called_once()


def test_main_rejects_image_without_prompt(monkeypatch):
    build_session = MagicMock()
    monkeypatch.setattr(cli, "_build_session", build_session)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--image", "shot.png"])
    assert exc.value.code == 2
    build_session.assert_not_called()


def test_main_rejects_missing_workspace(monkeypatch, tmp_path):
    build_session = MagicMock()
    monkeypatch.setattr(cli, "_build_session", build_session)

    with pytest.raises(SystemExit) as exc:
        cli.main(["--workspace", str(tmp_path / "missing")])

    assert exc.value.code == 2
    build_session.assert_not_called()



def test_main_keeps_green_startup_on_native_stdout(monkeypatch, capsys):
    router = MagicMock()
    monkeypatch.setattr(cli, "_supports_color", lambda *args, **kwargs: True)
    def build(**kwargs):
        cli._print_init("== AgentLab vtest ==")
        cli._print_init("model    : local-test-model")
        return router
    monkeypatch.setattr(cli, "_build_session", build)
    monkeypatch.setattr(cli, "_repl", lambda current: 0)
    assert cli.main([]) == 0
    printed = capsys.readouterr().out
    assert printed.count("AgentLab vtest") == 1
    assert printed.count("local-test-model") == 1
    assert "\x1b[32m" in printed


# ── 启动横幅：模型详情与恢复会话规模 ──────────────────────────────────────────
# 这些值决定"压缩何时介入""每轮最多产出多少""每次请求要重发多少输入"，
# 是排查长任务异常与额度消耗时最先要核对的信息。


def _fake_llm(**overrides):
    from app.models.context_window import ContextWindow
    llm = MagicMock()
    llm.context_window = ContextWindow(1_000_000, "config")
    llm.temperature = 0.2
    llm.top_p = None
    llm.reasoning_effort = "medium"
    # MagicMock 会把任意属性都变成真值，必须显式给默认值，
    # 否则"已忽略"标注会在所有测试里误触发。
    llm.temperature_ignored = False
    llm._adapter._cfg.max_tokens = 32768
    for key, value in overrides.items():
        setattr(llm, key, value)
    return llm


def test_model_detail_lines_show_window_sampling_and_output_cap():
    lines = cli._model_detail_lines(_fake_llm())
    joined = "\n".join(lines)
    assert "1,000,000 tokens" in joined
    assert "来源: config" in joined
    assert "temperature=0.2" in joined
    assert "推理强度 : medium" in joined
    assert "32,768 tokens/轮" in joined


def test_model_detail_lines_omit_unset_sampling_params():
    llm = _fake_llm(temperature=None, reasoning_effort=None)
    joined = "\n".join(cli._model_detail_lines(llm))
    assert "采样" not in joined  # 全部为 None 时不打印空行
    assert "top_p" not in joined


def test_model_detail_lines_handle_missing_max_tokens():
    llm = _fake_llm()
    llm._adapter._cfg.max_tokens = None
    assert not any("输出上限" in line for line in cli._model_detail_lines(llm))


def test_session_resume_summary_reports_size_and_percentage():
    messages = [{"role": "user", "content": "x" * 4000} for _ in range(50)]
    detail, warning = cli._session_resume_summary(messages, 1_000_000)
    assert "历史消息 50 条" in detail
    assert "tokens" in detail
    assert "占窗口" in detail
    assert warning == ""


def test_session_resume_summary_warns_when_history_dominates_window():
    messages = [{"role": "user", "content": "x" * 40_000} for _ in range(100)]
    detail, warning = cli._session_resume_summary(messages, 100_000)
    assert "⚠" in warning
    assert "/context compact" in warning
    assert "/session new" in warning


def test_session_resume_summary_handles_empty_history():
    detail, warning = cli._session_resume_summary([], 1_000_000)
    assert detail == "历史消息 0 条"
    assert warning == ""


def test_session_resume_summary_without_known_window_omits_percentage():
    messages = [{"role": "user", "content": "x" * 4000}]
    detail, warning = cli._session_resume_summary(messages, 0)
    assert "占窗口" not in detail
    assert warning == ""


# ── /tools 与能力概览 ────────────────────────────────────────────────────────
# 工具清单从启动横幅移到 /tools：十几个名字平铺一行读不动，而分组计数足够
# 让人判断"这个会话能做什么"。


def test_tool_names_are_grouped_by_purpose():
    names = ["read_file", "shell", "web_search", "write_file", "request_confirmation"]
    grouped = dict(cli._group_tool_names(names))
    assert grouped["读取"] == ["read_file"]
    assert grouped["写入"] == ["write_file"]
    assert grouped["执行"] == ["shell"]
    assert grouped["网络"] == ["web_search"]
    assert grouped["交互"] == ["request_confirmation"]


def test_unknown_tools_are_not_dropped_from_grouping():
    names = ["read_file", "some_future_tool"]
    grouped = dict(cli._group_tool_names(names))
    assert grouped["其他"] == ["some_future_tool"]
    assert sum(len(v) for v in grouped.values()) == len(names)


def test_tools_summary_line_reports_counts_not_full_list():
    names = ["read_file", "shell", "web_search"]
    line = cli._tools_summary_line(names, mcp_count=0, skill_total=6, skill_enabled=3)
    assert "3 个" in line
    assert "/tools" in line
    assert "Skill 3/6 启用" in line
    # 不该把具体工具名塞回横幅
    assert "read_file" not in line


def test_tools_summary_line_mentions_mcp_when_present():
    line = cli._tools_summary_line(["shell"], mcp_count=2, skill_total=0)
    assert "MCP 2 个 server" in line


def test_tools_command_lists_groups_and_skills():
    from app import cli as cli_module
    output = cli_module._handle_tools_command(None, "/tools")
    assert "内置工具" in output
    assert "[读取]" in output
    assert "read_file" in output
    assert "Skill" in output


def test_tools_command_filters_to_skills_only():
    from app import cli as cli_module
    output = cli_module._handle_tools_command(None, "/tools skills")
    assert "内置工具" not in output


def test_model_detail_lines_show_reasoning_effort_on_its_own_line():
    """推理强度单独一行：它直接决定延迟与思考 token 消耗。"""
    joined = "\n".join(cli._model_detail_lines(_fake_llm()))
    assert "推理强度 : medium" in joined


def test_model_detail_lines_omit_reasoning_effort_when_unset():
    llm = _fake_llm(reasoning_effort=None)
    assert not any("推理强度" in line for line in cli._model_detail_lines(llm))


def test_temperature_annotation_when_model_ignores_it():
    """claude-fable-5-2 只接受默认采样温度，adapter 会把参数摘掉。

    配置值仍显示出来，但必须标注"已忽略"，否则会被当成生效值去解释采样差异。
    """
    llm = _fake_llm(temperature_ignored=True)
    joined = "\n".join(cli._model_detail_lines(llm))
    assert "temperature=0.2（模型不支持，已忽略）" in joined


def test_temperature_not_annotated_when_model_supports_it():
    joined = "\n".join(cli._model_detail_lines(_fake_llm(temperature_ignored=False)))
    assert "temperature=0.2" in joined
    assert "已忽略" not in joined


def test_router_exposes_temperature_ignored_flag():
    from app.models.router import ModelRouter
    adapter = MagicMock()
    adapter._omit_temperature = True
    adapter._cfg.temperature = 0.2
    assert ModelRouter(adapter).temperature_ignored is True
    adapter._omit_temperature = False
    assert ModelRouter(adapter).temperature_ignored is False


def test_router_temperature_ignored_false_without_attribute():
    from app.models.router import ModelRouter
    adapter = MagicMock(spec=["_cfg"])
    adapter._cfg.temperature = 0.2
    assert ModelRouter(adapter).temperature_ignored is False


def test_window_source_warning_when_window_is_estimated():
    """兜底窗口值需要告警：它看起来正常，但可能远小于模型真实上限。"""
    from app.models.context_window import ContextWindow
    llm = _fake_llm(context_window=ContextWindow(64_000, "default"))
    warning = cli._window_source_warning(llm)
    assert warning.startswith("⚠")
    assert "64,000 tokens" in warning
    assert "context_size" in warning


@pytest.mark.parametrize("source", ["config", "provider"])
def test_window_source_warning_silent_for_declared_windows(source):
    from app.models.context_window import ContextWindow
    llm = _fake_llm(context_window=ContextWindow(1_000_000, source))
    assert cli._window_source_warning(llm) == ""


def test_window_source_warning_survives_broken_llm():
    """探测失败时不该让整个启动横幅崩掉。"""
    class Broken:
        @property
        def context_window(self):
            raise RuntimeError("no window")
    assert cli._window_source_warning(Broken()) == ""
