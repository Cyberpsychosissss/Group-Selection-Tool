import random
from collections import Counter

import pytest

from app.domain.execution import apply, current_swap, is_done, member_swaps, next_actions
from app.domain.pairing import build_swaps, link_moves
from app.domain.solver import Infeasible, solve
from app.domain.teams import build_teams, mutual_pairs, oversized


# ---------- teams ----------

def test_only_mutual_wishes_count():
    assert mutual_pairs([("a", "b"), ("b", "a"), ("a", "c")]) == {("a", "b")}


def test_teams_are_connected_components():
    teams = build_teams("abcdef", [("a", "b"), ("b", "a"), ("b", "c"), ("c", "b"), ("d", "e")])
    assert teams == [("a", "b", "c"), ("d",), ("e",), ("f",)]


def test_oversized_team_detected():
    ids = "abcdef"
    wishes = [(x, y) for x in ids for y in ids if x != y]
    assert oversized(build_teams(ids, wishes)) == [tuple(ids)]


# ---------- solver ----------

POOL_CURRENT = {
    "N1": "N", "N2": "N", "N3": "N", "N4": "N", "N5": "N",
    "B1": "B", "B2": "B", "B3": "B", "B4": "B",
    "C1": "C", "C2": "C", "C3": "C", "C4": "C",
}
POOL_TEAMS = [("C1", "N1", "N3", "B1", "N2"), ("B2", "B3", "C2")]


def _teams_with_singles(current, multi):
    in_team = {m for t in multi for m in t}
    return multi + [(m,) for m in current if m not in in_team]


def test_solver_keeps_teams_together_and_maximises_stayers():
    target = solve(POOL_CURRENT, ["N", "B", "C"], _teams_with_singles(POOL_CURRENT, POOL_TEAMS))
    for team in POOL_TEAMS:
        assert len({target[m] for m in team}) == 1
    assert target["N1"] == "N" and target["B2"] == "B"  # 去人最多的组
    assert max(Counter(target.values()).values()) <= 5
    moved = [m for m in POOL_CURRENT if target[m] != POOL_CURRENT[m]]
    assert sorted(moved) == sorted(["B1", "C1", "C2", "N4", "N5"])


def test_solver_keeps_group_sizes_and_prefers_swaps():
    target = solve(POOL_CURRENT, ["N", "B", "C"], _teams_with_singles(POOL_CURRENT, POOL_TEAMS))
    assert Counter(target.values()) == Counter(POOL_CURRENT.values())  # 5/4/4 不变
    moves = {m: (POOL_CURRENT[m], target[m]) for m in target if target[m] != POOL_CURRENT[m]}
    links = link_moves(moves)
    assert links["C1"].partner in {"N4", "N5"}  # C1 C→N 和一个 N→C 的人互换


# ---------- pairing ----------

def test_pairing_swap_cycle_and_vacancy():
    links = link_moves({"x": ("A", "B"), "y": ("B", "A"),          # 互换
                        "p": ("C", "D"), "q": ("D", "E"), "r": ("E", "C"),  # 三人接力
                        "v": ("F", "G")})                              # 进空位
    assert links["x"].partner == "y" and links["y"].partner == "x"
    assert links["p"].takes_seat_of == "q" and links["p"].replaced_by == "r" and links["p"].partner is None
    assert links["v"].takes_seat_of is None and links["v"].replaced_by is None


def test_solver_reports_infeasible_packing():
    current = {f"p{i}": ("A" if i < 5 else "B") for i in range(10)}
    teams = [("p0", "p1", "p2"), ("p3", "p4", "p5"), ("p6", "p7", "p8", "p9")]
    with pytest.raises(Infeasible):
        solve(current, ["A", "B"], teams)


# ---------- 拆成两两交换 ----------

def test_build_swaps_pairs_chains_and_cycles():
    moves = {"x": ("A", "B"), "y": ("B", "A"),                       # 互换
             "p": ("C", "D"), "q": ("D", "E"), "r": ("E", "C"),       # 三人环
             "v": ("F", "G")}                                          # 进空位
    swaps = build_swaps(moves)
    assert all(len(s.members) <= 2 for s in swaps)
    order = member_swaps(swaps)
    assert len(order["x"]) == len(order["y"]) == len(order["v"]) == 1
    assert sorted(len(order[m]) for m in "pqr") == [1, 1, 2]          # 环里中间的人换两次
    cycle = [s for s in swaps if set(s.members) <= set("pqr")]
    assert len(cycle) == 2 and cycle[1].after == (swaps.index(cycle[0]),)


