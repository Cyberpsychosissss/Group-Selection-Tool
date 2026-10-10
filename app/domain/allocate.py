"""全站统一分配：把所有等待中的意向组放在一起，找"一次让最多人如愿、移动最少"的方案。

视角是全局的，不是某一个意向组的：
- 不是"替某个组腾位置"——不参加本工具的人、还有这次没被选中的意向组，谁都不动；只在空位和参与者之间调。
- 几个意向组互相帮忙（你来我的组、我去你的组）在同一份方案里一起完成，所以相关的组都满员时也可能一起成功。
- 目标：① 如愿（整队同组）的人数最多；② 其次移动的人最少。

实现
- 枚举意向组的子集，按"总人数从多到少"排序；每个子集交给 solver.solve 算最少移动的方案（精确动态规划），
  没被选中的意向组当作固定不动的人。第一个有可行解的人数档次里取移动最少的。
- 没有成员在里面、但空位够最小意向组整队搬入的组也作为候选；它们对求解器来说只有"空位多少"的差别，
  最多带 MAX_FOREIGN 个（空位最多的优先）。
- 意向组很多时按时间预算截断，退回"按人数从多到少逐个试加"的贪心。
"""
import itertools
import time
from collections import Counter
from typing import NamedTuple

from . import solver

MAX_FOREIGN = 6        # 候选的"外来组"上限
MAX_ENUM_TEAMS = 10    # 意向组超过这个数就只做贪心
TIME_BUDGET = 1.5      # 秒


class Allocation(NamedTuple):
    teams: tuple[str, ...]      # 选中的意向组
    target: dict[str, str]      # 成员 → 目标组
    moves: int
    groups: tuple[str, ...]     # 方案涉及的组（成员所在组 + 目标组）


def allocate(team_members: dict[str, tuple[str, ...]], current: dict[str, str], counts: dict[str, int],
             capacity: int = 5, budget: float = TIME_BUDGET) -> Allocation | None:
    """team_members: 意向组 → 成员；current: 成员 → 此刻所在组；counts: 组 → LM Core 上的人数（含不参加的人）。
    返回最优的一轮；谁都放不下返回 None。"""
    tids = sorted(team_members, key=lambda t: (-len(team_members[t]), t))
    if not tids:
        return None
    people = {t: len(team_members[t]) for t in tids}
    home = sorted({current[m] for t in tids for m in team_members[t]})
    smallest = min(people.values())
    foreign = sorted((g for g, n in counts.items() if g not in home and n + smallest <= capacity),
                     key=lambda g: (counts[g], g))[:MAX_FOREIGN]
    groups = home + foreign
    in_grp = Counter(current[m] for t in tids for m in team_members[t])
    # 组里不参加的人：填报 / 爬虫的人数减去我们知道的意向成员；数据对不上时按组满处理，宁可少动
    outsiders = {g: max(0, min(counts.get(g, capacity), capacity) - in_grp[g]) for g in groups}

    def attempt(subset: tuple[str, ...]) -> Allocation | None:
        chosen = set(subset)
        fixed = Counter(outsiders)
        for t in tids:
            if t not in chosen:
                for m in team_members[t]:
                    fixed[current[m]] += 1
        cur = {m: current[m] for t in subset for m in team_members[t]}
        try:
            target = solver.solve(cur, groups, [team_members[t] for t in subset], capacity, fixed)
        except solver.Infeasible:
            return None
        moves = sum(cur[m] != target[m] for m in cur)
        used = tuple(sorted(set(cur.values()) | set(target.values())))
        return Allocation(tuple(subset), target, moves, used)

    deadline = time.monotonic() + budget
    best: Allocation | None = None
    if len(tids) <= MAX_ENUM_TEAMS:
        subsets = [c for k in range(len(tids), 0, -1) for c in itertools.combinations(tids, k)]
        subsets.sort(key=lambda c: (-sum(people[t] for t in c), -len(c), c))
        for c in subsets:
            n = sum(people[t] for t in c)
            if best and n < sum(people[t] for t in best.teams):
                break  # 人数更多的档次已经有解了
            if time.monotonic() > deadline:
                break
            a = attempt(c)
            if a and (best is None or a.moves < best.moves):
                best = a
        if best:
            return best
    chosen: list[str] = []  # 贪心：人多的先试，放得下就留下
    for t in tids:
        a = attempt(tuple(chosen + [t]))
        if a:
            chosen.append(t)
            best = a
    return best
