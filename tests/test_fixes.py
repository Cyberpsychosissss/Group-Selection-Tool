"""2026-10-10 审查后补的回归测试：XSS、池的退路、求解器最优、反代链接、口令限速、跨站 POST、占位、本组人数。"""
import itertools
import os
import random
import sqlite3
import tempfile
import time
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app import config
from app.domain.execution import member_swaps
from app.domain.solver import solve
from app.domain.teams import build_teams
from app.services import GHOST_NAME, Service, UserError
from app.store import Store
from tests.helpers import FULL, VACANCY, build_pool, start_execution
from tests.test_flow import _run_to_completion

PAYLOAD = "<svg/onload=alert()>"  # 正好 20 字符
CYCLE = {"groups": {"A": 5, "B": 5, "C": 5},  # 三人环，全满：C1 要换两次
         "teams": [["A1", "B2", "B3"], ["B1", "C2", "C3"], ["C1", "A2", "A3"]]}


@pytest.fixture
def svc():
    return Service(Store(os.path.join(tempfile.mkdtemp(), "t.db")))


def _reg(svc, name, grp):
    return svc.current(svc.register(name, grp))["id"]


def _act_until(svc, ids, pred, limit=60):
    """谁能动谁就动，直到 pred(svc) 为真。"""
    for _ in range(limit):
        if pred():
            return
        for mid in ids.values():
            v = svc.pool_view(mid)
            if v["my_action"]:
                svc.act(mid, v["my_action"], v["my_seq"])
                break
        else:
            raise AssertionError("没有人可以行动")
    raise AssertionError("没有达到预期状态")


# ---------- XSS ----------

def test_partner_nickname_is_escaped_in_swap_text(svc):
    ids = {PAYLOAD: _reg(svc, PAYLOAD, "A")}
    ids.update({f"A{i}": _reg(svc, f"A{i}", "A") for i in range(1, 5)})
    ids.update({f"B{i}": _reg(svc, f"B{i}", "B") for i in range(5)})
    code = svc.create_pool(ids[PAYLOAD], 5)
    for n, m in ids.items():
        if n != PAYLOAD:
            svc.join_pool(m, code, 5)
    team = [PAYLOAD, "B0", "B1", "B2"]
    for a in team:
        for b in team:
            if a != b:
                svc.set_wish(ids[a], ids[b], True)
    svc.make_plan(ids[PAYLOAD])
    victim = next(m for m in ids.values() if svc.pool_view(m)["mine"]["partner"] == PAYLOAD)
    text = svc.pool_view(victim)["swap_text"]
    assert PAYLOAD not in text and "&lt;svg" in text and "<b>" in text


def test_oversized_team_todo_is_escaped(svc):
    ids = {PAYLOAD: _reg(svc, PAYLOAD, "A")}
    ids.update({f"A{i}": _reg(svc, f"A{i}", "A") for i in range(1, 5)})
    ids.update({f"B{i}": _reg(svc, f"B{i}", "B") for i in range(5)})
    code = svc.create_pool(ids[PAYLOAD], 5)
    for n, m in ids.items():
        if n != PAYLOAD:
            svc.join_pool(m, code, 5)
    team = [PAYLOAD, "A1", "A2", "A3", "A4", "B0"]  # 6 人：超员
    for a in team:
        for b in team:
            if a != b:
                svc.set_wish(ids[a], ids[b], True)
    todo = svc.pool_view(ids["A1"])["todo"]
    assert "小队超过 5 人" in todo and PAYLOAD not in todo and "&lt;svg" in todo
    with pytest.raises(UserError, match="超过 5 人"):
        svc.make_plan(ids["A1"])


# ---------- 池的退路 ----------