def test_cycle_with_free_seat_needs_no_double_moves():
    moves = {"p": ("C", "D"), "q": ("D", "E"), "r": ("E", "C")}
    swaps = build_swaps(moves, free={"E"})                             # E 组有空位
    assert all(s.b is None for s in swaps)                             # 依次单独移动
    assert all(len(v) == 1 for v in member_swaps(swaps).values())      # 每人只动一次
    assert swaps[0].a == "q"                                           # 先让目标组是 E 的人进空位
    cur = {"p": "C", "q": "D", "r": "E", "c1": "C", "c2": "C", "c3": "C", "c4": "C",
           "d1": "D", "d2": "D", "d3": "D", "d4": "D", "e1": "E", "e2": "E", "e3": "E"}
    done = {}
    while not is_done(swaps, done):
        (m, (a, i)), = next_actions(swaps, cur, done).items()
        assert a == "move"                                             # 全程没人无组
        cur, done = apply(swaps, cur, done, m, a, i)
    assert (cur["p"], cur["q"], cur["r"]) == ("D", "E", "C")


def test_cycle_avoids_double_moves_for_team_members():
    moves = {"p": ("C", "D"), "q": ("D", "E"), "r": ("E", "C")}
    for t in "pqr":
        order = member_swaps(build_swaps(moves, in_team={t}))
        assert len(order[t]) == 1


# ---------- 执行 ----------

def _cur(spec):
    return {m: g for g, ms in spec.items() for m in ms}


def test_vacancy_lets_people_move_without_leaving():
    cur = _cur({"A": ["x", "a1", "a2", "a3", "a4"], "B": ["y", "b1", "b2"]})   # B 有空位
    swaps = build_swaps({"x": ("A", "B"), "y": ("B", "A")})
    assert next_actions(swaps, cur, {}) == {"x": ("move", 0)}


def test_full_groups_need_one_leave_by_team_member():
    cur = _cur({"A": ["x", "a1", "a2", "a3", "a4"], "B": ["y", "b1", "b2", "b3", "b4"]})
    swaps, done = build_swaps({"x": ("A", "B"), "y": ("B", "A")}), {}
    acts = next_actions(swaps, cur, done, in_team={"y"})
    assert acts == {"y": ("leave", 0)}
    for _ in range(3):
        (m, (a, i)), = next_actions(swaps, cur, done, in_team={"y"}).items()
        cur, done = apply(swaps, cur, done, m, a, i)
    assert is_done(swaps, done) and cur["x"] == "B" and cur["y"] == "A"


def _random_pool(rng):
    n = rng.randint(2, 8)
    groups = [f"G{i}" for i in range(n)]
    current = {}
    for g in groups:
        for k in range(rng.choice([3, 4, 5, 5, 5])):
            current[f"{g}-{k}"] = g
    people = list(current)
    rng.shuffle(people)
    wishes, i = [], 0
    while i < len(people):
        size = rng.choice([1, 1, 2, 3, 4, 5])
        team = people[i : i + size]
        wishes += [(a, b) for a in team for b in team if a != b]
        i += size
    return current, groups, build_teams(current, wishes)


@pytest.mark.parametrize("seed", range(1000))
def test_random_pools_always_finish_pairwise_without_overfilling(seed):
    rng = random.Random(seed)
    current, groups, teams = _random_pool(rng)
    try:
        target = solve(current, groups, teams)
    except Infeasible:
        return
    in_team = {m for t in teams if len(t) > 1 for m in t}
    moves = {m: (current[m], target[m]) for m in current if current[m] != target[m]}
    start, final = Counter(current.values()), Counter(target.values())
    free = {g for g in start if max(start[g], final[g]) < 5}
    swaps = build_swaps(moves, in_team, free)
    assert all(len(s.members) <= 2 for s in swaps)

    # 配对一致：我接替的人，一定是从我目标组离开、并且被我接替的人
    links = link_moves(moves)
    for m, link in links.items():
        if link.takes_seat_of:
            assert moves[link.takes_seat_of][0] == moves[m][1]
            assert links[link.takes_seat_of].replaced_by == m

    cur, done = dict(current), {}
    for _ in range(4 * len(moves) + 1):
        if is_done(swaps, done):
            break
        acts = next_actions(swaps, cur, done, in_team)
        assert acts, "卡死：没有任何可执行的动作"
        # 同一时刻每个人只在一个交换里，交换对象唯一
        assert all(current_swap(swaps, done, m) == i for m, (_, i) in acts.items())
        m = rng.choice(sorted(acts))  # 随机挑一个人先完成，模拟真实的先后顺序
        cur, done = apply(swaps, cur, done, m, *acts[m])
        assert max(Counter(g for g in cur.values() if g).values()) <= 5
    assert is_done(swaps, done)
    assert cur == target
