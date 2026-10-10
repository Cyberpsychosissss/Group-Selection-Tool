"""全局配置：全部来自环境变量，便于本地 / 树莓派 / 服务器共用一份代码。"""
import os
import secrets
import string
import time
from pathlib import Path


def _group_labels(last: str = "CK") -> list[str]:
    """LM Core 上的组名：A…Z, AA…AZ, BA…BZ, CA…CK。"""
    letters = string.ascii_uppercase
    labels = list(letters) + [a + b for a in letters for b in letters]
    return labels[: labels.index(last) + 1]


DB_PATH = os.environ.get("GST_DB", "data/app.db")
DEV_MODE = os.environ.get("GST_DEV", "0") == "1"  # 开发模式：显示身份切换栏（默认关闭，run.sh 本地启动时打开）
ADMIN_KEY = os.environ.get("GST_ADMIN_KEY", "")  # 非开发模式下进入 /admin 需要 ?key=
CAPACITY = 5
GROUPS = _group_labels(os.environ.get("GST_LAST_GROUP", "CK"))
LMCORE_URL = "https://core.xjtlu.edu.cn/mod/choicegroup/view.php?id=41683"
DEADLINE = "2026-10-18 17:00"

# 访问口令：正式上线时设置（在课程群里公布），不知道口令的人进不来；为空表示不启用（本地开发）
ACCESS_CODE = os.environ.get("GST_ACCESS_CODE", "")

# 反向代理（Caddy / nginx / frp http 模式）会在 X-Forwarded-For 里带上真实 IP；只有确认前面有代理时才打开。
# 打开后同时用 X-Forwarded-Proto 生成邀请链接 / 恢复链接的协议（否则 uvicorn 只信任 127.0.0.1 来的代理头，
# 从 Docker 网桥等地址进来的请求会把链接生成成 http://）。
TRUST_PROXY = os.environ.get("GST_TRUST_PROXY", "0") == "1"
# 前面有几层会往 X-Forwarded-For 追加地址的代理：真实 IP 取从右数第几个。
# 单层 nginx/Caddy 是 1；VPS nginx → frp → 本机 NPM 这种两层追加是 2。取最左边的值会被客户端伪造。
PROXY_HOPS = max(1, int(os.environ.get("GST_PROXY_HOPS", "1")))

# 限流：(次数, 秒)。进程内计数，多 worker 时每个进程各算各的
RL_DEVICE = (120, 60)  # 每台设备每分钟所有请求（页面轮询约 24 次/分钟）
RL_DEVICE_WRITE = (30, 60)  # 每台设备每分钟提交操作
RL_IP = (3000, 60)  # 每个 IP 每分钟（校园网很多人共用出口 IP，所以放宽）
RL_REGISTER_IP = (50, 600)  # 每个 IP 每 10 分钟登记次数
RL_GATE_FAIL = (10, 600)  # 每台设备每 10 分钟口令输错次数，超过就暂时锁定（设备标识是客户端 Cookie，只约束正常浏览器）
# 按 IP 只限速、不锁定：校园网很多人共用一个出口 IP，按 IP 锁定会让所有人一起被锁。
# 真正挡暴力猜测的是口令本身的长度 + 每次输错延迟 1 秒 + 这里的每分钟上限。
RL_GATE_IP = (30, 60)  # 每个 IP 每分钟口令输错次数


def _load_secret() -> bytes:
    """签名密钥：优先用环境变量；否则首次启动时随机生成，保存在数据目录（多个 worker 共用同一份）。"""
    if os.environ.get("GST_SECRET"):
        return os.environ["GST_SECRET"].encode()
    path = Path(DB_PATH).parent / "secret.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    for _ in range(50):  # 另一个 worker 可能刚创建、还没写完
        value = path.read_text().strip()
        if value:
            return value.encode()
        time.sleep(0.02)
    raise RuntimeError(f"无法读取密钥文件 {path}")


SECRET = _load_secret()