def test_abort_returns_to_open_recounts_and_lets_homeless_rejoin(svc):
    ids = build_pool(svc, FULL)
    start_execution(svc, ids)
    anyone = ids["E1"]
    with pytest.raises(UserError, match="中止执行"):
        svc.leave_pool(anyone)
    with pytest.raises(UserError, match="中止执行"):
        svc.reject(anyone)
    # 让第一个人先退出组（全满的池第一步一定是 leave），然后中止
    _act_until(svc, ids, lambda: any(m["grp"] is None for m in svc.identities()))
    homeless = next(m for m in svc.identities() if m["grp"] is None)
    svc.abort(anyone)
    svc.abort(anyone)  # 重复：忽略
    v = svc.pool_view(anyone)
    assert v["pool"]["state"] == "open" and not v.get("new_groups")
    assert v["teams"], "互选应保留"
    counts = {g["grp"]: g["lm_count"] for g in v["groups"]}
    assert sum(counts.values()) == len(ids) - 1  # 退出组的那个人不算在任何组里
    assert not v["wishes_open"] and "无组" in svc.pool_view(homeless["id"])["todo"]
    with pytest.raises(UserError, match="到齐"):
        svc.set_wish(anyone, ids["F2"], True)  # E1 和 F2 之前没互选过
    with pytest.raises(UserError, match="池内的组"):
        svc.change_group(homeless["id"], "Z")
    grp = next(g for g in counts if counts[g] < 5)
    svc.change_group(homeless["id"], grp)  # 他在 LM Core 加入了一个有空位的组
    v = svc.pool_view(anyone)
    assert v["wishes_open"] and sum(g["lm_count"] for g in v["groups"]) == len(ids)
    svc.make_plan(anyone)
    assert svc.pool_view(anyone)["pool"]["state"] == "confirming"


def test_done_pool_releases_groups_and_members(svc):
    ids = build_pool(svc, VACANCY)
    view = _run_to_completion(svc, ids)
    assert view["pool"]["state"] == "done"
    svc.leave_pool(ids["N1"])
    assert svc.current is not None and svc.pool_view(ids["N1"]) is None
    v = svc.pool_view(ids["N2"])  # 别人还能看最终分组，退出的人仍在表里
    assert "N1" in {r["name"] for rows in v["new_groups"].values() for r in rows}
    code = svc.create_pool(ids["N2"], 5)  # 自动离开已完成的池，N 组可以再建池
    assert svc.pool_view(ids["N2"])["pool"]["code"] == code
    svc.join_pool(ids["N3"], code, 5)  # 同样自动离开
    assert svc.pool_view(ids["N3"])["pool"]["code"] == code
    svc.change_group(ids["B1"], "Z")  # 在完成的池里改组号：先自动退出
    assert svc.pool_view(ids["B1"]) is None and svc.current and next(
        m["grp"] for m in svc.identities() if m["id"] == ids["B1"]) == "Z"
    for n in ("N4", "N5", "B2", "B3", "B4", "C1", "C2", "C3", "C4"):
        svc.leave_pool(ids[n])
    with svc.store.read() as r:
        assert len(r.all_pools()) == 1  # 最后一人退出后旧池删除


def test_admin_can_delete_one_pool():
    from app.web import app, svc as web_svc

    web_svc.purge()
    ids = build_pool(web_svc, VACANCY)
    other = web_svc.current(web_svc.register("solo-q", "Q"))["id"]
    web_svc.create_pool(other, 1)
    pool_id = web_svc.pool_view(ids["N1"])["pool"]["id"]
    client = TestClient(app)
    assert client.post("/admin/pool/delete", data={"pool_id": pool_id}, follow_redirects=False).status_code == 303
    assert web_svc.pool_view(ids["N1"]) is None and web_svc.pool_view(other) is not None
    assert web_svc.stats()["members"] == len(ids) + 1


# ---------- 本组人数 / 占位 ----------

def test_group_count_can_be_corrected(svc):
    a1, a2, a3 = (_reg(svc, f"a{i}", "A") for i in range(3))
    b1, b2 = _reg(svc, "b1", "B"), _reg(svc, "b2", "B")
    code = svc.create_pool(a1, 5)  # 填错：A 组其实只有 2 人
    svc.join_pool(a2, code, 5)
    svc.join_pool(b1, code, 2)
    svc.join_pool(b2, code, 2)
    assert not svc.pool_view(a1)["wishes_open"]
    with pytest.raises(UserError, match="2–5"):
        svc.set_group_count(a1, 1)
    svc.set_group_count(a1, 2)
    assert svc.pool_view(a1)["wishes_open"]
    with pytest.raises(UserError, match="更正"):
        svc.join_pool(a3, code, 5)
    svc.set_group_count(a2, 3)  # 原来填少了也能往上改
    svc.join_pool(a3, code, 5)
    with pytest.raises(UserError, match="方案已经生成"):
        svc.make_plan(a1) or svc.set_group_count(a1, 3)


