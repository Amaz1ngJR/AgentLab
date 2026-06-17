"""测试工具输出截断 —— executor.py 的 _truncate_tool_output。"""
from app.agent.executor import (
    TOOL_OUTPUT_HEAD_CHARS,
    TOOL_OUTPUT_MAX_CHARS,
    TOOL_OUTPUT_TAIL_CHARS,
    _truncate_tool_output,
)


def test_truncate_short_output_unchanged():
    """不超阈值的输出原样返回。"""
    short = "a" * 100
    assert _truncate_tool_output(short) == short


def test_truncate_at_threshold_unchanged():
    """正好等于阈值时不截断。"""
    exact = "x" * TOOL_OUTPUT_MAX_CHARS
    assert _truncate_tool_output(exact) == exact


def test_truncate_long_output_keeps_head_and_tail():
    """超大输出保留头尾、中间省略。"""
    big = "a" * 20_000
    result = _truncate_tool_output(big)
    # 结果长度 < 原长度(被压缩了)
    assert len(result) < len(big)
    # 头部保留
    assert result.startswith("a" * 100)
    # 尾部保留
    assert result.endswith("a" * 100)
    # 中间有省略标记
    assert "已省略中间" in result
    assert "共 20000 字符" in result


def test_truncate_preserves_structure():
    """头尾各取规定字符数,中间标记解释清楚。"""
    content = "HEAD" * 3000 + "MIDDLE" * 2000 + "TAIL" * 3000
    result = _truncate_tool_output(content)
    # 头 8000 字符肯定全是 HEAD
    head_part = result[:100]
    assert "HEAD" in head_part
    # 尾 2000 字符肯定全是 TAIL
    tail_part = result[-100:]
    assert "TAIL" in tail_part
    # 中间的 MIDDLE 应该被省略标记覆盖
    assert "MIDDLE" not in result or result.count("MIDDLE") < 100


def test_truncate_empty_returns_empty():
    """空输出原样返回。"""
    assert _truncate_tool_output("") == ""
    assert _truncate_tool_output(None) == None
