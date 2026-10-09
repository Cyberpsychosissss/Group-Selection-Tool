"""限流、一台设备一个代号、代号唯一、口令锁定、真实 IP 识别。"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import config
from app.ratelimit import RateLimiter
from app.services import Service, UserError
from app.store import Store
from app.web import _client_ip, app


def _client():
    return TestClient(app)


def _register(client, nickname, grp="A"):
    return client.post("/register", data={"nickname": nickname, "grp": grp}, follow_redirects=False)


# ---------- 限流器 ----------

def test_sliding_window():
    now = [0.0]
    rl = RateLimiter(clock=lambda: now[0])
    assert all(rl.allow("k", 3, 60) for _ in range(3))
    assert not rl.allow("k", 3, 60)
    now[0] = 61
    assert rl.allow("k", 3, 60)


# ---------- 代号 ----------

def test_one_codename_per_device():
    c = _client()
    r = _register(c, "U1")
    assert r.status_code == 303 and "gst" in r.cookies
    c.cookies.delete("gst")                     # 清掉身份，但设备还是同一台
    r = _register(c, "U2")
    assert "gst" not in r.cookies and "err=" in r.headers["location"]
    assert _register(_client(), "U2").status_code == 303   # 换一台设备可以


def test_logged_in_device_cannot_register_again():
    c = _client()
    _register(c, "U3")
    r = _register(c, "U4")
    assert "err=" in r.headers["location"]


def test_codename_unique_case_insensitive():
    svc = Service(Store(os.path.join(tempfile.mkdtemp(), "t.db")))
    svc.register("Abc1", "A")
    with pytest.raises(UserError, match="已被使用"):
        svc.register(" abc1 ", "B")


def test_concurrent_same_codename_only_one_wins():
    svc = Service(Store(os.path.join(tempfile.mkdtemp(), "t.db")))

    def go(i):
        try:
            svc.register("same-code", "A", device=f"dev{i:020d}")
            return True
        except UserError:
            return False

    with ThreadPoolExecutor(16) as ex:
        assert sum(ex.map(go, range(32))) == 1


# ---------- 限流 ----------

def test_device_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "RL_DEVICE", (5, 60))
    c = _client()
    codes = [c.get("/guide").status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429
    assert _client().get("/guide").status_code == 200     # 别的设备不受影响


def test_polling_gets_plain_429(monkeypatch):
    monkeypatch.setattr(config, "RL_DEVICE", (1, 60))
    c = _client()
    c.get("/guide")
    assert c.get("/pool/live").status_code == 429


def test_register_limit_per_ip(monkeypatch):
    monkeypatch.setattr(config, "RL_REGISTER_IP", (2, 600))
    results = [_register(_client(), f"ip{i}").status_code for i in range(3)]   # 3 台设备、同一 IP
    assert results == [303, 303, 429]


def test_gate_locks_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(config, "ACCESS_CODE", "test-Passcode1")
    monkeypatch.setattr(config, "RL_GATE_FAIL", (3, 600))
    c = _client()
    c.get("/gate")                                              # 拿到设备 Cookie
    for _ in range(3):
        c.post("/gate", data={"code": "guess", "next": "/"}, follow_redirects=False)
    r = c.post("/gate", data={"code": "test-Passcode1", "next": "/"}, follow_redirects=False)
    assert "gst_gate" not in r.cookies and "err=" in r.headers["location"]   # 锁定期间对的也不行
    r = _client().post("/gate", data={"code": "TEST-passcode1", "next": "/"}, follow_redirects=False)
    assert "gst_gate" in r.cookies                              # 其他设备不受影响，不区分大小写


# ---------- 真实 IP ----------

def _req(host, headers=()):
    return Request({"type": "http", "client": (host, 1), "headers": [(k.encode(), v.encode()) for k, v in headers]})


def test_client_ip(monkeypatch):
    assert _client_ip(_req("127.0.0.1")) is None                # 代理后面拿不到真实 IP：不按 IP 限流
    assert _client_ip(_req("10.1.2.3")) == "10.1.2.3"
    fwd = [("x-forwarded-for", "1.2.3.4, 127.0.0.1")]
    assert _client_ip(_req("127.0.0.1", fwd)) is None           # 默认不信任转发头（防伪造）
    monkeypatch.setattr(config, "TRUST_PROXY", True)
    assert _client_ip(_req("127.0.0.1", fwd)) == "1.2.3.4"