def test_ghost_occupies_seat_and_never_moves(svc):
    ids = {f"a{i}": _reg(svc, f"a{i}", "A") for i in range(1, 5)}
    ids.update({f"b{i}": _reg(svc, f"b{i}", "B") for i in range(1, 6)})
    code = svc.create_pool(ids["a1"], 5)  # A 组 5 人，但只有 4 人肯加入
    for n, m in ids.items():
        if n != "a1":
            svc.join_pool(m, code, 5)
    assert not svc.pool_view(ids["a1"])["wishes_open"]
    svc.remove_ghost(ids["b1"], "nope")  # 不存在的占位：忽略
    with pytest.raises(UserError, match="到齐"):
        svc.add_ghost(ids["b1"])  # B 组已到齐，不能加
    svc.add_ghost(ids["a1"])
    v = svc.pool_view(ids["a1"])
    assert v["wishes_open"]
    ghost = next(m for g in v["groups"] for m in g["members"] if m["ghost"])
    with pytest.raises(UserError, match="同一个池"):
        svc.set_wish(ids["a1"], ghost["id"], True)
    with pytest.raises(UserError, match="到齐"):
        svc.add_ghost(ids["a2"])
    svc.remove_ghost(ids["a1"], ghost["id"])
    assert not svc.pool_view(ids["a1"])["wishes_open"]
    svc.add_ghost(ids["a1"])
    ghost = next(m for g in svc.pool_view(ids["a1"])["groups"] for m in g["members"] if m["ghost"])
    svc.set_wish(ids["a1"], ids["b1"], True)
    svc.set_wish(ids["b1"], ids["a1"], True)
    svc.make_plan(ids["a1"])
    for m in ids.values():
        svc.approve(m)  # 占位不用同意
    v = svc.pool_view(ids["a1"])
    assert v["pool"]["state"] == "executing"
    rows = {r["name"]: r for rows in v["new_groups"].values() for r in rows}
    assert rows[GHOST_NAME]["from"] == rows[GHOST_NAME]["to"] == "A" and rows[GHOST_NAME]["approved"]
    assert all(len(rows) <= 5 for rows in v["new_groups"].values())
    _act_until(svc, ids, lambda: svc.pool_view(ids["a1"])["pool"]["state"] == "done")
    final = {r["name"]: r["to"] for rows in svc.pool_view(ids["a1"])["new_groups"].values() for r in rows}
    assert final["a1"] == final["b1"] and final[GHOST_NAME] == "A"
    with svc.store.read() as r:
        assert max(Counter(m["grp"] for m in r.pool_members(v["pool"]["id"])).values()) <= 5
        assert r.member(ghost["id"])["grp"] == "A"


def test_ghosts_leave_with_their_group(svc):
    a1, b1 = _reg(svc, "a1", "A"), _reg(svc, "b1", "B")
    code = svc.create_pool(a1, 2)
    svc.join_pool(b1, code, 1)
    svc.add_ghost(a1)
    svc.leave_pool(a1)  # A 组没有真人了：占位和组一起撤掉
    with svc.store.read() as r:
        assert [m["grp"] for m in r.pool_members(svc.pool_view(b1)["pool"]["id"])] == ["B"]
        assert r.pool_of_group("A") is None
    assert svc.stats()["members"] == 2  # identities 不含占位


# ---------- 求解器 ----------

def test_solver_moves_one_person_not_six():
    cur = {"a1": "A", "a2": "A", "a3": "A", "b1": "B", "b2": "B", "b3": "B", "b4": "B", "b5": "B"}
    team = ["a1", "a2", "a3", "b1"]
    target = solve(cur, ["A", "B"], build_teams(cur, [(x, y) for x in team for y in team if x != y]))
    assert {m for m in cur if cur[m] != target[m]} == {"b1"}


def test_solver_respects_fixed_seats():
    cur = {"a1": "A", "b1": "B", "b2": "B", "b3": "B", "b4": "B", "b5": "B"}
    teams = build_teams(cur, [("a1", "b1"), ("b1", "a1")])
    target = solve(cur, ["A", "B"], teams, fixed={"A": 4})  # A 里还有 4 个占位
    assert target["a1"] == target["b1"]
    assert Counter(target.values())["A"] + 4 <= 5


def test_solver_heuristic_fallback_is_still_valid(monkeypatch):
    """组很多时退回近似解：小队仍同组、不超员、占位仍被尊重。"""
    from app.domain import solver as solver_mod

    monkeypatch.setattr(solver_mod, "MAX_STATES", 1)
    rng = random.Random(3)
    for _ in range(30):
        groups = [f"G{i}" for i in range(rng.randint(2, 6))]
        current = {f"{g}{k}": g for g in groups for k in range(rng.randint(2, 5))}
        people = list(current)
        rng.shuffle(people)
        wishes, i = [], 0
        while i < len(people):
            team = people[i: i + rng.choice([1, 2, 2, 3, 4])]
            wishes += [(a, b) for a in team for b in team if a != b]
            i += len(team)
        teams = build_teams(current, wishes)
        fixed = {groups[0]: 1}
        current = {m: g for m, g in current.items() if not (g == groups[0] and m.endswith("0"))}
        teams = [tuple(m for m in t if m in current) for t in teams]
        teams = [t for t in teams if t]
        try:
            target = solve(current, groups, teams, fixed=fixed)
        except solver_mod.Infeasible:
            continue
        for t in teams:
            assert len({target[m] for m in t}) == 1
        cnt = Counter(target.values())
        assert all(cnt[g] + fixed.get(g, 0) <= 5 for g in groups)
        assert set(target) == set(current)


