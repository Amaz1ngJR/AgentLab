"""测试工具输出截断 —— executor.py 的 _truncate_tool_output。"""
from app.agent.executor import (
    TOOL_OUTPUT_MAX_CHARS,
    _truncate_tool_output,
)
from app.agent.context_budget import estimate_tokens
from app.tools.output_budget import TOOL_OUTPUT_MAX_TOKENS


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


def test_dense_output_is_bounded_and_can_be_read_back(monkeypatch, tmp_path):
    import re
    from app.agent.tool_batch import ToolCallBatch
    from app.models.protocol import ToolCall
    from app.tools.output_archive import read_output

    monkeypatch.setenv("AGENTLAB_OUTPUT_CACHE_DIR", str(tmp_path))
    output = "起点" + "测试" * 2500 + "结尾"
    batch = ToolCallBatch({"read_tool_output": object()}, [])
    preview = batch.limit_output(ToolCall("shell1", "shell", {}), output, False)
    assert len(output) < TOOL_OUTPUT_MAX_CHARS
    assert estimate_tokens(preview) <= TOOL_OUTPUT_MAX_TOKENS
    assert preview.startswith("起点") and preview.endswith("结尾")
    output_id = re.search(r"out_[a-f0-9]{64}", preview).group()
    first = read_output({"output_id": output_id})
    next_column = int(re.search(r"column_offset=(\d+)", first).group(1))
    second = read_output({"output_id": output_id, "column_offset": next_column})
    assert first.splitlines()[1] + second.splitlines()[1] == output
    assert estimate_tokens(first) <= TOOL_OUTPUT_MAX_TOKENS


def test_dense_batch_file_result_keeps_json_and_pending_indices():
    import json
    from app.agent.tool_batch import ToolCallBatch
    from app.models.protocol import ToolCall

    data = {"files": [{"index": index, "content": "内容" * 900} for index in range(2)],
            "pending_indices": []}
    batch = ToolCallBatch({}, [])
    output = batch.limit_output(ToolCall("batch", "read_files", {}), json.dumps(data, ensure_ascii=False), False)
    assert estimate_tokens(output) <= TOOL_OUTPUT_MAX_TOKENS
    result = json.loads(output)
    assert result["files"] == data["files"][:1]
    assert result["pending_indices"] == [1]
    assert result["truncated"] is True
