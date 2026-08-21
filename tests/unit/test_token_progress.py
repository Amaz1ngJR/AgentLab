"""实时 token 估算器测试。"""
from app.models.token_progress import StreamingTokenProgress, estimate_stream_tokens


def test_estimate_stream_tokens_handles_cjk_and_ascii():
    assert estimate_stream_tokens("你好") == 2
    assert estimate_stream_tokens("abcdefgh") == 2


def test_progress_changes_for_reasoning_before_final_text():
    seen = []
    progress = StreamingTokenProgress(seen.append, input_tokens=100)
    progress.emit(force=True)
    progress.add_reasoning("这是推理过程")
    progress.add_reasoning("继续推理")
    assert seen[0] == {"input_tokens": 100, "output_tokens": 0}
    assert seen[-1]["output_tokens"] > seen[1]["output_tokens"]


def test_final_usage_replaces_stream_estimate():
    seen = []
    progress = StreamingTokenProgress(seen.append, input_tokens=5)
    progress.add_text("a" * 40)
    assert seen[-1]["output_tokens"] == 10
    progress.set_usage(input_tokens=22, output_tokens=7, force=True, final=True)
    assert seen[-1] == {"input_tokens": 22, "output_tokens": 7, "final": True}