def _brute(current, groups, teams, cap=5):
    best = None
    for assign in itertools.product(range(len(groups)), repeat=len(teams)):
        cnt = Counter()
        for t, gi in zip(teams, assign):
            cnt[gi] += len(t)
        if any(c > cap for c in cnt.values()):
            continue
        moves = sum(1 for t, gi in zip(teams, assign) for m in t if current[m] != groups[gi])
        best = moves if best is None or moves < best else best
    return best


@pytest.mark.parametrize("seed", range(60))
def test_solver_matches_brute_force_on_small_pools(seed):
    rng = random.Random(seed)
    groups = [f"G{i}" for i in range(rng.randint(2, 3))]
    current = {f"{g}{k}": g for g in groups for k in range(rng.randint(1, 5))}
    if len(current) > 9:
        return
    people = list(current)
    rng.shuffle(people)
    wishes, i = [], 0
    while i < len(people):
        team = people[i: i + rng.choice([1, 1, 2, 2, 3, 4, 5])]
        wishes += [(a, b) for a in team for b in team if a != b]
        i += len(team)
    teams = build_teams(current, wishes)
    target = solve(current, groups, teams)
    assert sum(current[m] != target[m] for m in current) == _brute(current, groups, teams)


# ---------- 一对一对话 ----------

def test_full_note_names_current_step_and_old_partner_is_filtered(svc):
    ids = build_pool(svc, CYCLE)
    start_execution(svc, ids)
    with svc.store.read() as r:
        st = svc._exec_state(r, svc.pool_view(ids["A1"])["pool"]["id"])
    order = member_swaps(st.swaps)
    twice = next(n for n, m in ids.items() if len(order.get(m, [])) == 2)
    first, second = (st.swaps[i] for i in order[ids[twice]])
    hub = first.route(ids[twice])[1]
    assert hub != second.route(ids[twice])[1]
    pair, _ = svc.chat_peers(ids[twice])[0]
    svc.send_quick(ids[twice], "full", pair)
    old_partner = first.partner(ids[twice])
    assert svc.chat_since(old_partner, 0)[-1]["text"] == f"LM Core 上 {hub} 组显示已满，无法保存。"
    # 第一次交换做完：换两次的人的对话对象变了；旧搭档还能发，但他收不到（页面上也没有入口）
    _act_until(svc, ids, lambda: svc.pool_view(ids[twice])["my_seq"] == 1)
    assert svc.chat_peers(ids[twice])[0][0] != pair
    before = len(svc.chat_since(ids[twice], 0))
    svc.send_quick(old_partner, "ok", svc.chat_peers(old_partner)[0][0])
    assert len(svc.chat_since(ids[twice], 0)) == before
    assert svc.chat_since(old_partner, 0)[-1]["text"] == "收到。"


# ---------- Web 层 ----------

def test_links_use_forwarded_proto_when_proxy_trusted(monkeypatch):
    from app.web import app

    c = TestClient(app)
    c.cookies = c.post("/register", data={"nickname": "proxy-u1", "grp": "N"}, follow_redirects=False).cookies
    hdr = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "gst.example.com"}
    assert "http://testserver/login/" in c.get("/", headers=hdr).text
    monkeypatch.setattr(config, "TRUST_PROXY", True)
    assert "https://gst.example.com/login/" in c.get("/", headers=hdr).text


def test_client_ip_takes_hops_from_the_right(monkeypatch):
    from starlette.requests import Request

    from app.web import _client_ip

    def req(fwd):
        return Request({"type": "http", "client": ("127.0.0.1", 1), "headers": [(b"x-forwarded-for", fwd.encode())]})

    monkeypatch.setattr(config, "TRUST_PROXY", True)
    monkeypatch.setattr(config, "PROXY_HOPS", 2)
    assert _client_ip(req("6.6.6.6, 1.2.3.4, 10.0.0.1")) == "1.2.3.4"
    assert _client_ip(req("1.2.3.4")) == "1.2.3.4"


