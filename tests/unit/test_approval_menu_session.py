"""审批菜单必须在独立 AppSession 中运行。

回归背景:prompt_toolkit 的 ``set_app()`` 是直接改写"当前 AppSession"上的
``app`` 字段,不是 ContextVar 隔离。聊天 composer(主线程 loop)和审批菜单
(工作线程里新建的 loop)如果共用一个 AppSession,两边会互相覆盖:

  * 菜单按键产生的 ``KeyPressEvent`` 会把 ``get_app()`` 快照成 composer;
  * ``_digit``/``_enter`` 里的 ``event.app.exit()`` 于是退出的是 composer,
    菜单自己的 ``app.run()`` 永远不返回 —— 用户看到的就是"卡在选择框"。

下面三个用例覆盖:菜单使用独立会话、菜单不劫持 composer 的当前 app、以及在
composer 真正运行期间菜单仍能读到按键并返回。
"""
import threading
import time
from unittest.mock import patch

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import (
    create_app_session,
    get_app_or_none,
    get_app_session,
)
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from app.util import menu as menu_module


@pytest.fixture
def fake_tty(monkeypatch):
    """让 select_menu 走 prompt_toolkit 分支,而不是非 TTY 的 input() 回退。"""
    monkeypatch.setattr(menu_module.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(menu_module.sys.stdout, "isatty", lambda: True, raising=False)


def _press(pipe, keys, delay=0.3):
    def run():
        time.sleep(delay)
        pipe.send_text(keys)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _composer_like_app(pipe):
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    return Application(
        layout=Layout(Window(FormattedTextControl(lambda: "composer"))),
        input=pipe,
        output=DummyOutput(),
    )


def test_menu_runs_in_a_separate_app_session(fake_tty):
    """菜单运行期间持有独立 AppSession,退出后恢复调用方会话。"""
    from prompt_toolkit.application.current import set_app

    with create_pipe_input() as pipe:
        composer_app = _composer_like_app(pipe)
        seen = {}
        original = menu_module.Application

        class _Probe(original):
            def run(self, *args, **kwargs):
                # 采样点是 app.run() 内部,此时菜单应已切到自己的 AppSession。
                seen["app"] = self
                seen["session"] = get_app_session()
                return super().run(*args, **kwargs)

        typer = _press(pipe, "1")
        try:
            with create_app_session(input=pipe, output=DummyOutput()):
                caller_session = get_app_session()
                with set_app(composer_app), patch.object(menu_module, "Application", _Probe):
                    result = menu_module.select_menu(
                        [("接受", "yes"), ("拒绝", "no")],
                        header_lines=["● shell"],
                        title="是否允许执行?",
                    )
        finally:
            typer.join(2)

        assert result == "yes"
        # 菜单用的是新会话,而不是 composer 所在的会话。
        assert seen["session"] is not caller_session


def test_menu_does_not_steal_the_composer_app(fake_tty):
    """菜单退出后当前 app 仍是 composer,菜单自身不再处于运行态。"""
    from prompt_toolkit.application.current import set_app

    with create_pipe_input() as pipe:
        composer_app = _composer_like_app(pipe)
        holder = {}
        original = menu_module.Application

        class _Probe(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                holder["app"] = self

        typer = _press(pipe, "2")
        try:
            with create_app_session(input=pipe, output=DummyOutput()), \
                 set_app(composer_app), \
                 patch.object(menu_module, "Application", _Probe):
                result = menu_module.select_menu(
                    [("接受", "yes"), ("拒绝", "no")],
                    header_lines=["● shell"],
                    title="是否允许执行?",
                )
        finally:
            typer.join(2)

        assert result == "no"
        assert holder["app"] is not composer_app
        assert not holder["app"]._is_running


def test_menu_returns_while_a_composer_app_is_running(fake_tty):
    """composer 在自己的 loop 里运行时,菜单仍能读到按键并返回。

    这是用户实际遇到的形态:聊天进行中弹出审批菜单。未修复时菜单的
    KeyPressEvent 绑定到 composer,``event.app.exit()`` 退错对象,菜单永不返回。
    回归发生时菜单会一直阻塞,因此这里把菜单放进后台线程并限时等待 —— 失败时
    断言超时,而不是把测试套件挂死。
    """
    import asyncio

    with create_pipe_input() as pipe:
        composer = PromptSession(input=pipe, output=DummyOutput())

        def drive():
            try:
                asyncio.run(composer.prompt_async("> "))
            except BaseException:
                pass

        composer_thread = threading.Thread(target=drive, daemon=True)
        composer_thread.start()
        time.sleep(0.5)

        answer = {}

        def open_menu():
            with create_app_session(input=pipe, output=DummyOutput()):
                answer["result"] = menu_module.select_menu(
                    [("接受", "yes"), ("拒绝", "no")],
                    header_lines=["● shell"],
                    title="是否允许执行?",
                )

        menu_thread = threading.Thread(target=open_menu, daemon=True)
        typer = _press(pipe, "1", delay=0.5)
        try:
            menu_thread.start()
            menu_thread.join(5)
            assert not menu_thread.is_alive(), "审批菜单没有响应按键（按键退出到了别的 app）"
        finally:
            typer.join(2)
            pipe.send_text("\x04")
            composer_thread.join(2)

        assert answer["result"] == "yes"
