"""state.py — 进程内小状态：轮次级决策暂存与最近决策环形缓冲。

为什么需要：``pre_llm_call`` 做出判定，而 ``llm_request`` 中间件要在**同一个 turn**
的请求上按该判定裁剪工具面——两者之间只能通过 turn_id 传递。不做跨进程持久化：
进程重启后本轮早已结束，持久化没有意义，反而制造陈旧状态。

上限固定（LRU + 环形缓冲），不随会话增长；线程安全（hook 与中间件可能在不同线程）。
"""
from __future__ import annotations

import threading
from collections import OrderedDict, deque
from typing import Any, Deque, List, Optional

MAX_TURNS = 64
MAX_RECENT = 20


class TurnDecisions:
    """``turn_id -> Decision`` 的 LRU，外加最近决策环形缓冲（供 ``/intent why``）。"""

    def __init__(self, capacity: int = MAX_TURNS) -> None:
        self.capacity = max(1, int(capacity))
        self._by_turn: "OrderedDict[str, Any]" = OrderedDict()
        self._recent: Deque[Any] = deque(maxlen=MAX_RECENT)
        self._lock = threading.Lock()

    def put(self, turn_id: str, decision: Any) -> None:
        if not turn_id:
            return
        with self._lock:
            self._by_turn[turn_id] = decision
            self._by_turn.move_to_end(turn_id)
            while len(self._by_turn) > self.capacity:
                self._by_turn.popitem(last=False)
            self._recent.append(decision)

    def get(self, turn_id: str) -> Optional[Any]:
        if not turn_id:
            return None
        with self._lock:
            return self._by_turn.get(turn_id)

    def last(self) -> Optional[Any]:
        with self._lock:
            return self._recent[-1] if self._recent else None

    def recent(self, limit: int = MAX_RECENT) -> List[Any]:
        with self._lock:
            items = list(self._recent)
        return items[-max(1, limit):]
