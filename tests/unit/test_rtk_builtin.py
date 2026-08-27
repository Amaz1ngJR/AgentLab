"""内置 RTK 压缩引擎测试。"""
import threading

import pytest

from app.tools.rtk_builtin import BuiltinRTK, BuiltinRTKConfig, format_status


@pytest.fixture(autouse=True)
def reset_stats():
    """每个测试前重置全局统计，避免测试间干扰。"""
    BuiltinRTK.reset_stats()
    yield
    BuiltinRTK.reset_stats()


def test_compression_stats_accumulate_across_instances():
    """统计是全局的，多个 BuiltinRTK 实例共享同一个计数器。"""
    rtk1 = BuiltinRTK()
    rtk2 = BuiltinRTK()

    rtk1.compress("git status", "On branch main\nnothing to commit", "", 0)
    rtk2.compress("echo hello", "hello\n", "", 0)

    stats = BuiltinRTK.get_stats()
    assert stats.total_calls == 2


def test_stats_count_applied_vs_fallback():
    """applied_count 只计实际压缩的，回退原文的不算。"""
    rtk = BuiltinRTK()

    # git status 会被压缩
    rtk.compress("git status", "On branch main\nnothing to commit, working tree clean", "", 0)
    # 未识别命令且收益不足，会回退
    rtk.compress("unknown_cmd", "hello", "", 0)

    stats = BuiltinRTK.get_stats()
    assert stats.total_calls == 2
    assert stats.applied_count == 1


def test_stats_bytes_are_cumulative():
    """字节数累加正确，节省百分比按总量计算。"""
    rtk = BuiltinRTK()

    # 模拟一个能压缩的命令
    result1 = rtk.compress("git status", "On branch main\n" + "M file.py\n" * 10, "", 0)
    # 模拟一个回退的命令
    result2 = rtk.compress("echo test", "test\n", "", 0)

    stats = BuiltinRTK.get_stats()
    assert stats.total_original_bytes == result1.original_bytes + result2.original_bytes
    assert stats.total_output_bytes == result1.output_bytes + result2.output_bytes
    assert stats.total_saved_bytes == (result1.original_bytes - result1.output_bytes) + (result2.original_bytes - result2.output_bytes)


def test_stats_thread_safe():
    """多线程并发压缩时统计不丢失。"""
    rtk = BuiltinRTK()

    def worker():
        for _ in range(10):
            rtk.compress("git status", "On branch main", "", 0)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stats = BuiltinRTK.get_stats()
    assert stats.total_calls == 40


def test_reset_stats_clears_all_counters():
    """reset_stats 清空所有累计值。"""
    rtk = BuiltinRTK()
    rtk.compress("git status", "On branch main", "", 0)

    BuiltinRTK.reset_stats()
    stats = BuiltinRTK.get_stats()

    assert stats.total_calls == 0
    assert stats.applied_count == 0
    assert stats.total_original_bytes == 0
    assert stats.total_output_bytes == 0


def test_format_status_shows_stats_when_available():
    """format_status 在有统计数据时显示会话统计。"""
    rtk = BuiltinRTK()

    # 没有调用前，不显示统计
    status = format_status()
    assert "Session statistics:" not in status

    # 调用后，显示统计
    rtk.compress("git status", "On branch main\nnothing to commit", "", 0)
    status = format_status()
    assert "Session statistics:" in status
    assert "total commands: 1" in status
    assert "saved:" in status


def test_average_savings_percent_with_zero_original():
    """原始字节为 0 时平均节省百分比应为 0。"""
    stats = BuiltinRTK.get_stats()
    assert stats.average_savings_percent == 0.0


def test_disabled_rtk_still_records_stats():
    """RTK 禁用时仍记录统计（虽然 applied=False）。"""
    rtk = BuiltinRTK(BuiltinRTKConfig(enabled=False))
    rtk.compress("git status", "On branch main", "", 0)

    stats = BuiltinRTK.get_stats()
    assert stats.total_calls == 1
    assert stats.applied_count == 0
