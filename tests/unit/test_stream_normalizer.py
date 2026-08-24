"""Provider 流事件增量规范化测试。"""
from app.models.stream_normalizer import StreamDeltaNormalizer


def test_true_deltas_are_appended_unchanged():
    normalizer = StreamDeltaNormalizer()
    chunks = [
        normalizer.normalize("text", "目前可确定："),
        normalizer.normalize("text", "蓝屏无信号"),
        normalizer.normalize("text", "。"),
    ]
    assert chunks == ["目前可确定：", "蓝屏无信号", "。"]
    assert normalizer.emitted("text") == "目前可确定：蓝屏无信号。"


def test_cumulative_snapshots_only_emit_new_suffix():
    normalizer = StreamDeltaNormalizer()
    snapshots = [
        "目前可确定：蓝屏无信号",
        "目前可确定：蓝屏无信号对应容器退出",
        "目前可确定：蓝屏无信号对应容器退出后分辨率回退。",
    ]
    chunks = [normalizer.normalize("text", value) for value in snapshots]
    assert chunks == [
        "目前可确定：蓝屏无信号",
        "对应容器退出",
        "后分辨率回退。",
    ]
    assert "".join(chunks) == snapshots[-1]


def test_exact_long_duplicate_chunk_is_dropped():
    normalizer = StreamDeltaNormalizer()
    assert normalizer.normalize("reasoning", "分析中，正在检查证据") == "分析中，正在检查证据"
    assert normalizer.normalize("reasoning", "分析中，正在检查证据") == ""


def test_repeated_single_character_delta_is_preserved():
    normalizer = StreamDeltaNormalizer()
    assert normalizer.normalize("text", "p") == "p"
    assert normalizer.normalize("text", "p") == "p"
    assert normalizer.emitted("text") == "pp"


def test_channels_are_independent():
    normalizer = StreamDeltaNormalizer()
    assert normalizer.normalize("reasoning", "A") == "A"
    assert normalizer.normalize("content", "A") == "A"
    assert normalizer.emitted("reasoning") == "A"
    assert normalizer.emitted("content") == "A"


def test_legitimate_non_consecutive_repetition_is_preserved():
    normalizer = StreamDeltaNormalizer()
    chunks = [
        normalizer.normalize("text", "same\n"),
        normalizer.normalize("text", "other\n"),
        normalizer.normalize("text", "same\n"),
    ]
    assert "".join(chunks) == "same\nother\nsame\n"
