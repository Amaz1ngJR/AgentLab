"""CancelToken 回调机制的回归测试。

背景：取消原本只是"置一个标志位"，阻塞中的资源（模型流、shell 子进程）要等到
下一个安全检查点才能发现取消 —— 用户按 Esc 后界面像卡住一样。现在 token 支持
注册即时回调，Esc 置位时同步唤醒这些资源。

这里锁定四件事：
  1. cancel() 会同步触发已注册回调（不是等到下一个检查点）；
  2. 注册发生在取消之后时，回调在注册线程立即执行（检查/注册竞态）；
  3. 退订后不再触发，且退订是幂等的；
  4. 单个回调抛异常不影响其他回调，也不让 cancel() 失败。
"""
from __future__ import annotations

import threading

import pytest

from app.agent.cancel import CancelToken, Cancelled, close_on_cancel


# ── 即时触发 ──────────────────────────────────────────────────────────────────


def test_cancel_invokes_registered_callback_immediately():
    """cancel() 当场触发回调，不需要调用方再走检查点。"""
    token = CancelToken()
    fired = []
    token.add_callback(lambda: fired.append("closed"))

    assert fired == []  # 未取消前不触发
    token.cancel()
    assert fired == ["closed"]


def test_callback_registered_after_cancel_fires_at_registration():
    """竞态：注册发生在 cancel 之后，回调仍会执行（在注册线程里）。"""
    token = CancelToken()
    token.cancel()
    fired = []
    token.add_callback(lambda: fired.append("late"))
    assert fired == ["late"]


def test_cancel_is_idempotent_and_fires_callbacks_once():
    """cancel() 幂等：重复调用不会重复触发回调。"""
    token = CancelToken()
    fired = []
    token.add_callback(lambda: fired.append(1))

    token.cancel()
    token.cancel()
    token.cancel()
    assert fired == [1]


def test_multiple_callbacks_all_fire():
    token = CancelToken()
    fired = []
    for name in ("a", "b", "c"):
        token.add_callback(lambda n=name: fired.append(n))

    token.cancel()
    assert sorted(fired) == ["a", "b", "c"]


# ── 退订 ──────────────────────────────────────────────────────────────────────


def test_unsubscribe_prevents_callback():
    """阻塞操作正常结束后退订，取消时不应再碰这个已失效的资源。"""
    token = CancelToken()
    fired = []
    unsubscribe = token.add_callback(lambda: fired.append("x"))

    unsubscribe()
    token.cancel()
    assert fired == []


def test_unsubscribe_is_idempotent():
    token = CancelToken()
    unsubscribe = token.add_callback(lambda: None)
    unsubscribe()
    unsubscribe()  # 重复退订不应报错
    assert token.cancelled is False


def test_callback_context_manager_unsubscribes_on_exit():
    """with cancel.callback(...)：退出作用域后回调自动失效。"""
    token = CancelToken()
    fired = []

    with token.callback(lambda: fired.append("in")):
        pass

    token.cancel()
    assert fired == []


def test_callback_context_manager_fires_while_inside():
    token = CancelToken()
    fired = []
    with token.callback(lambda: fired.append("in")):
        token.cancel()
    assert fired == ["in"]


# ── 健壮性 ────────────────────────────────────────────────────────────────────


def test_one_failing_callback_does_not_block_others_or_cancel():
    """取消必须尽力通知所有资源：单个 close 抛异常不能阻断其余回调。"""
    token = CancelToken()
    fired = []

    def boom():
        raise RuntimeError("close failed")

    token.add_callback(boom)
    token.add_callback(lambda: fired.append("ok"))

    token.cancel()  # 不应抛出
    assert token.cancelled is True
    assert fired == ["ok"]


def test_failing_callback_at_registration_after_cancel_is_swallowed():
    token = CancelToken()
    token.cancel()

    def boom():
        raise RuntimeError("already closed")

    token.add_callback(boom)  # 不应抛出


# ── 与检查点语义的配合 ────────────────────────────────────────────────────────


def test_raise_if_cancelled_after_callback():
    token = CancelToken()
    token.add_callback(lambda: None)
    token.cancel()
    with pytest.raises(Cancelled):
        token.raise_if_cancelled()


def test_callbacks_run_before_cancel_returns():
    """关键时序：回调必须在 cancel() 返回前跑完。

    CLI 的 Ctrl-C/Esc 处理依赖这个保证 —— cancel() 返回就意味着阻塞资源已被
    唤醒，上层可以安全地继续收尾。
    """
    token = CancelToken()
    order = []

    def callback():
        order.append("callback")

    token.add_callback(callback)
    token.cancel()
    order.append("after_cancel")
    assert order == ["callback", "after_cancel"]


# ── close_on_cancel ───────────────────────────────────────────────────────────


def test_close_on_cancel_closes_resource():
    token = CancelToken()

    class Stream:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    stream = Stream()
    with close_on_cancel(token, stream):
        token.cancel()
    assert stream.closed == 1


def test_close_on_cancel_without_token_is_noop():
    """cancel=None（非 Agent 调用）时退化为普通上下文，不得报错。"""
    class Stream:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    stream = Stream()
    with close_on_cancel(None, stream):
        pass
    assert stream.closed == 0


def test_close_on_cancel_ignores_resource_without_close():
    token = CancelToken()
    with close_on_cancel(token, object()):
        token.cancel()  # 没有 close 方法，不应报错


def test_close_on_cancel_unsubscribes_after_exit():
    """退出上下文后资源已正常关闭，取消不应再 close 第二次。"""
    token = CancelToken()

    class Stream:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    stream = Stream()
    with close_on_cancel(token, stream):
        pass
    token.cancel()
    assert stream.closed == 0


# ── 并发安全 ──────────────────────────────────────────────────────────────────


def test_concurrent_cancel_fires_callback_exactly_once():
    """多线程同时 cancel()，回调只能触发一次（锁保护的标志位）。"""
    token = CancelToken()
    fired = []
    token.add_callback(lambda: fired.append(1))

    threads = [threading.Thread(target=token.cancel) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert fired == [1]
    assert token.cancelled is True
