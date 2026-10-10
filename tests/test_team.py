"""意向组 + 全站统一分配（2026-10-10）：
组长拉人、各自登记组号 / 人数、宣布到齐 → 全站所有意向组一起算 → 坐得下或能互换时自动开一轮 → 确认 / 执行 / 完成。
不参加工具的人谁都不动；几个意向组互换位置算一次成功；人多的优先；爬虫接口推人数。"""
import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import config
from app.domain.allocate import allocate
from app.services import ROUND_GHOST_NAME, Service, UserError
from app.store import Store


@pytest.fixture
def svc():
    return Service(Store(os.path.join(tempfile.mkdtemp(), "t.db")))


def _reg(svc, name, grp):
    return svc.current(svc.register(name, grp))["id"]


def _team(svc, spec: dict[str, tuple[str, int]], lock=True) -> dict[str, str]:
    """spec: 代号 → (组, 该组人数)。第一个人是组长。返回 代号 → id。"""
    ids = {}
    leader = None
    for name, (grp, n) in spec.items():
        ids[name] = _reg(svc, name, grp)
        if leader is None:
            leader = ids[name]
            code = svc.create_team(leader, n)
        else:
            svc.join_team(ids[name], code, n)
    if lock:
        svc.lock_team(leader)
    return ids


def _state(svc, mid):
    return svc.pool_view(mid)["pool"]["state"]


def _run(svc, ids, limit=100):
    """全员同意 → 谁能动谁就动，直到完成。"""
    for mid in ids.values():
        if _state(svc, mid) == "confirming":
            svc.approve(mid)
    for _ in range(limit):
        if all(_state(svc, m) == "done" for m in ids.values()):
            return
        for mid in ids.values():
            v = svc.pool_view(mid)
            if v.get("my_action"):
                svc.act(mid, v["my_action"], v["my_seq"])
                break
        else:
            raise AssertionError("没有人可以行动")
    raise AssertionError("没有完成")


# ---------- 分配算法 ----------

def test_allocate_moves_team_into_group_with_room():
    teams = {"t": ("a", "b", "c")}
    cur = {"a": "A", "b": "B", "c": "C"}
    counts = {"A": 5, "B": 5, "C": 3, "D": 1}  # C 有 2 个空位（c 已在里面）：a、b 搬去 C，2 人移动；D 要 3 人移动
    r = allocate(teams, cur, counts)
    assert r.teams == ("t",) and r.moves == 2 and {r.target[m] for m in "abc"} == {"C"}
    assert set(r.target) == {"a", "b", "c"}  # 组外的人不在方案里


def test_allocate_two_teams_swap_when_everything_is_full():
    teams = {"x": ("x1", "x2"), "y": ("y1", "y2")}
    cur = {"x1": "A", "x2": "B", "y1": "B", "y2": "A"}
    r = allocate(teams, cur, {"A": 5, "B": 5})
    assert set(r.teams) == {"x", "y"} and r.moves == 2
    assert len({r.target[m] for m in ("x1", "x2")}) == 1 and len({r.target[m] for m in ("y1", "y2")}) == 1


def test_allocate_prefers_more_people_then_fewer_moves():
    # 只有一个组 Z 有 3 个空位：3 人的 big 和 2 人的 small 不能都进；选人多的
    teams = {"big": ("b1", "b2", "b3"), "small": ("s1", "s2")}
    cur = {"b1": "A", "b2": "B", "b3": "C", "s1": "D", "s2": "E"}
    counts = {"A": 5, "B": 5, "C": 5, "D": 5, "E": 5, "Z": 2}
    r = allocate(teams, cur, counts)
    assert r.teams == ("big",) and r.moves == 3 and {r.target[m] for m in ("b1", "b2", "b3")} == {"Z"}


def test_allocate_uses_seats_freed_by_another_team_in_the_same_round():
    # 全局视角：big 搬去 Z 后 A、B 各空出一个位子，small 顺势凑到 A —— 一轮里两队都如愿
    teams = {"big": ("b1", "b2", "b3"), "small": ("s1", "s2")}
    cur = {"b1": "A", "b2": "B", "b3": "C", "s1": "A", "s2": "B"}
    counts = {"A": 5, "B": 5, "C": 5, "Z": 2}
    r = allocate(teams, cur, counts)
    assert set(r.teams) == {"big", "small"} and r.moves == 4
    assert {r.target[m] for m in ("b1", "b2", "b3")} == {"Z"} and r.target["s1"] == r.target["s2"] == "A"


