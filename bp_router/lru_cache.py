"""Minimal bounded LRU dict used for hot-path lookup caches.

In-house rather than adding a `cachetools` dependency — the
interface needed is small (`__getitem__` / `__setitem__` /
`__contains__` / `get` / `pop`) and the semantics are simple
LRU eviction on overflow.

Used by `state.caller_agent_cache` (R8) and reusable for any
future router-side cache that wants bounded RSS without TTL
semantics. For TTL behaviour see `LlmService._user_level_cache`
in `bp_router/llm/service.py`.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterator


class BoundedLRUDict:
    """OrderedDict-backed dict with LRU eviction past `maxsize`.

    Reads via `__getitem__` / `get` count as "touch" and move
    the entry to most-recent. Writes via `__setitem__` insert at
    most-recent. When inserting past the cap, the OLDEST entry
    (least-recently-touched) is popped.

    Not thread-safe — the router runs single-threaded asyncio,
    so the cooperative scheduling guarantees the get-then-touch
    pair runs atomically between awaits.
    """

    __slots__ = ("_maxsize", "_data")

    def __init__(self, *, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._maxsize = maxsize
        self._data: OrderedDict[Any, Any] = OrderedDict()

    def __getitem__(self, key: Any) -> Any:
        value = self._data[key]
        # Touch — move to most-recent end.
        self._data.move_to_end(key)
        return value

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self._data:
            self._data.move_to_end(key)
            self._data[key] = value
            return
        self._data[key] = value
        if len(self._data) > self._maxsize:
            # popitem(last=False) → FIFO order (oldest first), which
            # after our `move_to_end` discipline IS least-recently-touched.
            self._data.popitem(last=False)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def pop(self, key: Any, default: Any = None) -> Any:
        return self._data.pop(key, default)

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._data)

    def clear(self) -> None:
        self._data.clear()
