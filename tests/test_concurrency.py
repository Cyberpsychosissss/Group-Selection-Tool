"""并发与幂等：双击、重复提交、多线程、多进程同时写。"""
import multiprocessing as mp
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services import Service, UserError
from app.store import Store
from tests.helpers import VACANCY, build_pool


@pytest.fixture
def svc():
    return Service(Store(os.path.join(tempfile.mkdtemp(), "t.db")))


def _parallel(fn, args, workers=16):
    def safe(a):
        try:
            fn(*a)
            return "ok"
        except UserError as e:
            return str(e)
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(safe, args))


def test_double_submits_are_harmless(svc):
    ids = build_pool(svc, VACANCY)
    a, b = ids["N1"], ids["N2"]
    for _ in range(3):                      # 连点三次"选"（已经选过）
        svc.set_wish(a, b, True)
    assert "N2" in svc.pool_view(a)["my_team"]
    svc.make_plan(a)
    svc.make_plan(a)                        # 重复生成：忽略
    for mid in ids.values():
        svc.approve(mid)
        svc.approve(mid)                    # 重复同意：忽略
    events = [e["text"] for e in svc.pool_view(a)["events"]]
    assert sum(t.endswith(" 同意") for t in events) == len(ids)
    mover = next(m for m in ids.values() if svc.pool_view(m)["my_action"])
    v = svc.pool_view(mover)
    svc.act(mover, v["my_action"], v["my_seq"])
    svc.act(mover, v["my_action"], v["my_seq"])   # 重复确认（同一个页面点两次）：忽略，不报错
    assert sum("→" in e["text"] for e in svc.pool_view(a)["events"]) == 1


def test_concurrent_approvals_start_execution_exactly_once(svc):
    ids = build_pool(svc, VACANCY)
    svc.make_plan(ids["N1"])
    results = _parallel(svc.approve, [(m,) for m in ids.values()] * 2)   # 每人点两次，全部并发
    assert set(results) == {"ok"}
    v = svc.pool_view(ids["N1"])
    assert v["pool"]["state"] == "executing"
    assert sum("全员同意" in e["text"] for e in v["events"]) == 1


def test_reject_and_approve_race(svc):
    ids = build_pool(svc, VACANCY)
    svc.make_plan(ids["N1"])
    calls = [(svc.approve, m) for m in ids.values()] + [(svc.reject, ids["N5"])]
    _parallel(lambda f, m: f(m), calls)
    state = svc.pool_view(ids["N1"])["pool"]["state"]
    assert state in ("open", "executing")   # 要么被退回，要么全员同意；不会卡在中间
    if state == "open":
        assert not svc.pool_view(ids["N1"]).get("new_groups")


def test_same_group_cannot_end_up_in_two_pools(svc):
    members = [svc.current(svc.register(f"m{i}", "Q"))["id"] for i in range(10)]
    results = _parallel(svc.create_pool, [(m, 5) for m in members])
    pools = {svc.pool_view(m)["pool"]["id"] for m in members if svc.pool_view(m)}
    assert len(pools) == 1 and results.count("ok") == 1


def _register_many(db, start):
    svc = Service(Store(db))
    for i in range(200):
        svc.register(f"p{start + i}", "A")


def test_multi_process_writes_share_one_database():
    """模拟 uvicorn --workers 4：4 个进程同时写同一个 SQLite 文件。"""
    db = os.path.join(tempfile.mkdtemp(), "mp.db")
    Store(db)
    procs = [mp.get_context("spawn").Process(target=_register_many, args=(db, k * 1000)) for k in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    assert len(Service(Store(db)).identities()) == 800
