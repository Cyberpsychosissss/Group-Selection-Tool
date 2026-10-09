# ISA302 换组助手（MVP）

几个组的全部成员组成一个**重组池**：池里的人互相勾选想同组的人 → 系统算出方案 → 全员同意 →
按页面提示，轮到谁谁就去 LM Core 上操作。只在池内的组之间调人，不碰 LM Core，也不收学号和密码。

## 本地运行

```bash
./run.sh
```

打开 <http://127.0.0.1:8000>。`run.sh` 以开发模式启动：页面顶部有身份切换栏，可以新建身份、一键切换，方便一个人测试多个账号。不设 `GST_DEV=1` 时（正式模式）没有这个栏。

## 同时开多个身份

- 顶部开发栏：一键切换（同一个浏览器里同时只能有一个身份）。
- 想**并排**看两个人：一个窗口用 `http://127.0.0.1:8000`，另一个用 `http://localhost:8000`，两者的 Cookie 互不影响；也可以再开一个隐私窗口。

## 正式模式（上公网时）

```bash
GST_DEV=0 GST_ACCESS_CODE=<口令> GST_ADMIN_KEY=<长随机串> .venv/bin/uvicorn app.web:app --host 127.0.0.1 --port 8000 --workers 1
```

- 只开 1 个 worker：一对一对话和限流计数只在进程内存里（不存储），多个 worker 之间互相看不到。
- `GST_ACCESS_CODE`：访问口令，所有页面都要先输入。不设置就不启用。
- `GST_DEV=0`：关掉身份切换栏，Cookie 加上 Secure（必须走 HTTPS）。
- `GST_ADMIN_KEY`：管理页需要 `/admin?key=…`。
- `GST_TRUST_PROXY=1`：前面有 Caddy / nginx 等会加 `X-Forwarded-For` 的反向代理时才打开，用于按真实 IP 限流。

本地想试试口令页的效果：

```bash
GST_ACCESS_CODE=<口令> ./run.sh
```

## 测试

```bash
.venv/bin/python -m pytest -q
```

## 清空数据

打开 `/admin` 页面，输入“删除”后点按钮；或者直接删除 `data/` 目录。

## 代码结构

```
app/
  domain/          纯逻辑，不做 IO
    teams.py       互选 → 小队
    solver.py      池内重组方案
    pairing.py     拆成两人一组的交换（谁和谁换）
    execution.py   此刻谁该在 LM Core 上做什么
  chat.py          一对一临时对话（只在内存里）
  ratelimit.py     按设备 / IP 限流
  store.py         唯一写 SQL 的地方（SQLite WAL）
  services.py      用例编排
  web.py           路由（FastAPI）
  templates/ static/
tests/
```

依赖方向：`web → services → domain / store`。