def test_allocate_none_when_nobody_fits():
    assert allocate({"t": ("a", "b")}, {"a": "A", "b": "B"}, {"A": 5, "B": 5}) is None


def test_allocate_leaves_unchosen_teams_in_place_as_fixed_seats():
    # C 有 2 个空位。p（2 人）已占 C 里 1 个位子 + 别处 1 人；q（2 人）在 A、B。两队都想要 C：只能进一队，
    # 没选中的那队的人要当作占着位置不动。
    teams = {"p": ("p1", "p2"), "q": ("q1", "q2")}
    cur = {"p1": "C", "p2": "A", "q1": "D", "q2": "E"}
    counts = {"A": 5, "B": 5, "C": 4, "D": 5, "E": 5}
    r = allocate(teams, cur, counts)
    assert r.teams == ("p",) and r.moves == 1 and r.target == {"p1": "C", "p2": "C"}


def test_allocate_respects_time_budget_with_many_teams():
    teams = {f"t{i}": (f"m{i}a", f"m{i}b") for i in range(9)}
    cur = {m: ["A", "B", "C"][i % 3] for t in teams.values() for i, m in enumerate(t)}
    counts = {"A": 5, "B": 5, "C": 5, "D": 0, "E": 0}
    r = allocate(teams, cur, counts, budget=0.05)
    assert r is not None and len(r.teams) >= 1


# ---------- 意向组流程 ----------

def test_team_waits_until_crawler_frees_seats_then_runs_to_done(svc):
    ids = _team(svc, {"L": ("A", 5), "m2": ("B", 5), "m3": ("C", 5)})
    v = svc.pool_view(ids["L"])
    assert v["kind"] == "team" and v["stage"] == "wait" and v["pool"]["state"] == "open"
    assert "还坐不下" in v["todo"] and all(not c["feasible"] for c in v["candidates"])
    out = svc.update_counts({"C": 3})  # C 组走了两个人
    assert out["planned"] and _state(svc, ids["L"]) == "confirming"
    for n in ("L", "m2", "m3"):
        v = svc.pool_view(ids[n])
        assert v["kind"] == "team" and v["mine"]["to"] == "C"
    assert svc.pool_view(ids["m3"])["mine"]["moved"] is False  # m3 本来就在 C
    _run(svc, ids)
    assert {m["grp"] for m in svc.identities()} == {"C"}
    counts = {c["grp"]: c for c in svc.group_counts()}
    assert counts["C"]["count"] == 5 and counts["C"]["source"] == "round"
    assert counts["A"]["count"] == 4 and counts["B"]["count"] == 4
    html_names = {x["name"] for rows in svc.pool_view(ids["L"])["new_groups"].values() for x in rows}
    assert any(n.startswith(ROUND_GHOST_NAME) for n in html_names)  # 组里不参加的人合成一行"组内其他人 ×n"、不动
    svc.leave_team(ids["L"])
    assert svc.pool_view(ids["L"]) is None and svc.pool_view(ids["m2"])["pool"]["state"] == "done"


def test_two_teams_help_each_other_in_one_round(svc):
    x = _team(svc, {"x1": ("A", 5), "x2": ("B", 5)})
    assert _state(svc, x["x1"]) == "open"  # 一个队自己坐不下
    y = _team(svc, {"y1": ("B", 5), "y2": ("A", 5)})
    assert _state(svc, x["x1"]) == "confirming" == _state(svc, y["y1"])  # 两队互换，一起进入确认
    v = svc.pool_view(x["x1"])
    assert v["movers_total"] == 2 and v["swaps"] == 1 and v["total"] == 4  # 只有 4 个真人需要表态
    assert sum(len(rows) for rows in v["new_groups"].values()) == 4 + 2  # A、B 各一行"组内其他人 ×3"
    _run(svc, {**x, **y})
    grp = {m["nickname"]: m["grp"] for m in svc.identities()}
    assert grp["x1"] == grp["x2"] and grp["y1"] == grp["y2"] and grp["x1"] != grp["y1"]
    counts = {c["grp"]: c["count"] for c in svc.group_counts()}
    assert counts == {"A": 5, "B": 5}


