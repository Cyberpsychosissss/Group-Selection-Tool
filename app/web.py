"""Web 层：只做参数解析、身份识别和页面渲染，所有业务逻辑都在 services 里。"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config
from .chat import Chat
from .ratelimit import RateLimiter
from .services import QUICK_NOTES, Service, UserError
from .store import Store

BASE = Path(__file__).parent
COOKIE = "gst"  # 身份
GATE_COOKIE = "gst_gate"  # 口令已通过
DEVICE_COOKIE = "gst_dev"  # 设备标识（限流、一台设备一个代号）
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_YEAR = 60 * 60 * 24 * 365

app = FastAPI(title="ISA302 换组助手", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
svc = Service(Store(config.DB_PATH), Chat())
limiter = RateLimiter()


# ---------------------------------------------------------------- 守卫：设备标识 → 限流 → 访问口令

def _gate_value() -> str:
    """口令通过后发放的 Cookie 值：用服务器密钥签名，客户端无法伪造；换口令后旧 Cookie 自动失效。"""
    return hmac.new(config.SECRET, b"gate:" + config.ACCESS_CODE.encode(), hashlib.sha256).hexdigest()


def _client_ip(request: Request) -> str | None:
    """真实客户端 IP。拿不到（经代理但没开 TRUST_PROXY 时全是 127.0.0.1）就返回 None，不按 IP 限流。"""
    if config.TRUST_PROXY and (fwd := request.headers.get("x-forwarded-for")):
        return fwd.split(",")[0].strip()
    host = request.client.host if request.client else None
    try:
        return None if host is None or ipaddress.ip_address(host).is_loopback else host
    except ValueError:
        return host


def _limits(request: Request, device: str, ip: str | None) -> list[tuple[str, int, int]]:
    write = request.method == "POST"
    rules = [(f"d:{device}", *config.RL_DEVICE)]
    if write:
        rules.append((f"dw:{device}", *config.RL_DEVICE_WRITE))
    if ip:
        rules.append((f"i:{ip}", *config.RL_IP))
        if write and request.url.path == "/register":
            rules.append((f"ir:{ip}", *config.RL_REGISTER_IP))
    return rules


def _notice(request: Request, title: str, msg: str, status: int) -> HTMLResponse:
    return templates.TemplateResponse(request, "busy.html", {"title": title, "msg": msg}, status_code=status)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static/"):
        return await call_next(request)

    device = request.cookies.get(DEVICE_COOKIE, "")
    new_device = not _DEVICE_RE.match(device)
    if new_device:
        device = secrets.token_urlsafe(18)
    request.state.device = device
    ip = _client_ip(request)
    request.state.ip = ip

    if not all(limiter.allow(key, n, window) for key, n, window in _limits(request, device, ip)):
        resp = (Response(status_code=429) if path == "/pool/live"
                else _notice(request, "操作太频繁", "请稍等一分钟再试。", 429))
    elif (config.ACCESS_CODE and path != "/gate"
          and not hmac.compare_digest(request.cookies.get(GATE_COOKIE, "").encode(), _gate_value().encode())):
        resp = Response(status_code=401) if path == "/pool/live" else RedirectResponse(
            f"/gate?next={quote(path)}", status_code=303)
    else:
        resp = await call_next(request)

    if new_device:
        resp.set_cookie(DEVICE_COOKIE, device, httponly=True, samesite="lax", secure=not config.DEV_MODE, max_age=_YEAR)
    return resp


@app.get("/gate", response_class=HTMLResponse)
def gate_page(request: Request, next: str = "/"):
    return _render(request, "gate.html", me=None, next=next)


@app.post("/gate")
async def gate_submit(request: Request, code: str = Form(""), next: str = Form("/")):
    if not next.startswith("/") or next.startswith("//"):  # 只允许站内跳转
        next = "/"
    n, window = config.RL_GATE_FAIL
    keys = [f"gf:{request.state.device}"] + ([f"gfi:{request.state.ip}"] if request.state.ip else [])
    if any(limiter.count(k, window) >= n * (10 if k.startswith("gfi:") else 1) for k in keys):
        return _back(f"/gate?next={quote(next)}", "输错次数过多，请 10 分钟后再试")
    if not config.ACCESS_CODE or not hmac.compare_digest(code.strip().lower().encode(), config.ACCESS_CODE.lower().encode()):
        for k in keys:
            limiter.hit(k)
        await asyncio.sleep(1)  # 输错延迟 1 秒，增加暴力猜测成本
        return _back(f"/gate?next={quote(next)}", "口令不对")
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie(GATE_COOKIE, _gate_value(), httponly=True, samesite="lax",
                    secure=not config.DEV_MODE, max_age=60 * 60 * 24 * 30)
    return resp


@app.exception_handler(sqlite3.OperationalError)
def db_busy(request: Request, exc: sqlite3.OperationalError):
    """写锁排队超时（极端高峰）时，给用户一个可以重试的提示，而不是 500。"""
    return _notice(request, "请求过多", "操作未生效，请稍后重试。", 503)


# ---------------------------------------------------------------- 工具函数

def _me(request: Request):
    return svc.current(request.cookies.get(COOKIE))


def _back(path: str = "/", err: str | None = None) -> RedirectResponse:
    url = f"{path}?err={quote(err)}" if err else path
    return RedirectResponse(url, status_code=303)


def _login(token: str, path: str = "/") -> RedirectResponse:
    resp = RedirectResponse(path, status_code=303)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=not config.DEV_MODE, max_age=60 * 60 * 24 * 30)
    return resp


def _asset_version() -> int:
    """静态文件版本号（取修改时间），改了 CSS/JS 后浏览器不会用旧缓存。"""
    return int(max(p.stat().st_mtime for p in (BASE / "static").iterdir()))


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("me", _me(request))
    ctx.update(
        asset_v=_asset_version(),
        err=request.query_params.get("err"),
        dev=config.DEV_MODE,
        identities=svc.identities() if config.DEV_MODE else [],
        groups=config.GROUPS,
        deadline=config.DEADLINE,
        lmcore_url=config.LMCORE_URL,
    )
    return templates.TemplateResponse(request, name, ctx)


def _run(fn, *args, ok: str = "/", fail: str = "/") -> RedirectResponse:
    try:
        fn(*args)
    except UserError as e:
        return _back(fail, str(e))
    return _back(ok)


# ---------------------------------------------------------------- 页面

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    me = _me(request)
    if me and me["pool_id"]:
        return _back("/pool", request.query_params.get("err"))
    return _render(request, "home.html", me=me, invite=None)


@app.get("/j/{code}", response_class=HTMLResponse)
def invite(request: Request, code: str):
    """邀请链接：没登记的人在这一页同时完成登记和加入。"""
    me = _me(request)
    if me and me["pool_id"]:
        return _back("/pool")
    if not svc.pool_exists(code):
        return _back("/", "邀请链接无效，可能池已经被删除")
    return _render(request, "home.html", me=me, invite=code.upper())


@app.get("/guide", response_class=HTMLResponse)
def guide(request: Request):
    return _render(request, "guide.html")


@app.get("/pool", response_class=HTMLResponse)
def pool_page(request: Request):
    me = _me(request)
    view = svc.pool_view(me["id"]) if me else None
    if not view:
        return _back("/")
    return _render(request, "pool.html", me=me, v=view, chats=svc.chat_peers(me["id"]),
                   quick=[(k, label) for k, (label, _) in QUICK_NOTES.items()])


@app.get("/pool/live")
def pool_live(request: Request, rev: int = -1, m: int = 0):
    """轮询接口：rev 没变直接 204（几乎零开销）；变了才返回新的页面片段。
    一对一对话的新消息放在响应头 X-Msg 里（m = 已收到的最后一条消息 id）；对话列表变化时放在 X-Chats 里。"""
    me = _me(request)
    current = svc.pool_rev(me["id"]) if me else None
    if current is None:
        return Response(status_code=410)
    if current == rev:
        resp = Response(status_code=204)
    else:
        view = svc.pool_view(me["id"])
        resp = templates.TemplateResponse(request, "_pool_body.html", {"me": me, "v": view})
        resp.headers["X-Rev"] = str(current)
        chats = svc.chat_peers(me["id"])  # 方案生成 / 退回 / 完成时对话会变
        resp.headers["X-Chats"] = json.dumps([{"pair": p, "peer": name} for p, name in chats])
    if msgs := svc.chat_since(me["id"], m):
        resp.headers["X-Msg"] = json.dumps(msgs)
    return resp


# ---------------------------------------------------------------- 身份

@app.post("/register")
def register(request: Request, nickname: str = Form(...), grp: str = Form(...), code: str = Form(""),
             lm_count: int = Form(5)):
    if me := _me(request):
        return _back("/", f"你已登记为“{me['nickname']}”")
    try:
        token = svc.register(nickname, grp, device=request.state.device)
    except UserError as e:
        return _back(f"/j/{code}" if code else "/", str(e))
    if not code:
        return _login(token)
    me = svc.current(token)
    try:
        svc.join_pool(me["id"], code, lm_count)
    except UserError as e:
        return _login(token, f"/?err={quote(str(e))}")
    return _login(token, "/pool")


@app.post("/me/group")
def change_group(request: Request, grp: str = Form(...)):
    me = _me(request)
    return _run(svc.change_group, me["id"], grp) if me else _back("/")


@app.get("/login/{token}")
def login(token: str):
    """恢复链接：换设备或清了 Cookie 时用。"""
    return _login(token) if svc.current(token) else _back("/", "链接无效")


@app.get("/logout")
def logout():
    resp = _back("/")
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- 池

def _pool_action(request: Request, fn, *args):
    me = _me(request)
    if not me:
        return _back("/")
    return _run(fn, me["id"], *args, ok="/pool", fail="/pool")


@app.post("/pool/create")
def pool_create(request: Request, lm_count: int = Form(5)):
    me = _me(request)
    return _run(svc.create_pool, me["id"], lm_count, ok="/pool") if me else _back("/")


@app.post("/pool/join")
def pool_join(request: Request, code: str = Form(...), lm_count: int = Form(5)):
    me = _me(request)
    return _run(svc.join_pool, me["id"], code, lm_count, ok="/pool", fail=f"/j/{code}") if me else _back("/")


@app.post("/pool/leave")
def pool_leave(request: Request):
    me = _me(request)
    return _run(svc.leave_pool, me["id"], ok="/", fail="/pool") if me else _back("/")


@app.post("/pool/wish")
def pool_wish(request: Request, to_id: str = Form(...), want: int = Form(...)):
    return _pool_action(request, svc.set_wish, to_id, bool(want))


@app.post("/pool/plan")
def pool_plan(request: Request):
    return _pool_action(request, svc.make_plan)


@app.post("/pool/approve")
def pool_approve(request: Request):
    return _pool_action(request, svc.approve)


@app.post("/pool/reject")
def pool_reject(request: Request):
    return _pool_action(request, svc.reject)


@app.post("/pool/act")
def pool_act(request: Request, action: str = Form(...), seq: int | None = Form(None)):
    return _pool_action(request, svc.act, action, seq)


@app.post("/pool/msg")
def pool_msg(request: Request, kind: str = Form(...), pair: str = Form(...)):
    """一对一对话里发预设短语（只能选预设的 kind，不接受自定义文本）。页面里用 fetch 发送，返回 204。"""
    me = _me(request)
    if not me:
        return Response(status_code=401)
    try:
        svc.send_quick(me["id"], kind, pair)
    except UserError as e:
        return Response(str(e), status_code=400, media_type="text/plain; charset=utf-8")
    return Response(status_code=204)


# ---------------------------------------------------------------- 开发模式 / 管理

if config.DEV_MODE:

    @app.get("/dev/as/{member_id}")
    def dev_switch(member_id: str):
        try:
            return _login(svc.login_as(member_id))
        except UserError as e:
            return _back("/", str(e))

    @app.post("/dev/new")
    def dev_new(nickname: str = Form(...), grp: str = Form(...)):
        try:
            return _login(svc.register(nickname, grp))
        except UserError as e:
            return _back("/", str(e))


def _admin_ok(request: Request) -> bool:
    return config.DEV_MODE or (config.ADMIN_KEY and request.query_params.get("key") == config.ADMIN_KEY)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    if not _admin_ok(request):
        return Response(status_code=404)
    return _render(request, "admin.html", stats=svc.stats(), key=request.query_params.get("key", ""))


@app.post("/admin/purge")
def admin_purge(request: Request, confirm: str = Form("")):
    if not _admin_ok(request):
        return Response(status_code=404)
    if confirm != "删除":
        return _back("/admin", "请输入“删除”二字确认")
    svc.purge()
    resp = _back("/")
    resp.delete_cookie(COOKIE)
    return resp
