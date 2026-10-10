"""端到端流程：services 层 + HTTP 层。"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.services import Service, UserError
from app.store import Store
from tests.helpers import FULL, VACANCY, build_pool, start_execution


@pytest.fixture
def svc():
    return Service(Store(os.path.join(tempfile.mkdtemp(), "t.db")))


def _run_to_completion(svc, ids):
    """模拟所有人：生成方案 → 全员同意 → 谁能动谁就动，直到完成。"""
    anyone = next(iter(ids.values()))
    svc.make_plan(anyone)
    for mid in ids.values():
        svc.approve(mid)
    for _ in range(100):
        view = svc.pool_view(anyone)
        if view["pool"]["state"] == "done":
            return view
        did = False
        for mid in ids.values():
            v = svc.pool_view(mid)
            if v["my_action"]:
                svc.act(mid, v["my_action"])
                did = True
                break
        assert did, "没有任何人可以行动"
    raise AssertionError("没有在 100 步内完成")


@pytest.mark.parametrize("spec", [VACANCY, FULL], ids=["vacancy", "full"])
def test_pools_finish(svc, spec):
    ids = build_pool(svc, spec)
    view = _run_to_completion(svc, ids)
    final = {r["name"]: r["to"] for rows in view["new_groups"].values() for r in rows}
    for team in spec["teams"]:
        assert len({final[p] for p in team}) == 1, f"小队没有凑到一起：{team}"
    for rows in view["new_groups"].values():
        assert len(rows) <= 5
    assert {m["grp"] for m in svc.identities()} <= set(spec["groups"])


def test_full_pool_starts_with_leaves(svc):
    ids = build_pool(svc, FULL)
    start_execution(svc, ids)
    actions = {n: svc.pool_view(mid)["my_action"] for n, mid in ids.items()}
    assert set(filter(None, actions.values())) == {"leave"}   # 全满：每组交换都要先有人退出


def test_group_cannot_join_two_pools_and_counts_are_enforced(svc):
    a = svc.current(svc.register("a", "A"))["id"]
    b = svc.current(svc.register("b", "A"))["id"]
    c = svc.current(svc.register("c", "A"))["id"]
    svc.create_pool(a, 2)
    with pytest.raises(UserError, match="另一个池"):
        svc.create_pool(b, 5)
    code = svc.pool_view(a)["pool"]["code"]
    svc.join_pool(b, code, 5)
    with pytest.raises(UserError, match="检查你的组号"):
        svc.join_pool(c, code, 5)


def test_concurrent_acts_never_overfill(svc):
    """全员同时点"完成"：事务保证只有被分配到空位的人能成功。"""
    ids = build_pool(svc, VACANCY)
    start_execution(svc, ids)

    def try_all(action):
        def go(mid):
            try:
                svc.act(mid, action)
                return True
            except UserError:
                return False
        with ThreadPoolExecutor(16) as ex:
            return sum(ex.map(go, ids.values()))

    allowed = sum(1 for mid in ids.values() if svc.pool_view(mid)["my_action"] == "move")
    # 先成功的人会腾出新空位，后面的人可能随之合法，所以成功数 ≥ 起初可动的人数
    assert try_all("move") >= allowed
    counts = svc.pool_view(ids["N1"])["live_counts"]
    assert max(counts.values()) <= 5
    assert sum(counts.values()) + sum(1 for m in svc.identities() if m["grp"] is None) == len(ids)


def test_http_flow_and_live_polling():
    from app.web import app, svc as web_svc

    web_svc.purge()
    client = TestClient(app)
    r = client.post("/register", data={"nickname": "N1", "grp": "N"}, follow_redirects=False)
    assert r.status_code == 303 and "gst" in r.cookies
    client.cookies = r.cookies
    assert "创建意向组" in client.get("/").text and "创建池" not in client.get("/").text  # 旧的整组池入口不再显示
    client.post("/pool/create", data={"lm_count": 2})
    page = client.get("/pool")
    assert "邀请" in page.text and "N 还差 1 人" in page.text and "data-copy" in page.text
    rev = int(page.text.split('data-rev="')[1].split('"')[0])
    assert client.get(f"/pool/live?rev={rev}").status_code == 204
    client.post("/pool/leave")
    assert client.get(f"/pool/live?rev={rev}").status_code == 410
    assert client.get("/admin").status_code == 200


def test_wishes_open_only_after_all_groups_are_complete(svc):
    ids = {}
    for g in "ABC":
        for i in (1, 2):
            ids[f"{g}{i}"] = svc.current(svc.register(f"{g}{i}", g))["id"]
    code = svc.create_pool(ids["A1"], 2)
    for n in ("A2", "B1", "B2", "C1"):
        svc.join_pool(ids[n], code, 2)               # A、B 到齐，C 还差 1 人
    assert not svc.pool_view(ids["A1"])["wishes_open"]
    with pytest.raises(UserError, match="全员到齐后才开放互选"):
        svc.set_wish(ids["A1"], ids["B1"], True)
    svc.join_pool(ids["C2"], code, 2)                # 全部到齐 → 开放
    assert svc.pool_view(ids["A1"])["wishes_open"]
    svc.set_wish(ids["A1"], ids["B1"], True)
    svc.set_wish(ids["B1"], ids["A1"], True)
    assert sorted(svc.pool_view(ids["A1"])["my_team"]) == ["A1", "B1"]
