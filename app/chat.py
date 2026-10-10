"""一对一临时对话：两个直接交换位置的人之间，只能发系统预设的短语。

- 只放在当前进程的内存里，不写数据库、不写磁盘，服务重启即清空；
- 每条消息最多保留 TTL 秒，每个对话最多保留最近 N 条；池完成后整池的对话一起删除；
- 文本由 services 从预设短语里生成，这里不接受任何用户输入的明文；
- 「加我微信细说」是浏览器之间端到端加密的密文（cipher），服务器只负责转发，看不到内容。

因为在进程内存里，部署时只能开 1 个 worker（多个 worker 之间看不到彼此的消息）。
"""
import hashlib
import itertools
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime


def pair_id(pool_id: str, a: str, b: str) -> str:
    """两个人的对话 id：不暴露成员 id，别人也猜不到。"""
    return pool_id + "." + hashlib.sha256("|".join(sorted((a, b))).encode()).hexdigest()[:16]


class Chat:
    def __init__(self, ttl: float = 900, per_pair: int = 50, clock: Callable[[], float] = time.monotonic):
        self._msgs: dict[str, deque] = defaultdict(lambda: deque(maxlen=per_pair))
        self._pairs: dict[str, frozenset[str]] = {}  # 对话 → 两个人
        self._mine: dict[str, set[str]] = defaultdict(set)  # 人 → 他的对话
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._ttl = ttl
        self._clock = clock

    def open(self, pool_id: str, a: str, b: str) -> str:
        pid = pair_id(pool_id, a, b)
        with self._lock:
            self._pairs[pid] = frozenset((a, b))
            self._mine[a].add(pid)
            self._mine[b].add(pid)
        return pid

    def is_member(self, pid: str, member_id: str) -> bool:
        return member_id in self._pairs.get(pid, ())

    def post(self, pid: str, sender_id: str, sender: str, text: str | None = None,
             cipher: dict[str, str] | None = None) -> None:
        if not self.is_member(pid, sender_id):
            return
        msg = {"pair": pid, "from": sender, "at": datetime.now().strftime("%H:%M")}
        if cipher:
            msg["cipher"] = cipher
        else:
            msg["text"] = text
        with self._lock:
            self._msgs[pid].append((self._clock(), next(self._seq), sender_id, msg))

    def since(self, member_id: str, after: int = 0) -> list[dict]:
        """我所有一对一对话里 id 大于 after、且没过期的消息（按时间顺序）。"""
        cutoff = self._clock() - self._ttl
        with self._lock:
            rows = [row for pid in self._mine.get(member_id, ()) for row in self._msgs.get(pid, ())]
        return [{**m, "id": i, "mine": s == member_id}
                for ts, i, s, m in sorted(rows, key=lambda r: r[1]) if i > after and ts > cutoff]

    def close_pool(self, pool_id: str) -> None:
        """池完成后删除这个池的所有对话。"""
        with self._lock:
            for pid in [p for p in self._pairs if p.startswith(pool_id + ".")]:
                for member in self._pairs.pop(pid):
                    self._mine[member].discard(pid)
                self._msgs.pop(pid, None)

    def clear(self) -> None:
        with self._lock:
            self._msgs.clear()
            self._pairs.clear()
            self._mine.clear()
