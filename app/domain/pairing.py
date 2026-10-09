"""把方案里的移动整理成"谁接替谁"，让每个人知道自己的交换搭档。

在每个组里，离开的人和进来的人一一配对：进来的人"接替"离开的人的位置。
优先配成互换：x: A→B 且 y: B→A 时，y 接替 x 在 A 的位置，x 接替 y 在 B 的位置，
这和官方邮件里 X、Y 互换的例子完全一样。凑不成互换的，就是几个人的接力（A→B→C→A）。
"""
from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class Link:
    takes_seat_of: str | None  # 我在目标组接替谁；None = 占用一个空位
    replaced_by: str | None  # 谁来原组接替我；None = 我的位置空出来

    @property
    def partner(self) -> str | None:
        """互换搭档：我接替的人正好也来接替我。"""
        return self.takes_seat_of if self.takes_seat_of and self.takes_seat_of == self.replaced_by else None


def link_moves(moves: dict[str, tuple[str, str]]) -> dict[str, Link]:
    """moves: 成员 → (原组, 目标组)，只包含需要移动的人。"""
    leaving: dict[str, list[str]] = defaultdict(list)
    coming: dict[str, list[str]] = defaultdict(list)
    for m, (src, dst) in sorted(moves.items()):
        leaving[src].append(m)
        coming[dst].append(m)

    takes: dict[str, str] = {}  # m → m 接替的人
    replaced_by: dict[str, str] = {}  # m → 接替 m 的人

    def link(newcomer: str, old: str) -> None:
        takes[newcomer] = old
        replaced_by[old] = newcomer

    # 第一轮：凑互换
    for x in sorted(moves):
        if x in takes or x in replaced_by:
            continue
        src, dst = moves[x]
        y = next((y for y in coming[src] if moves[y][0] == dst and y not in takes and y not in replaced_by), None)
        if y:
            link(y, x)
            link(x, y)

    # 第二轮：剩下的在每个组里按顺序接替
    for g in sorted(set(leaving) | set(coming)):
        olds = [x for x in leaving[g] if x not in replaced_by]
        news = [y for y in coming[g] if y not in takes]
        for old, new in zip(olds, news):
            link(new, old)

    return {m: Link(takes.get(m), replaced_by.get(m)) for m in moves}


@dataclass(frozen=True)
class Swap:
    """一次交换：a 从 ga 去 gb；b 从 gb 去 ga（b 为 None 表示 a 单独进空位）。after：必须先完成的交换。"""
    a: str
    b: str | None
    ga: str
    gb: str
    after: tuple[int, ...] = ()

    @property
    def members(self) -> tuple[str, ...]:
        return (self.a,) if self.b is None else (self.a, self.b)

    def route(self, m: str) -> tuple[str, str]:
        """m 在这次交换里的 (出发组, 到达组)。"""
        return (self.ga, self.gb) if m == self.a else (self.gb, self.ga)

    def partner(self, m: str) -> str | None:
        return self.b if m == self.a else self.a if m == self.b else None


def build_swaps(
    moves: dict[str, tuple[str, str]],
    in_team: frozenset[str] | set[str] = frozenset(),
    free: frozenset[str] | set[str] = frozenset(),
) -> list[Swap]:
    """把方案拆成"两人一组"的交换，保证任何时刻每个人最多只有一个交换对象。

    free：整个执行过程中始终有空位的组（原人数和最终人数都小于 5）。环只能借用这些组的空位：
          最终会被填满的空位是留给单独进空位的人的，借用它会互相等待而卡死。
          用方案生成时的人数计算，执行过程中保持不变，拆分结果才稳定。

    1. 能直接互换的两人：一次交换。
    2. 接力链（第一个人进空位，后面的人依次接替前一个人的位置）：按顺序逐个单独移动。
    3. 三人以上的环 x1→x2→…→xk：
       - 环里有人的目标组有空位：从他开始依次单独移动，每人只动一次；
       - 环里的组全满：拆成依次进行的 k−1 次两两互换，都经过 x1 的原组，中间的人会移动两次；
         选择让多人小队成员尽量不移动两次的起点。
    """
    links = link_moves(moves)
    swaps: list[Swap] = []
    seen: set[str] = set()

    for x in sorted(moves):
        p = links[x].partner
        if p and x not in seen:
            seen |= {x, p}
            swaps.append(Swap(x, p, *moves[x]))

    for head in sorted(moves):
        if head in seen or links[head].takes_seat_of is not None:
            continue
        prev, x = None, head
        while x is not None and x not in seen:
            seen.add(x)
            swaps.append(Swap(x, None, *moves[x], after=() if prev is None else (prev,)))
            prev, x = len(swaps) - 1, links[x].replaced_by

    for start in sorted(moves):
        if start in seen:
            continue
        cyc = [start]
        while (nxt := links[cyc[-1]].takes_seat_of) not in (start, None):
            cyc.append(nxt)
        seen |= set(cyc)
        entry = next((x for x in sorted(cyc) if moves[x][1] in free), None)
        if entry is not None:  # 有空位：从进空位的人开始，后一个人接替前一个人腾出的位置
            prev, x = None, entry
            for _ in cyc:
                swaps.append(Swap(x, None, *moves[x], after=() if prev is None else (prev,)))
                prev, x = len(swaps) - 1, links[x].replaced_by
            continue
        k = len(cyc)
        best = min(range(k), key=lambda r: sum((cyc[r:] + cyc[:r])[i] in in_team for i in range(1, k - 1)))
        cyc = cyc[best:] + cyc[:best]
        hub, prev = moves[cyc[0]][0], None
        for j in range(k - 1):
            swaps.append(Swap(cyc[j], cyc[j + 1], hub, moves[cyc[j + 1]][0], after=() if prev is None else (prev,)))
            prev = len(swaps) - 1
    return swaps

