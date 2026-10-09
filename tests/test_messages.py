"""一对一临时对话：只能发预设短语，只在内存里，不进数据库，第三人看不到。"""
import json
import os
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.chat import Chat
from app.services import Service, UserError
from app.store import Store
from tests.helpers import VACANCY, build_pool, start_execution


def test_chat_is_one_to_one_and_expires():
    now = [0.0]
    chat = Chat(ttl=60, clock=lambda: now[0])
    ab = chat.open("pool", "a", "b")
    chat.post(ab, "a", "A1", "准备好了。")
    chat.post(ab, "c", "C1", "我不在这个对话里")          # 非成员发不进去
    assert [(m["text"], m["mine"]) for m in chat.since("b")] == [("准备好了。", False)]
    assert [m["mine"] for m in chat.since("a")] == [True]
    assert chat.since("c") == []                           # 第三人看不到
    last = chat.since("b")[-1]["id"]
    assert chat.since("b", after=last) == []               # 增量拉取
    now[0] = 61
    assert chat.since("b") == []                           # 过期清除
    chat.close_pool("pool")
    assert chat.since("a") == []


@pytest.fixture
def executing():
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    svc = Service(Store(db), Chat())
    ids = build_pool(svc, VACANCY)
    start_execution(svc, ids)
    return svc, ids, db


def test_step_completion_notifies_only_the_swap_partner(executing):
    svc, ids, _ = executing
    names = {v: k for k, v in ids.items()}
    actor = next(m for m in ids.values()
                 if svc.pool_view(m)["my_action"] and svc.pool_view(m)["mine"]["partner"])
    v = svc.pool_view(actor)
    partner = v["mine"]["partner"]
    svc.act(actor, v["my_action"], v["my_seq"])
    texts = [m["text"] for m in svc.chat_since(ids[partner], 0)]
    assert len(texts) == 1 and texts[0].startswith(("我已从", "我已退出", "我已加入"))
    assert "轮到你" in texts[0] or "完成" in texts[0]
    for name, mid in ids.items():                           # 其他人什么都收不到
        if name not in (partner, names[actor]):
            assert svc.chat_since(mid, 0) == []


def test_everyone_has_at_most_one_chat(executing):
    svc, ids, _ = executing
    assert all(len(svc.chat_peers(m)) <= 1 for m in ids.values())
    solo = [m for m in ids.values() if svc.pool_view(m)["mine"]["moved"] and not svc.pool_view(m)["mine"]["partner"]]
    assert all(svc.chat_peers(m) == [] for m in solo)       # 单独进空位的人没有对话


def test_quick_phrases_only_preset_and_only_own_pairs(executing):
    svc, ids, db = executing
    mover = next(m for m in ids.values() if svc.pool_view(m)["mine"]["partner"])
    stayer = next(m for m in ids.values() if not svc.pool_view(m)["mine"]["moved"])
    assert svc.chat_peers(stayer) == []
    pair, peer = svc.chat_peers(mover)[0]
    with pytest.raises(UserError):
        svc.send_quick(mover, "随便写的内容", pair)          # 只能用预设短语
    with pytest.raises(UserError):
        svc.send_quick(stayer, "ready", pair)                # 不能往别人的对话里发
    svc.send_quick(mover, "ready", pair)
    assert [m["text"] for m in svc.chat_since(ids[peer], 0)] == ["准备好了。"]
    assert svc.chat_since(stayer, 0) == []
    with sqlite3.connect(db) as c:                          # 消息内容不进数据库
        assert "准备好了" not in "\n".join(c.iterdump())


def test_polling_returns_chats_and_new_messages():
    from app.web import app, svc as web_svc

    web_svc.purge()
    ids = build_pool(web_svc, VACANCY)
    web_svc.make_plan(ids["N1"])
    mover = next(n for n, m in ids.items() if web_svc.pool_view(m)["mine"]["partner"])
    client = TestClient(app)
    client.cookies.set("gst", web_svc.login_as(ids[mover]))
    r = client.get("/pool/live?rev=-1")
    chats = json.loads(r.headers["x-chats"])
    assert chats and all(set(c) == {"pair", "peer"} for c in chats)
    assert client.post("/pool/msg", data={"kind": "ok", "pair": chats[0]["pair"]}).status_code == 204
    assert client.post("/pool/msg", data={"kind": "hello", "pair": chats[0]["pair"]}).status_code == 400
    msgs = json.loads(client.get("/pool/live?rev=-1&m=0").headers["x-msg"])
    assert msgs[-1]["mine"] and msgs[-1]["text"] == "收到。"
    assert "x-msg" not in client.get(f"/pool/live?rev=-1&m={msgs[-1]['id']}").headers
