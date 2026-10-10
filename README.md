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
GST_DEV=0 GST_ACCESS_CODE=<口令> GST_ADMIN_KEY=<长随机串> GST_TRUST_PROXY=1 GST_PROXY_HOPS=1 \
  .venv/bin/uvicorn app.web:app --host 127.0.0.1 --port 8000 --workers 1 --forwarded-allow-ips '*'
```

- 只开 1 个 worker：一对一对话和限流计数只在进程内存里（不存储），多个 worker 之间互相看不到。
- `GST_ACCESS_CODE`：访问口令，所有页面都要先输入。不设置就不启用。建议用一串不在字典里的字符（10 位以上）：
  按 IP 的保护只是每分钟限速（校园网大家共用出口 IP，不能一锁锁全校），真正挡猜测的是口令本身。
- `GST_DEV=0`：关掉身份切换栏，Cookie 加上 Secure（必须走 HTTPS）。
- `GST_ADMIN_KEY`：管理页需要 `/admin?key=…`。管理页可以按池删除，也可以清空全部数据。
- `GST_TRUST_PROXY=1`：前面有 Caddy / nginx 等反向代理时打开。作用有两个：按 `X-Forwarded-For` 里的真实 IP 限流；
  按 `X-Forwarded-Proto` / `X-Forwarded-Host` 生成邀请链接和恢复链接（否则链接会是 `http://`）。
- `GST_PROXY_HOPS`：前面有几层会往 `X-Forwarded-For` **追加**地址的代理，真实 IP 取从右数第几个（默认 1）。
  单层 nginx / Caddy 填 1；"VPS nginx → frp → 本机 Nginx Proxy Manager" 这种两层追加填 2。取最左边的值会被客户端伪造。
- `--forwarded-allow-ips`：uvicorn 默认只信任 127.0.0.1 发来的代理头；代理不在本机（比如 Docker 网桥 172.17.0.1）时要加上它的地址或 `'*'`。
- 绑定地址：代理在 Docker 里时要让容器能连到，`--host` 改成 `0.0.0.0` 或网桥地址（如 `172.17.0.1`）。

本地想试试口令页的效果：

```bash
GST_ACCESS_CODE=<口令> ./run.sh
```

## 流程里的几个"后门"

- **本组人数填错**：互选阶段本组任何人可以在池页面更正（不能少于已加入的人数）。
- **组里有人不肯加入**：本组任何人可以添加"占位"代表他。占位占着位置、不参与互选和确认、也不会被移动。
- **执行中出了问题**（有人操作错、失联、LM Core 上目标组被池外的人占了）：任何人可以"中止执行"退回互选。
  方案作废、互选保留，各组人数按此刻实际情况重算；已经退出组的人要先在 LM Core 加入池内某个组，再在池页面更新组号。
- **完成后**：各组自动释放，成员退出池后可以创建或加入新的池再换一轮。

## 测试

```bash
.venv/bin/python -m pytest -q
```

## 清空数据

打开 `/admin` 页面：可以按池删除，或输入"删除"后清空全部；也可以直接删除 `data/` 目录。

## 代码结构

```
app/
  domain/          纯逻辑，不做 IO
    teams.py       互选 → 小队
    solver.py      池内重组方案（动态规划精确求最少移动；组很多时退回近似解）
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
