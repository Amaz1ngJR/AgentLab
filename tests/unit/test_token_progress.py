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


def test_fragmented_channels_match_whole_text_estimates():
    text = "a你bc好d🙂EF" * 100
    reasoning = "推理a零碎bc" * 73
    for size in (1, 3, 8, 31):
        progress = StreamingTokenProgress(None)
        for offset in range(0, max(len(text), len(reasoning)), size):
            progress.add_text(text[offset:offset + size])
            progress.add_reasoning(reasoning[offset:offset + size])
        assert progress.output_tokens == estimate_stream_tokens(text) + estimate_stream_tokens(reasoning)


def test_counting_work_is_linear_and_does_not_retain_stream_text(monkeypatch):
    import app.models.token_progress as module
    original = module._count_stream_chars
    inspected = 0
    def count(delta):
        nonlocal inspected
        inspected += len(delta)
        return original(delta)
    monkeypatch.setattr(module, "_count_stream_chars", count)
    progress = StreamingTokenProgress(None)
    for _ in range(4000):
        progress.add_text("abcd你好ef")
        progress.add_reasoning("x")
    assert inspected == 36000
    assert not hasattr(progress, "_text") and not hasattr(progress, "_reasoning")


def test_final_usage_emits_even_when_values_are_unchanged():
    seen = []
    progress = StreamingTokenProgress(seen.append)
    progress.add_text("abcd")
    progress.set_usage(output_tokens=1, final=True)
    assert seen[-1] == {"input_tokens": 0, "output_tokens": 1, "final": True}


def test_ascii_rounding_is_per_channel_not_per_delta():
    progress = StreamingTokenProgress(None)
    for _ in range(4):
        progress.add_text("a")
    progress.add_reasoning("b")
    assert progress.output_tokens == 2