def test_bigger_team_wins_the_only_room_and_smaller_waits(svc):
    small = _team(svc, {"s1": ("D", 5), "s2": ("E", 5)})
    big = _team(svc, {"b1": ("A", 5), "b2": ("B", 5), "b3": ("C", 5)})
    assert _state(svc, small["s1"]) == "open" == _state(svc, big["b1"])
    svc.update_counts({"Z": 2})  # 爬虫：Z 组只有 2 人 —— 3 个空位，两队不能都进，人多的先
    assert _state(svc, big["b1"]) == "confirming" and _state(svc, small["s1"]) == "open"
    assert "另一轮" in svc.pool_view(small["s1"])["todo"]  # Z 坐得下 small，但正被 big 那一轮占着
    _run(svc, big)
    counts = {c["grp"]: c["count"] for c in svc.group_counts()}
    assert counts["Z"] == 5 and counts["A"] == 4 and counts["B"] == 4 and counts["C"] == 4
    assert _state(svc, small["s1"]) == "open" and "还坐不下" in svc.pool_view(small["s1"])["todo"]
    svc.update_counts({"D": 4})  # D 组走了一个人：s2 可以搬过去
    assert _state(svc, small["s1"]) == "confirming" and svc.pool_view(small["s2"])["mine"]["to"] == "D"


def test_small_team_rides_on_seats_freed_by_big_team_in_the_same_round(svc):
    small = _team(svc, {"s1": ("A", 5), "s2": ("B", 5)})
    big = _team(svc, {"b1": ("A", 5), "b2": ("B", 5), "b3": ("C", 5)})
    svc.update_counts({"Z": 2})  # big 搬去 Z，A 空出一个位子，s2 顺势搬到 A：一轮里两队都如愿
    assert _state(svc, big["b1"]) == "confirming" == _state(svc, small["s1"])
    assert svc.pool_view(small["s1"])["code"] != svc.pool_view(big["b1"])["code"]
    assert svc.pool_view(small["s2"])["mine"]["to"] == "A" and svc.pool_view(big["b1"])["mine"]["to"] == "Z"
    _run(svc, {**small, **big})
    grp = {m["nickname"]: m["grp"] for m in svc.identities()}
    assert grp["s1"] == grp["s2"] == "A" and grp["b1"] == grp["b2"] == grp["b3"] == "Z"


def test_already_together_team_finishes_immediately(svc):
    ids = _team(svc, {"a": ("A", 5), "b": ("A", 5)})
    assert _state(svc, ids["a"]) == "done"
    assert not svc.pool_view(ids["a"])["mine"]["moved"]


def test_reject_cancels_round_pauses_that_team_and_replans_others(svc):
    x = _team(svc, {"x1": ("A", 5), "x2": ("B", 5)})
    y = _team(svc, {"y1": ("C", 5), "y2": ("D", 5)})
    svc.update_counts({"Z": 0})  # 空组：两队一起进（2+2）
    assert _state(svc, x["x1"]) == "confirming" == _state(svc, y["y1"])
    assert svc.pool_view(x["x1"])["code"] != svc.pool_view(y["y1"])["code"]  # 各自的意向组邀请码
    svc.reject(y["y2"])
    assert _state(svc, y["y1"]) == "open" and svc.pool_view(y["y1"])["paused"] and "暂停" in svc.pool_view(y["y1"])["todo"]
    assert _state(svc, x["x1"]) == "confirming"  # x 不受牵连：立刻单独重新开了一轮
    with pytest.raises(UserError, match="组长"):
        svc.resume_team(y["y2"])
    svc.resume_team(y["y1"])
    assert _state(svc, y["y1"]) == "open" and "另一轮" in svc.pool_view(y["y1"])["todo"]  # Z 正被 x 那一轮占着
    _run(svc, x)
    assert _state(svc, y["y1"]) == "confirming"  # x 完成后 Z 还剩 3 个位子，y 自动进入


