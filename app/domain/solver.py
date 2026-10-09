"""池内重组方案：给定池里各组的当前成员和小队，算出每个人的目标组。

池是封闭的：只在池内的组之间调人，不往池外的组塞人，池外的人也不受影响。

步骤
1. 装箱：多人小队按人数从多到少，首次适应装进 n 个容量为 5 的"新组"（n = 池里组数）。
   小队人数只有 2–5，这种情况下首次适应递减法就是最优的。装不下就说明这个池凑不出来。
2. 定组号：新组 → 现有组号做一一对应，让"本来就在这个组"的人尽量多（匈牙利算法）。
3. 单人：原组还有位置就留下；否则优先去"人数比原来少"的组（各组人数尽量保持原样；
   实在要有组变小，就让 5 人组变 4 人，而不是 4 人组变 3 人），
   其次去"有人从那里来到我原组"的组（这样能凑成两两互换）。
"""
from collections import Counter, defaultdict

from .teams import MAX_TEAM


class Infeasible(Exception):
    """池内凑不出满足所有小队的方案。"""


def solve(
    current: dict[str, str],
    groups: list[str],
    teams: list[tuple[str, ...]],
    capacity: int = MAX_TEAM,
) -> dict[str, str]:
    """current: 成员 → 当前组；groups: 池里的组号；teams: build_teams() 的结果。返回 成员 → 目标组。"""
    n = len(groups)
    if sum(1 for _ in current) > n * capacity:
        raise Infeasible("池里人数超过了这些组的总容量")

    multi = [t for t in teams if len(t) > 1]
    singles = [t[0] for t in teams if len(t) == 1]

    # 1. 装箱
    bins: list[list[str]] = [[] for _ in range(n)]
    for team in sorted(multi, key=lambda t: (-len(t), t)):
        slot = next((b for b in bins if len(b) + len(team) <= capacity), None)
        if slot is None:
            raise Infeasible(f"有 {len(team)} 人的小队放不下：需要更多组加入这个池，或者拆分小队")
        slot.extend(team)

    # 2. 定组号：得分 = 留下的人数（已在该组的队员 + 该组单人里还能留下的）× 100
    #    − 小队超出该组原人数的部分 × 1000（超出就意味着别的组要变小；宁可多移动一人，也不让组变小）
    singles_in = defaultdict(int)
    for m in singles:
        singles_in[current[m]] += 1
    quota = Counter(current.values())
    score = [
        [
            100 * (sum(current[m] == g for m in b) + min(capacity - len(b), singles_in[g]))
            - 1000 * max(0, len(b) - quota[g])
            for g in groups
        ]
        for b in bins
    ]
    assign = _max_assignment(score)
    target = {m: groups[assign[i]] for i, b in enumerate(bins) for m in b}

    # 3. 单人
    room = {groups[assign[i]]: capacity - len(b) for i, b in enumerate(bins)}
    overflow = []
    for m in sorted(singles):
        g = current[m]
        if room.get(g, 0) > 0:
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
