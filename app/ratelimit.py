"""进程内限流：按 key 做滑动窗口计数。

不依赖数据库，开销只有一次字典查询；多个 worker 时每个进程各自计数（实际上限 ≈ 限额 × worker 数）。
"""
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()
        self._clock = clock
        self._max_keys = max_keys

    def _prune(self, key: str, window: float, now: float) -> deque:
        q = self._hits[key]
        while q and q[0] <= now - window:
            q.popleft()
        return q

    def allow(self, key: str, limit: int, window: float) -> bool:
        """窗口内次数未满就记一次并放行，否则拒绝（拒绝不计数）。"""
        now = self._clock()
        with self._lock:
            q = self._prune(key, window, now)
            if len(q) >= limit:
                return False
            q.append(now)
            self._gc(window, now)
            return True

    def count(self, key: str, window: float) -> int:
        with self._lock:
            return len(self._prune(key, window, self._clock()))

    def hit(self, key: str) -> None:
        with self._lock:
            self._hits[key].append(self._clock())

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def _gc(self, window: float, now: float) -> None:
        """key 太多时清掉已经过期的，防止内存无限增长。"""
        if len(self._hits) <= self._max_keys:
            return
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - window]:
            del self._hits[k]
