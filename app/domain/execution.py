"""执行阶段：按"两人一组"的交换（pairing.build_swaps）一组一组推进，算出此刻谁该在 LM Core 上做什么。

三种动作（都对应 LM Core Grouping 页面的一次 Save my choice）：
  move  —— 同一次保存里：取消勾选原组 + 勾选目标组（目标组有空位时）
  leave —— 只取消勾选原组（两个组都满时，由一人先腾出位置，暂时无组）
  join  —— 只勾选目标组（之前 leave 过的人）

规则
- 一次交换依赖的交换都完成后才开始；同一个组同一时间只参与一次进行中的交换，互不抢位。
- 两人交换：谁的目标组有空位谁先 move；都满时一人先 leave（优先多人小队成员），
  另一人 move 进腾出的位置，最后 leave 的人 join。
- 进度用"每人已完成的移动次数"记录：环里中间的人会参与两次交换。
- 池是封闭的，所以组人数 = 池内此刻在该组的人数。
"""
from collections import Counter
from collections.abc import Iterable

from .pairing import Swap
from .teams import MAX_TEAM

Cur = dict[str, str | None]  # 成员 → 此刻所在组（None = 暂时无组）
Done = dict[str, int]  # 成员 → 已完成的移动次数


def member_swaps(swaps: list[Swap]) -> dict[str, list[int]]:
    """每个人依次参与的交换编号（大多数人 1 次，环里中间的人 2 次）。"""
    order: dict[str, list[int]] = {}
    for i, s in enumerate(swaps):
        for m in s.members:
            order.setdefault(m, []).append(i)
    return order


def current_swap(swaps: list[Swap], done: Done, m: str) -> int | None:
    """m 正在进行（或下一次）的交换；全部完成返回 None。"""
    mine = member_swaps(swaps).get(m, [])
    k = done.get(m, 0)
    return mine[k] if k < len(mine) else None


def _moved(m: str, i: int, order: dict[str, list[int]], done: Done) -> bool:
    return done.get(m, 0) > order[m].index(i)


def swap_done(i: int, swaps: list[Swap], done: Done, order: dict[str, list[int]] | None = None) -> bool:
    order = order or member_swaps(swaps)
    return all(_moved(m, i, order, done) for m in swaps[i].members)


def is_done(swaps: list[Swap], done: Done) -> bool:
    order = member_swaps(swaps)
    return all(swap_done(i, swaps, done, order) for i in range(len(swaps)))


def next_actions(
    swaps: list[Swap], cur: Cur, done: Done, in_team: Iterable[str] = (), capacity: int = MAX_TEAM
) -> dict[str, tuple[str, int]]:
    """返回 成员 → (动作, 交换编号)。没有出现在结果里的人此刻只需等待。"""
    order, in_team = member_swaps(swaps), set(in_team)
    finished = [swap_done(i, swaps, done, order) for i in range(len(swaps))]
    cnt = Counter(g for g in cur.values() if g)

    def started(i: int) -> bool:
        return any(cur[m] is None or _moved(m, i, order, done) for m in swaps[i].members)

    ready = [i for i, s in enumerate(swaps) if not finished[i] and all(finished[d] for d in s.after)]
    ready.sort(key=lambda i: (not started(i), i))  # 进行到一半的交换优先占用它的两个组
    locked: set[str] = set()
    actions: dict[str, tuple[str, int]] = {}
    for i in ready:
        s = swaps[i]
        if {s.ga, s.gb} & locked:
            continue
        step = _step(s, i, cur, done, order, cnt, in_team, capacity)
        if step or started(i):  # 还没开始、此刻也动不了的交换不占锁，免得挡住能腾出空位的人
            locked |= {s.ga, s.gb}
        if step:
            actions[step[0]] = (step[1], i)
    return actions


def _step(s: Swap, i: int, cur: Cur, done: Done, order, cnt: Counter, in_team: set, cap: int):
    pending = [m for m in s.members if not _moved(m, i, order, done)]
    if len(pending) == 1:
        m = pending[0]
        if cnt[s.route(m)[1]] < cap:
            return m, "join" if cur[m] is None else "move"
        return None
    homeless = [m for m in pending if cur[m] is None]
    if homeless:  # 有人已退出：另一人先换进他腾出的位置
        other = s.partner(homeless[0])
        return (other, "move") if cnt[s.route(other)[1]] < cap else None
    for m in pending:  # 谁的目标组有空位谁先换
        if cnt[s.route(m)[1]] < cap:
            return m, "move"
    leaver = min(pending, key=lambda m: (m not in in_team, m))
    return leaver, "leave"


def apply(swaps: list[Swap], cur: Cur, done: Done, m: str, action: str, i: int) -> tuple[Cur, Done]:
    """执行一个动作，返回新状态（不修改原状态）。"""
    cur, done = dict(cur), dict(done)
    if action == "leave":
        cur[m] = None
    else:
        cur[m] = swaps[i].route(m)[1]
        done[m] = done.get(m, 0) + 1
    return cur, done
