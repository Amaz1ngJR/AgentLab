"""订阅者集合 —— 统一 add/remove 与安全迭代。

为什么单独抽出来：
  `subscribe()` 返回幂等的 unsubscribe 闭包这个模式，在 service / approval_broker /
  cancel / runtime 里各写了一份，其中四份是 `if cb in list: list.remove(cb)`。
  除了重复本身，它还带来两个隐患：

  1. 线性扫描：每次退订都是 O(n)。
  2. 迭代期变更：`for cb in self._subscribers: cb(...)` 时若回调整体退订，
     会直接踩到 "list changed size during iteration"。

  这里用 dict 保序存（Python 3.7+ 字典有序），按 id 定位 O(1) 退订，
  并用 `snapshot()` 返回副本供广播，把「迭代」和「变更」彻底解耦。

语义对调用方不变：重复 add 同一 callback 仍会被去重，退订仍可重复调用。
"""
from __future__ import annotations

import threading
from typing import Callable, Generic, Iterator, TypeVar

T = TypeVar("T")


class SubscriberList(Generic[T]):
    """线程安全的订阅者容器，可重复退订、迭代时快照。"""

    __slots__ = ("_lock", "_items")

    def __init__(self, lock: threading.RLock | None = None) -> None:
        # 允许外部传入与宿主对象共享的锁，便于把「注册订阅者」和宿主状态变更
        # 放进同一临界区（service 的 _ensure_open 检查需要这样）。
        self._lock = lock if lock is not None else threading.RLock()
        # key 用 id(callback)：callback 一般是不可哈希的闭包或方法，id 足够唯一，
        # 且在持有引用期间不会复用。value 存原对象，保证快照仍可调用。
        self._items: dict[int, Callable[[T], None]] = {}

    def add(self, callback: Callable[[T], None]) -> Callable[[], None]:
        """注册回调并返回幂等的退订函数。"""
        key = id(callback)
        with self._lock:
            self._items[key] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._items.pop(key, None)

        return unsubscribe

    def remove(self, callback: Callable[[T], None]) -> None:
        """显式退订（等价于调用 add 返回的函数）。"""
        with self._lock:
            self._items.pop(id(callback), None)

    def snapshot(self) -> list[Callable[[T], None]]:
        """返回当前订阅者副本。广播一律迭代副本，避免回调内退订触发 RuntimeError。"""
        with self._lock:
            return list(self._items.values())

    def clear(self) -> list[Callable[[T], None]]:
        """清空并返回被清掉的订阅者，供调用方在锁外逐个收尾。"""
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
            return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __iter__(self) -> Iterator[Callable[[T], None]]:
        return iter(self.snapshot())
