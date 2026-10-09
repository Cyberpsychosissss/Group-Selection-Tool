"""互选关系 → 小队。

两个人互相勾选"想同组"才算一条有效关系；有效关系连通的人组成一个小队
（A↔B、B↔C 则 A、B、C 同队）。没有互选的人自成一队（单人），重组时可灵活安排。
"""
from collections import defaultdict
from collections.abc import Iterable

MAX_TEAM = 5


def mutual_pairs(wishes: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
    """只保留双向互选的关系，每对返回一次（小 id 在前）。"""
    s = set(wishes)
    return {(a, b) for a, b in s if a < b and (b, a) in s}


def build_teams(member_ids: Iterable[str], wishes: Iterable[tuple[str, str]]) -> list[tuple[str, ...]]:
    """返回所有小队（含单人），按人数从多到少排序。"""
    parent = {m: m for m in member_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in mutual_pairs(wishes):
        if a in parent and b in parent:
            parent[find(a)] = find(b)

    comps: dict[str, list[str]] = defaultdict(list)
    for m in parent:
        comps[find(m)].append(m)
    return sorted((tuple(sorted(c)) for c in comps.values()), key=lambda t: (-len(t), t))


def oversized(teams: Iterable[tuple[str, ...]], limit: int = MAX_TEAM) -> list[tuple[str, ...]]:
    return [t for t in teams if len(t) > limit]
