import json

import pytest

from app.agent.delegation_context import OMISSION, bounded_brief, build_handoff


@pytest.mark.parametrize("text", ["", "中文与特殊字符：<>&\\\"", "代码定位：\napp/x.py:run\n\n本次边界：只检查 run"])
def test_short_brief_preserves_content(text):
    assert bounded_brief(text, 1000) == (text, False)


def test_long_brief_preserves_complete_priority_sections():
    text = ("已完成工作：\n" + "无关日志" * 500 +
            "\n\n代码定位：\napp/x.py:run\n\n本次边界：\n只检查 run\n\n交付要求：\n返回证据")
    result, clipped = bounded_brief(text, 200)
    assert clipped and len(result) <= 200
    for section in ["代码定位：\napp/x.py:run", "本次边界：\n只检查 run", "交付要求：\n返回证据"]:
        assert section in result
    assert "无关日志" not in result
    assert result.endswith(OMISSION)


@pytest.mark.parametrize("text", [json.dumps({"data": "x" * 1000}), "```json\n{\n\"data\": \"" + "x" * 1000 + "\"\n}\n```"])
def test_oversized_structured_block_is_not_sliced(text):
    assert bounded_brief(text, 150) == (OMISSION, True)


def test_handoff_redacts_and_prioritizes_explicit_context():
    result, diagnostics = build_handoff("background " * 1000, "已确认事实：api_key=super_secret_value")
    assert result.startswith("【本次任务交接】")
    assert "super_secret_value" not in result
    assert diagnostics["truncated"]
    assert diagnostics["mode"] == "brief"
    assert diagnostics["output_chars"] == len(result)
    assert len(result) < 20_000


def test_snapshot_is_labelled_and_diagnostics_do_not_contain_text():
    result, diagnostics = build_handoff("", "", "HISTORY", snapshot_truncated=True)
    assert "非原生前缀" in result
    assert diagnostics == {"mode": "snapshot", "input_chars": 7,
                           "output_chars": len(result), "truncated": True}


def test_redaction_precedes_size_check():
    result, clipped = bounded_brief("api_key=" + "s" * 500, 100)
    assert result == "api_key=***"
    assert not clipped


def test_json_embedded_in_prose_with_blank_lines_is_atomic():
    text = 'Evidence:\n{\n"first": "ok",\n\n"large": "' + 'x' * 1000 + '"\n}\n\n本次边界：只核实'
    result, clipped = bounded_brief(text, 160)
    assert clipped
    assert '"first"' not in result and '"large"' not in result
    assert "本次边界：只核实" in result


def test_fenced_heading_is_not_treated_as_task_boundary():
    text = '```text\n本次边界：not a heading\n' + 'x' * 1000 + '\n```\n\n本次边界：real boundary'
    result, clipped = bounded_brief(text, 160)
    assert clipped and 'not a heading' not in result
    assert '本次边界：real boundary' in result