def test_abort_recounts_and_homeless_member_reports_new_group(svc):
    x = _team(svc, {"x1": ("A", 5), "x2": ("B", 5)})
    y = _team(svc, {"y1": ("B", 5), "y2": ("A", 5)})
    everyone = {**x, **y}
    for mid in everyone.values():
        svc.approve(mid)
    first = next(m for m in everyone.values() if svc.pool_view(m)["my_action"] == "leave")
    svc.act(first, "leave", 0)
    svc.abort(first)
    me = next(m for m in svc.identities() if m["id"] == first)
    assert me["grp"] is None
    counts = {c["grp"]: c["count"] for c in svc.group_counts()}
    assert sorted(counts.values()) == [4, 5]  # 退出的那个组少了一个人
    v = svc.pool_view(first)
    assert v["kind"] == "team" and v["pool"]["state"] == "open" and v["paused"] and "暂时无组" in v["todo"]
    mine, other = (x, y) if first in x.values() else (y, x)
    assert _state(svc, next(iter(other.values()))) == "confirming"  # 另一队没被牵连，而且空出的位子正好够它
    with pytest.raises(UserError, match="人数"):
        svc.change_group(first, "Q")  # 没人填过 Q 组的人数
    svc.change_group(first, "Q", 1)
    assert {c["grp"]: c["count"] for c in svc.group_counts()}["Q"] == 1
    leader = next(m for m in mine.values() if svc.pool_view(m)["is_leader"])
    svc.resume_team(leader)
    assert _state(svc, first) == "open" and "另一轮" in svc.pool_view(first)["todo"]  # 队友的组被另一轮占着
    _run(svc, other)
    assert _state(svc, first) == "confirming"  # 另一轮完成后自动进入
    targets = {svc.pool_view(m)["mine"]["to"] for m in mine.values()}
    assert len(targets) == 1


def test_guards_join_leave_lock_leader(svc):
    ids = _team(svc, {"L": ("A", 5), "m2": ("B", 5)}, lock=False)
    code = svc.pool_view(ids["L"])["code"]
    with pytest.raises(UserError, match="组长"):
        svc.lock_team(ids["m2"])
    late = _reg(svc, "late", "C")
    for i in range(3):
        svc.join_team(_reg(svc, f"f{i}", "C"), code, 5)
    with pytest.raises(UserError, match="最多"):
        svc.join_team(late, code, 5)
    svc.leave_team(ids["m2"])
    svc.join_team(late, code, 5)
    svc.lock_team(ids["L"])
    with pytest.raises(UserError, match="到齐"):
        svc.join_team(_reg(svc, "x", "A"), code, 5)
    with pytest.raises(UserError, match="到齐"):
        svc.leave_team(late)
    svc.unlock_team(ids["L"])
    svc.leave_team(ids["L"])  # 组长退出：最早登记的成员接任（late 登记最早）
    assert svc.pool_view(late)["is_leader"] is True
    for n in ("f0", "f1", "f2"):
        svc.leave_team(next(m["id"] for m in svc.identities() if m["nickname"] == n))
    svc.leave_team(late)
    with svc.store.read() as r:
        assert r.all_teams() == []
    one = _reg(svc, "one", "A")
    svc.create_team(one, 5)
    with pytest.raises(UserError, match="至少 2"):
        svc.lock_team(one)


def test_team_and_pool_are_exclusive(svc):
    a = _reg(svc, "a", "A")
    svc.create_team(a, 5)
    with pytest.raises(UserError, match="意向组"):
        svc.create_pool(a, 5)
    b = _reg(svc, "b", "B")
    svc.create_pool(b, 5)
    with pytest.raises(UserError, match="整组池"):
        svc.create_team(b, 5)
    code = svc.pool_view(a)["code"]
    with pytest.raises(UserError, match="整组池"):
        svc.join_team(b, code, 5)
    assert svc.invite_kind(code) == "team" and svc.invite_kind(svc.pool_view(b)["code"]) == "pool"
    with pytest.raises(UserError, match="池"):
        svc.set_wish(a, b, True)


def test_fresh_crawler_count_beats_member_report(svc):
    svc.update_counts({"A": 3})
    ids = _team(svc, {"a": ("A", 5), "b": ("B", 5)}, lock=False)
    assert {c["grp"]: c for c in svc.group_counts()}["A"]["count"] == 3  # 两小时内的爬虫数据优先
    svc.set_group_count(ids["b"], 4)
    assert {c["grp"]: c for c in svc.group_counts()}["B"]["count"] == 4
    with pytest.raises(UserError, match="1–5"):
        svc.set_group_count(ids["b"], 0)
    with pytest.raises(UserError, match="组号不存在"):
        svc.update_counts({"ZZZ": 1})
    with pytest.raises(UserError, match="整数"):
        svc.update_counts({"A": "5"})


def test_member_count_change_triggers_allocation(svc):
    ids = _team(svc, {"a": ("A", 5), "b": ("B", 5)})
    assert _state(svc, ids["a"]) == "open"
    svc.set_group_count(ids["b"], 4)  # B 组其实只有 4 人：a 可以搬过去
    assert _state(svc, ids["a"]) == "confirming" and svc.pool_view(ids["a"])["mine"]["to"] == "B"