def test_gate_ip_throttle_is_temporary(monkeypatch):
    from app.web import app, limiter

    monkeypatch.setattr(config, "ACCESS_CODE", "test-code")
    monkeypatch.setattr(config, "RL_GATE_IP", (2, 60))
    for _ in range(2):  # 两台不同设备各输错一次（同一个 IP）
        TestClient(app).post("/gate", data={"code": "x", "next": "/"}, follow_redirects=False)
    r = TestClient(app).post("/gate", data={"code": "test-code", "next": "/"}, follow_redirects=False)
    assert "gst_gate" not in r.cookies and "err=" in r.headers["location"]
    monkeypatch.setattr(limiter, "_clock", lambda: time.monotonic() + 61)  # 一分钟后自动恢复，不是锁 10 分钟
    r = TestClient(app).post("/gate", data={"code": "test-code", "next": "/"}, follow_redirects=False)
    assert "gst_gate" in r.cookies


def test_cross_site_posts_are_rejected():
    from app.web import app

    c = TestClient(app)
    data = {"nickname": "csrf-u1", "grp": "N"}
    assert c.post("/register", data=data, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/register", data=data, headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert c.post("/register", data=data, headers={"Origin": "https://evil.example"}).status_code == 403
    r = c.post("/register", data=data, headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"},
               follow_redirects=False)
    assert r.status_code == 303 and "gst" in r.cookies


def test_only_lock_timeouts_become_503(monkeypatch):
    from app import web

    monkeypatch.setattr(web.svc, "current", lambda token: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    assert TestClient(web.app).get("/guide").status_code == 503
    monkeypatch.setattr(web.svc, "current", lambda token: (_ for _ in ()).throw(sqlite3.OperationalError("no such table: x")))
    assert TestClient(web.app).get("/guide").status_code == 500


# ---------- 页面在每个状态都能渲染 ----------

def test_pool_page_renders_in_every_state():
    from app.web import app, svc as web_svc

    web_svc.purge()
    ids = {f"a{i}": web_svc.current(web_svc.register(f"a{i}", "A"))["id"] for i in range(1, 5)}
    ids.update({f"b{i}": web_svc.current(web_svc.register(f"b{i}", "B"))["id"] for i in range(1, 6)})
    code = web_svc.create_pool(ids["a1"], 5)
    for n, m in ids.items():
        if n != "a1":
            web_svc.join_pool(m, code, 5)

    def page(name):
        c = TestClient(app)
        c.cookies.set("gst", web_svc.login_as(ids[name]))
        r = c.get("/pool")
        assert r.status_code == 200, r.text
        return r.text

    html = page("a1")  # 互选前：本组工具（占位 / 更正人数）
    assert "+ 占位" in html and "更正人数" in html and "未加入" in html
    web_svc.add_ghost(ids["a1"])
    html = page("b1")
    assert GHOST_NAME in html and "+ 占位" not in html  # B 组到齐，B 的人看不到加占位
    for a, b in (("a1", "b1"), ("b1", "a1")):
        web_svc.set_wish(ids[a], ids[b], True)
    web_svc.make_plan(ids["a1"])
    assert "同意" in page("a1") and "互换" in page("a1")  # 确认
    for m in ids.values():
        web_svc.approve(m)
    html = page("a1")
    assert "中止执行" in html and 'data-confirm' in html  # 执行
    assert "LM Core" in html
    _act_until(web_svc, ids, lambda: any(m["grp"] is None for m in web_svc.identities()))
    homeless = next(n for n, m in ids.items() if next(x for x in web_svc.identities() if x["id"] == m)["grp"] is None)
    web_svc.abort(ids["a1"])
    html = page(homeless)  # 中止后：暂无组的人看到更新组号表单
    assert "我已加入该组" in html and "暂无组" in html
    assert "待更新组号" in page("a1")
    web_svc.change_group(ids[homeless], "B" if homeless.startswith("b") else "A")
    web_svc.make_plan(ids["a1"])
    for m in ids.values():
        web_svc.approve(m)
    _act_until(web_svc, ids, lambda: web_svc.pool_view(ids["a1"])["pool"]["state"] == "done")
    html = page("a1")  # 完成：退出按钮 + 最终分组
    assert "最终分组" in html and "退出池" in html and GHOST_NAME in html
    web_svc.leave_pool(ids["a1"])
    assert "a1" in page("b1")  # 退出的人仍显示在最终分组里
    admin = TestClient(app).get("/admin").text
    assert code in admin and "删除" in admin
