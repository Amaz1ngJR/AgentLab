"""测试 stdin 独占协调 —— input_arbiter 的 pause/resume 语义。"""
from app.util.input_arbiter import (
    clear_background_reader,
    foreground_stdin,
    set_background_reader,
)


class _FakeReader:
    """记录 pause/resume 调用次数的假后台 reader。"""
    def __init__(self):
        self.paused = 0
        self.resumed = 0

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1


def test_foreground_stdin_pauses_and_resumes():
    """注册了 reader 时,foreground_stdin 进出各调一次 pause/resume。"""
    r = _FakeReader()
    set_background_reader(r)
    try:
        with foreground_stdin():
            assert r.paused == 1
            assert r.resumed == 0
        assert r.resumed == 1
    finally:
        clear_background_reader(r)


def test_foreground_stdin_resumes_on_exception():
    """前台代码抛异常时,reader 仍被恢复(finally 保证)。"""
    r = _FakeReader()
    set_background_reader(r)
    try:
        try:
            with foreground_stdin():
                raise ValueError("boom")
        except ValueError:
            pass
        assert r.resumed == 1
    finally:
        clear_background_reader(r)


def test_foreground_stdin_noop_without_reader():
    """没有注册 reader 时是空操作,不抛异常。"""
    # 确保干净状态
    r = _FakeReader()
    clear_background_reader(r)  # 清掉可能的残留(幂等)
    with foreground_stdin():
        pass  # 不应抛异常


def test_clear_only_removes_matching_reader():
    """clear 只在当前注册的就是该 reader 时才清,避免误清别人。"""
    r1 = _FakeReader()
    r2 = _FakeReader()
    set_background_reader(r1)
    clear_background_reader(r2)  # r2 不是当前的,不应清掉 r1
    with foreground_stdin():
        assert r1.paused == 1  # r1 仍生效
    clear_background_reader(r1)