def test_leader_lock_requires_everyone_to_have_a_group(svc):
    ids = _team(svc, {"a": ("A", 5), "b": ("B", 5)}, lock=False)
    with svc.store.tx() as r:
        r.set_member_group(ids["b"], None)
    with pytest.raises(UserError, match="暂时无组"):
        svc.lock_team(ids["a"])


# ---------- HTTP ----------

def _client_as(web_svc, app, mid):
    c = TestClient(app)
    c.cookies.set("gst", web_svc.login_as(mid))
    return c


def test_counts_api_requires_token_and_bypasses_gate(monkeypatch):
    from app.web import app, svc as web_svc

    web_svc.purge()
    monkeypatch.setattr(config, "ACCESS_CODE", "secret-code")
    monkeypatch.setattr(config, "COUNTS_TOKEN", "")
    c = TestClient(app)
    assert c.post("/api/counts", json={"counts": {"A": 1}}).status_code == 401  # 没配令牌：接口关闭
    monkeypatch.setattr(config, "COUNTS_TOKEN", "tok-123")
    assert c.post("/api/counts", json={"counts": {"A": 1}}).status_code == 401
    assert c.post("/api/counts", json={"counts": {"A": 1}}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    h = {"Authorization": "Bearer tok-123", "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    r = c.post("/api/counts", json={"counts": {"a": 3, "B": 5}}, headers=h)  # 不用口令、不看 Origin；组号不分大小写
    assert r.status_code == 200 and r.json() == {"updated": 2, "planned": []}
    assert c.post("/api/counts", json={"A": 2}, headers=h).json()["updated"] == 1  # 也接受直接的 {组: 人数}
    assert c.post("/api/counts", json={"counts": {"A": 9}}, headers=h).status_code == 400
    assert c.post("/api/counts", json={"counts": {"ZZZ": 1}}, headers=h).status_code == 400
    assert c.post("/api/counts", content=b"not json", headers=h).status_code == 400
    assert c.post("/api/counts", json=[], headers=h).status_code == 400
    got = c.get("/api/counts", headers=h).json()["counts"]
    assert {x["grp"]: x["count"] for x in got} == {"A": 2, "B": 5} and all(x["source"] == "crawler" for x in got)
    assert c.get("/api/counts").status_code == 401


def test_team_pages_render_in_every_state():
    from app.web import app, svc as web_svc

    web_svc.purge()
    c = TestClient(app)
    r = c.post("/register", data={"nickname": "L", "grp": "A"}, follow_redirects=False)
    assert r.status_code == 303
    r = c.post("/team/create", data={"lm_count": 5}, follow_redirects=False)
    assert r.headers["location"] == "/pool"
    html = c.get("/pool").text
    assert "意向组" in html and "成员已到齐" in html and "坐得下吗" in html and 's-team' in html
    code = web_svc.pool_view(web_svc.identities()[0]["id"])["code"]
    assert "/j/" + code in html

    # 邀请页：意向组
    c2 = TestClient(app)
    html = c2.get(f"/j/{code}").text
    assert "加入意向组" in html and 'name="lm_count"' in html
    r = c2.post("/register", data={"nickname": "m2", "grp": "B", "code": code, "lm_count": 5}, follow_redirects=False)
    assert r.headers["location"] == "/pool"
    html = c2.get("/pool").text
    assert "等组长宣布成员到齐" in html and "退出意向组" in html
    assert c2.post("/team/lock", follow_redirects=False).headers["location"].startswith("/pool?err=")  # 不是组长

    ids = {m["nickname"]: m["id"] for m in web_svc.identities()}
    assert c.post("/team/lock", follow_redirects=False).headers["location"] == "/pool"
    html = c.get("/pool").text
    assert "还坐不下" in html and "重新开放加入" in html and "等待分配" in html
    # 成员改人数 → 自动分配 → 确认页
    assert c2.post("/pool/count", data={"lm_count": 4}, follow_redirects=False).headers["location"] == "/pool"
    html = c.get("/pool").text
    assert "同意" in html and "不同意" in html and ROUND_GHOST_NAME in html and "组内其他人" in html
    live = c.get("/pool/live?rev=-1")
    assert live.status_code == 200 and live.headers["X-Rev"].isdigit() and int(live.headers["X-Rev"]) >= 1_000_000
    for cli in (c, c2):
        assert cli.post("/pool/approve", follow_redirects=False).status_code == 303
    html = c.get("/pool").text
    assert "轮到你" in html and "中止执行" in html
    v = web_svc.pool_view(ids["L"])
    assert c.post("/pool/act", data={"action": v["my_action"], "seq": v["my_seq"]}, follow_redirects=False).status_code == 303
    html = c.get("/pool").text
    assert "最终分组" in html and "退出意向组" in html
    admin = c.get("/admin").text
    assert code in admin and "意向组" in admin and "各组人数" in admin
    assert c.post("/team/leave", follow_redirects=False).headers["location"] == "/"
    assert c.get("/", follow_redirects=False).status_code == 200
    assert c.post("/admin/team/delete", data={"team_id": web_svc.stats()["team_list"][0]["id"]}, follow_redirects=False).status_code == 303
    assert web_svc.stats()["team_list"] == []


# ---------- 管理页：换组进度 ----------

def test_progress_covers_every_stage(svc):
    done = _team(svc, {"e1": ("A", 5), "e2": ("A", 5)})            # 本来就同组：立刻完成
    x = _team(svc, {"x1": ("B", 5), "x2": ("C", 5)})               # 和 y 互换，执行中
    y = _team(svc, {"y1": ("C", 5), "y2": ("B", 5)})
    for mid in {**x, **y}.values():
        svc.approve(mid)
    first = next(m for m in {**x, **y}.values() if svc.pool_view(m)["my_action"])
    svc.act(first, svc.pool_view(first)["my_action"], 0)
    waiting = _team(svc, {"w1": ("D", 5), "w2": ("E", 5)})          # 等待分配
    build = _team(svc, {"b1": ("F", 5), "b2": ("G", 5)}, lock=False)  # 组建中
    svc.update_counts({"H": 4})
    pg = svc.progress()
    assert pg["members"] == 10 and pg["in_teams"] == 10
    assert {k: v["teams"] for k, v in pg["stages"].items()} == {"build": 1, "wait": 1, "paused": 0, "confirming": 0, "executing": 2, "done": 1}
    assert pg["summary"]["done_rounds"] == 1 and pg["summary"]["executing_rounds"] == 1
    by_code = {t["code"]: t for t in pg["teams"]}
    w = by_code[svc.pool_view(waiting["w1"])["code"]]
    assert w["stage"] == "wait" and "还坐不下" in w["note"] and w["target"] is None
    b = by_code[svc.pool_view(build["b1"])["code"]]
    assert b["stage"] == "build" and "邀请链接" in b["note"] and b["leader"] == "b1"
    ex = by_code[svc.pool_view(x["x1"])["code"]]
    assert ex["stage"] == "executing" and ex["movers"] == 1 and ex["target"] in ("B", "C") and ex["round"]
    d = by_code[svc.pool_view(done["e1"])["code"]]
    assert d["stage"] == "done" and d["movers"] == 0 and d["target"] == "A"
    rounds = {r["state"]: r for r in pg["rounds"]}
    assert rounds["executing"]["movers"] == 2 and rounds["executing"]["moved"] in (0, 1) and rounds["executing"]["approved"] == 4
    assert set(rounds["executing"]["groups"]) == {"B", "C"} and len(rounds["executing"]["teams"]) == 2
    groups = {g["grp"]: g for g in pg["groups"]}
    assert groups["H"]["source"] == "crawler" and groups["H"]["members"] == []
    assert groups["B"]["busy"] and len(groups["D"]["members"]) == 1 and "w1" in groups["D"]["members"][0]
    assert pg["events"] and pg["events"][0]["at"] >= pg["events"][-1]["at"]


def test_admin_progress_page_and_live_fragment(monkeypatch):
    from app import web
    from app.web import app, svc as web_svc

    web_svc.purge()
    ids = _team(web_svc, {"p1": ("A", 5), "p2": ("B", 5)})
    c = TestClient(app)
    html = c.get("/admin").text
    assert "换组进度" in html and "等待分配" in html and "还坐不下" in html and 'data-live="/admin/live"' in html
    live = c.get("/admin/live")
    assert live.status_code == 200 and "stage-cards" in live.text and "<html" not in live.text
    web_svc.update_counts({"Z": 0})
    assert "确认中" in c.get("/admin/live").text
    monkeypatch.setattr(config, "DEV_MODE", False)
    monkeypatch.setattr(config, "WHOAMI_URL", "")
    monkeypatch.setattr(config, "ADMIN_KEY", "")
    assert c.get("/admin/live").status_code == 404
    assert ids
