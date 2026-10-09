"""访问口令：不知道口令的人进不来，也不能伪造 Cookie 绕过。"""
import pytest
from fastapi.testclient import TestClient

from app import config
from app.web import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "ACCESS_CODE", "test-code")
    return TestClient(app)


def test_everything_is_behind_the_gate(client):
    for path in ["/", "/guide", "/pool", "/admin", "/j/ABCDEFGH", "/login/whatever"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/gate"), path
    assert client.get("/pool/live").status_code == 401
    assert client.post("/register", data={"nickname": "x", "grp": "A"}, follow_redirects=False).status_code == 303
    assert client.get("/static/app.css").status_code == 200       # 静态文件不拦


def test_wrong_code_and_forged_cookie_are_rejected(client):
    r = client.post("/gate", data={"code": "wrong", "next": "/"}, follow_redirects=False)
    assert "gst_gate" not in r.cookies and "err=" in r.headers["location"]
    r = client.post("/gate", data={"code": "口令", "next": "/"}, follow_redirects=False)  # 中文输入不报错
    assert r.status_code == 303 and "gst_gate" not in r.cookies
    client.cookies.set("gst_gate", "0" * 64)
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/gate")


def test_right_code_lets_you_in_and_blocks_open_redirect(client):
    r = client.post("/gate", data={"code": " TEST-CODE ", "next": "//evil.example"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    client.cookies = r.cookies
    assert client.get("/guide").status_code == 200


def test_changing_the_code_invalidates_old_cookies(client, monkeypatch):
    r = client.post("/gate", data={"code": "test-code", "next": "/"}, follow_redirects=False)
    client.cookies = r.cookies
    monkeypatch.setattr(config, "ACCESS_CODE", "newcode")
    assert client.get("/guide", follow_redirects=False).status_code == 303


def test_gate_off_when_no_code(monkeypatch):
    monkeypatch.setattr(config, "ACCESS_CODE", "")
    assert TestClient(app).get("/guide").status_code == 200
