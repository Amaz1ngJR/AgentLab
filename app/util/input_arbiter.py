"""协调对 stdin 的独占访问。

问题背景:
  聊天执行期间,cli._EscWatcher 有一个后台线程持续 os.read(stdin) 监听 Esc 键
  (用于中断当前回复)。但审批工具调用时弹出的方向键菜单(util.menu)也用
  prompt_toolkit 读同一个 stdin。两个 reader 同时读,用户按键的字节会被随机
  分给其中一个 —— 菜单经常收不到按键,导致"选 1 / Enter 要按很多次才生效"。

解决办法:
  提供一个全局协调点。前台需要独占 stdin(如运行 prompt_toolkit 菜单)时,
  用 foreground_stdin() 临时暂停后台 reader;退出后自动恢复。后台 reader
  (_EscWatcher)在 __enter__ 时把自己注册进来,__exit__ 时注销。

  只有一个 reader 会注册(REPL 同时只跑一轮 chat),所以用单个模块级引用即可。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Optional, Protocol


class _PausableReader(Protocol):
    def pause(self) -> None: ...
    def resume(self) -> None: ...


_lock = threading.Lock()
_reader: Optional[_PausableReader] = None


def set_background_reader(obj: _PausableReader) -> None:
    """注册当前后台 stdin reader(需实现 pause()/resume())。"""
    global _reader
    with _lock:
        _reader = obj


def clear_background_reader(obj: _PausableReader) -> None:
    """注销后台 reader(仅当注册的就是它,避免误清别人)。"""
    global _reader
    with _lock:
        if _reader is obj:
            _reader = None


@contextmanager
def foreground_stdin():
    """前台独占 stdin 期间暂停后台 reader;无 reader 时为空操作。

    用法:
        with foreground_stdin():
            app.run()   # prompt_toolkit 菜单独占 stdin,Esc 监听线程不抢字节
    """
    with _lock:
        obj = _reader
    if obj is None:
        yield
        return
    obj.pause()
    try:
        yield
    finally:
        obj.resume()
