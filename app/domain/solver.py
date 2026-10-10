"""池内重组方案：给定池里各组的当前成员和小队，算出每个人的目标组。

池是封闭的：只在池内的组之间调人，不往池外的组塞人，池外的人也不受影响。

目标（按优先级）
1. 每个小队的人同组，每个组不超过容量；
2. 移动的人最少；
3. 移动人数相同时，尽量不让任何组比原来小（各组人数尽量保持原样）。

算法
- 小队 → 组：按"各组已占人数"做动态规划（状态 = 各组已占人数的元组），精确求出移动最少的分法。
  组很多、状态数超过上限时退回到"首次适应装箱 + 匈牙利算法"的近似解。
- 单人：原组还有位置就留下；否则优先去"人数比原来少"的组，其次去"有人从那里来到我原组"的组
  （这样能凑成两两互换），最后原来人少的组优先补。
- fixed：各组里不参与重组的占位人数（LM Core 上在组里、但没加入本工具的人），他们占着位置不动。
"""
from collections import Counter, defaultdict

from .teams import MAX_TEAM

MAX_STATES = 30_000  # 动态规划的状态数上限，超过就退回近似解。8 个组以内总是精确解；树莓派上最坏约 0.8 秒（9–10 个组）


class Infeasible(Exception):
    """池内凑不出满足所有小队的方案。"""


def solve(
    current: dict[str, str],
    groups: list[str],
    teams: list[tuple[str, ...]],
    capacity: int = MAX_TEAM,
    fixed: dict[str, int] | None = None,
) -> dict[str, str]:
    """current: 成员 → 当前组；groups: 池里的组号；teams: build_teams() 的结果；
    fixed: 组 → 占位人数（不动、占容量）。返回 成员 → 目标组。"""
    fixed = dict(fixed or {})
    cap = {g: capacity - fixed.get(g, 0) for g in groups}
    if any(c < 0 for c in cap.values()):
        raise Infeasible("占位人数超过了组的容量")
    if len(current) > sum(cap.values()):
        raise Infeasible("池里人数超过了这些组的总容量")

    multi = sorted((t for t in teams if len(t) > 1), key=lambda t: (-len(t), t))
    singles = sorted(t[0] for t in teams if len(t) == 1)
    singles_in = Counter(current[m] for m in singles)
    quota = Counter(current.values())  # 各组原人数（不含占位；占位在前后都在，比较时抵消）

    assign = _assign_exact(multi, current, groups, cap, singles_in, quota)
    if assign is None:
        assign = _assign_heuristic(multi, current, groups, cap, singles_in, quota)
    target = {m: groups[assign[i]] for i, team in enumerate(multi) for m in team}

    # 单人
    load = Counter(target.values())
    room = {g: cap[g] - load[g] for g in groups}
    overflow = []
    for m in singles:
        g = current[m]
        if room[g] > 0:
            target[m] = g
            room[g] -= 1
        else:
            overflow.append(m)
    assigned = Counter(target.values())
    flow = Counter((current[m], target[m]) for m in target if current[m] != target[m])
    for m in overflow:
        src = current[m]
        g = max(
            (x for x in groups if room[x] > 0),
            # 先补回人数变少的组，再优先凑成互换，最后原来人少的组优先补
            key=lambda x: (quota[x] > assigned[x], flow[(x, src)] > flow[(src, x)], -quota[x], -groups.index(x)),
        )
        target[m] = g
        room[g] -= 1
        assigned[g] += 1
        flow[(src, g)] += 1
    return target


def _assign_exact(multi, current, groups, cap, singles_in, quota, max_states: int | None = None):
    """动态规划：状态 = 各组已被小队占用的人数；代价 = 小队里要移动的人数。
    最后加上单人里留不下的人数（原组被小队占满的单人必须移动），取总移动最少；
    并列时取"小队超出原组人数"最少的（超出意味着别的组要变小）。返回 小队序号 → 组序号。"""
    n = len(groups)
    max_states = MAX_STATES if max_states is None else max_states
    dp: dict[tuple[int, ...], tuple[int, tuple[int, ...]]] = {tuple([0] * n): (0, ())}
    for team in multi:
        k = len(team)
        nxt: dict[tuple[int, ...], tuple[int, tuple[int, ...]]] = {}
        for state, (cost, asg) in dp.items():
            for j, g in enumerate(groups):
                if state[j] + k > cap[g]:
                    continue
                s2 = state[:j] + (state[j] + k,) + state[j + 1:]
                val = (cost + sum(current[m] != g for m in team), asg + (j,))
                if s2 not in nxt or val < nxt[s2]:
                    nxt[s2] = val
        dp = nxt
        if not dp:
            raise Infeasible(f"有 {k} 人的小队放不下：需要更多组加入这个池，或者拆分小队")
        if len(dp) > max_states:
            return None
    best = None
    for state, (cost, asg) in dp.items():
        single_moves = sum(max(0, singles_in[g] - (cap[g] - state[j])) for j, g in enumerate(groups))
        excess = sum(max(0, state[j] - quota[g]) for j, g in enumerate(groups))
        key = (cost + single_moves, excess, asg)
        if best is None or key < best:
            best = key
    return list(best[2])


def _assign_heuristic(multi, current, groups, cap, singles_in, quota):
    """近似解：小队按人数从多到少首次适应装进 n 个箱子，再用匈牙利算法把箱子对应到组，
    让"本来就在这个组"的人尽量多；"超出原组人数"只作为很弱的次要项。返回 小队序号 → 组序号。"""
    n = len(groups)
    cap_max = max(cap.values())
    bins: list[list[str]] = [[] for _ in range(n)]
    team_bin: list[int] = []
    for team in multi:
        slot = next((i for i, b in enumerate(bins) if len(b) + len(team) <= cap_max), None)
        if slot is None:
            raise Infeasible(f"有 {len(team)} 人的小队放不下：需要更多组加入这个池，或者拆分小队")
        bins[slot].extend(team)
        team_bin.append(slot)
    score = [
        [
            (100 * (sum(current[m] == g for m in b) + min(cap[g] - len(b), singles_in[g]))
             - max(0, len(b) - quota[g])) if len(b) <= cap[g] else -10**9
            for g in groups
        ]
        for b in bins
    ]
    bin_group = _max_assignment(score)
    if any(len(b) > cap[groups[bin_group[i]]] for i, b in enumerate(bins)):
        raise Infeasible("小队放不下：需要更多组加入这个池，或者拆分小队")
    return [bin_group[b] for b in team_bin]


def _max_assignment(score: list[list[int]]) -> list[int]:
    """方阵最大权匹配（匈牙利算法，O(n³)）。返回 行 → 列。"""
    n = len(score)
    inf = float("inf")
    u, v, p, way = [0.0] * (n + 1), [0.0] * (n + 1), [0] * (n + 1), [0] * (n + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [inf] * (n + 1), [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = -score[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    result = [0] * n
    for j in range(1, n + 1):
        if p[j]:
            result[p[j] - 1] = j - 1
    return result
