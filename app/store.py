"""数据访问层：全项目唯一写 SQL 的地方。

并发策略
- SQLite WAL 模式：读写互不阻塞，多个读可以并行；多个进程（--workers）共用同一个文件也安全。
- 写操作走 tx()：BEGIN IMMEDIATE 一开始就拿写锁，"读状态 → 判断 → 写入"整段原子，避免竞态。
- 读操作走 read()：放在一个只读事务里，多条查询看到同一时刻的快照。
- 每个请求一个短连接，事务尽量短；写锁排队最多等 busy_timeout（10 秒）。
- pool.rev 每次池内有变化就 +1，前端轮询时比对 rev，没变化直接返回 204。
"""
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pool (
    id         TEXT PRIMARY KEY,
    code       TEXT UNIQUE NOT NULL,
    state      TEXT NOT NULL DEFAULT 'open',      -- open | confirming | executing | done
    rev        INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS member (
    id         TEXT PRIMARY KEY,
    nickname   TEXT NOT NULL,
    grp        TEXT,                              -- 此刻所在组；执行中 leave 后为 NULL
    token_hash TEXT UNIQUE NOT NULL,
    pool_id    TEXT REFERENCES pool(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    device_hash TEXT,                             -- 登记时的设备（一台设备只能登记一个代号）
    ghost      INTEGER NOT NULL DEFAULT 0         -- 占位：LM Core 上在组里但没加入本工具的人，占着位置不动
);
CREATE TABLE IF NOT EXISTS pool_group (
    pool_id  TEXT NOT NULL REFERENCES pool(id) ON DELETE CASCADE,
    grp      TEXT NOT NULL UNIQUE,                -- 一个组只能在一个池里
    lm_count INTEGER NOT NULL,                    -- 该组在 LM Core 上的人数
    PRIMARY KEY (pool_id, grp)
);
CREATE TABLE IF NOT EXISTS wish (
    pool_id TEXT NOT NULL REFERENCES pool(id) ON DELETE CASCADE,
    from_id TEXT NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    to_id   TEXT NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    PRIMARY KEY (from_id, to_id)
);
CREATE TABLE IF NOT EXISTS plan (
    pool_id   TEXT NOT NULL REFERENCES pool(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    from_grp  TEXT NOT NULL,
    to_grp    TEXT NOT NULL,
    in_team   INTEGER NOT NULL,
    approved  INTEGER NOT NULL DEFAULT 0,
    moves_done INTEGER NOT NULL DEFAULT 0,             -- 执行中已完成的移动次数（环里中间的人会移动两次）
    PRIMARY KEY (pool_id, member_id)
);
CREATE TABLE IF NOT EXISTS event (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id TEXT NOT NULL REFERENCES pool(id) ON DELETE CASCADE,
    at      TEXT NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS member_pool ON member(pool_id);
CREATE INDEX IF NOT EXISTS event_pool ON event(pool_id, id);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """给旧数据库补上后加的列和索引（新建的数据库也会走一遍，都是幂等的）。"""
        cols = {row["name"] for row in c.execute("PRAGMA table_info(member)")}
        if "device_hash" not in cols:
            c.execute("ALTER TABLE member ADD COLUMN device_hash TEXT")
        if "ghost" not in cols:
            c.execute("ALTER TABLE member ADD COLUMN ghost INTEGER NOT NULL DEFAULT 0")
        if "moves_done" not in {row["name"] for row in c.execute("PRAGMA table_info(plan)")}:
            c.execute("ALTER TABLE plan ADD COLUMN moves_done INTEGER NOT NULL DEFAULT 0")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS member_nickname ON member(nickname COLLATE NOCASE)")
        c.execute("CREATE INDEX IF NOT EXISTS member_device ON member(device_hash)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA synchronous=NORMAL")
        try:
            yield c
        finally:
            c.close()

    def vacuum(self) -> None:
        """删除数据后把文件真正缩小（不能在事务里执行）。"""
        with self._connect() as c:
            c.execute("VACUUM")

    @contextmanager
    def read(self) -> Iterator["Repo"]:
        """只读事务：一次页面渲染里的多条查询看到的是同一时刻的快照。"""
        with self._connect() as c:
            c.execute("BEGIN")
            try:
                yield Repo(c)
            finally:
                c.execute("COMMIT")

    @contextmanager
    def tx(self) -> Iterator["Repo"]:
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield Repo(c)
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")


class Repo:
    """一个连接上的查询集合。方法名即含义，只返回 sqlite3.Row 或基本类型。"""

    def __init__(self, c: sqlite3.Connection):
        self.c = c

    # ---- member ----
    def member(self, member_id: str):
        return self.c.execute("SELECT * FROM member WHERE id=?", (member_id,)).fetchone()

    def member_by_token(self, token_hash: str):
        return self.c.execute("SELECT * FROM member WHERE token_hash=? AND ghost=0", (token_hash,)).fetchone()

    def all_members(self):
        return self.c.execute("SELECT * FROM member WHERE ghost=0 ORDER BY grp, created_at").fetchall()

    def member_by_device(self, device_hash: str):
        return self.c.execute("SELECT * FROM member WHERE device_hash=?", (device_hash,)).fetchone()

    def nickname_taken(self, nickname: str) -> bool:
        return self.c.execute(
            "SELECT 1 FROM member WHERE nickname=? COLLATE NOCASE", (nickname,)
        ).fetchone() is not None

    def insert_member(self, member_id: str, nickname: str, grp: str, token_hash: str,
                      device_hash: str | None = None, pool_id: str | None = None, ghost: bool = False) -> None:
        self.c.execute(
            "INSERT INTO member (id, nickname, grp, token_hash, created_at, device_hash, pool_id, ghost)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (member_id, nickname, grp, token_hash, now(), device_hash, pool_id, int(ghost)),
        )

    def delete_member(self, member_id: str) -> None:
        self.c.execute("DELETE FROM member WHERE id=?", (member_id,))

    def set_member_group(self, member_id: str, grp: str | None) -> None:
        self.c.execute("UPDATE member SET grp=? WHERE id=?", (grp, member_id))

    def set_member_pool(self, member_id: str, pool_id: str | None) -> None:
        self.c.execute("UPDATE member SET pool_id=? WHERE id=?", (pool_id, member_id))

    def set_member_token(self, member_id: str, token_hash: str) -> None:
        self.c.execute("UPDATE member SET token_hash=? WHERE id=?", (token_hash, member_id))

    def pool_members(self, pool_id: str):
        """此刻在池里的人（含占位）。"""
        return self.c.execute(
            "SELECT * FROM member WHERE pool_id=? ORDER BY grp, created_at", (pool_id,)
        ).fetchall()

    def plan_members(self, pool_id: str):
        """方案里的所有人（池完成后退出的人也在内，用来展示最终分组）。"""
        return self.c.execute(
            "SELECT m.* FROM plan p JOIN member m ON m.id = p.member_id WHERE p.pool_id=?"
            " ORDER BY m.grp, m.created_at", (pool_id,)
        ).fetchall()

    def delete_ghosts(self, pool_id: str, grp: str | None = None) -> None:
        if grp is None:
            self.c.execute("DELETE FROM member WHERE ghost=1 AND pool_id=?", (pool_id,))
        else:
            self.c.execute("DELETE FROM member WHERE ghost=1 AND pool_id=? AND grp=?", (pool_id, grp))

    # ---- pool ----
    def pool(self, pool_id: str):
        return self.c.execute("SELECT * FROM pool WHERE id=?", (pool_id,)).fetchone()

    def pool_by_code(self, code: str):
        return self.c.execute("SELECT * FROM pool WHERE code=?", (code,)).fetchone()

    def pool_rev(self, pool_id: str) -> int | None:
        row = self.c.execute("SELECT rev FROM pool WHERE id=?", (pool_id,)).fetchone()
        return row["rev"] if row else None

    def insert_pool(self, pool_id: str, code: str) -> None:
        self.c.execute("INSERT INTO pool (id, code, created_at) VALUES (?,?,?)", (pool_id, code, now()))

    def set_pool_state(self, pool_id: str, state: str) -> None:
        self.c.execute("UPDATE pool SET state=? WHERE id=?", (state, pool_id))

    def bump(self, pool_id: str) -> None:
        self.c.execute("UPDATE pool SET rev=rev+1 WHERE id=?", (pool_id,))

    def delete_pool(self, pool_id: str) -> None:
        self.delete_ghosts(pool_id)
        self.c.execute("DELETE FROM pool WHERE id=?", (pool_id,))

    def all_pools(self):
        return self.c.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM member m WHERE m.pool_id = p.id AND m.ghost=0) AS members"
            " FROM pool p ORDER BY created_at"
        ).fetchall()

    # ---- pool_group ----
    def pool_groups(self, pool_id: str):
        return self.c.execute("SELECT * FROM pool_group WHERE pool_id=? ORDER BY grp", (pool_id,)).fetchall()

    def pool_of_group(self, grp: str):
        return self.c.execute("SELECT * FROM pool_group WHERE grp=?", (grp,)).fetchone()

    def insert_pool_group(self, pool_id: str, grp: str, lm_count: int) -> None:
        self.c.execute("INSERT INTO pool_group (pool_id, grp, lm_count) VALUES (?,?,?)", (pool_id, grp, lm_count))

    def set_group_count(self, pool_id: str, grp: str, lm_count: int) -> None:
        self.c.execute("UPDATE pool_group SET lm_count=? WHERE pool_id=? AND grp=?", (lm_count, pool_id, grp))

    def delete_pool_group(self, pool_id: str, grp: str) -> None:
        self.c.execute("DELETE FROM pool_group WHERE pool_id=? AND grp=?", (pool_id, grp))

    def delete_pool_groups(self, pool_id: str) -> None:
        """池完成后释放各组，让它们可以参加新的池。"""
        self.c.execute("DELETE FROM pool_group WHERE pool_id=?", (pool_id,))

    # ---- wish ----
    def wishes(self, pool_id: str) -> list[tuple[str, str]]:
        rows = self.c.execute("SELECT from_id, to_id FROM wish WHERE pool_id=?", (pool_id,)).fetchall()
        return [(r["from_id"], r["to_id"]) for r in rows]

    def has_wish(self, from_id: str, to_id: str) -> bool:
        return self.c.execute("SELECT 1 FROM wish WHERE from_id=? AND to_id=?", (from_id, to_id)).fetchone() is not None

    def insert_wish(self, pool_id: str, from_id: str, to_id: str) -> None:
        self.c.execute("INSERT OR IGNORE INTO wish VALUES (?,?,?)", (pool_id, from_id, to_id))

    def delete_wish(self, from_id: str, to_id: str) -> None:
        self.c.execute("DELETE FROM wish WHERE from_id=? AND to_id=?", (from_id, to_id))

    def delete_wishes_of(self, member_id: str) -> None:
        self.c.execute("DELETE FROM wish WHERE from_id=? OR to_id=?", (member_id, member_id))

    # ---- plan ----
    def plan(self, pool_id: str):
        return self.c.execute("SELECT * FROM plan WHERE pool_id=?", (pool_id,)).fetchall()

    def insert_plan(self, pool_id: str, member_id: str, from_grp: str, to_grp: str, in_team: bool,
                    approved: bool = False) -> None:
        self.c.execute(
            "INSERT INTO plan (pool_id, member_id, from_grp, to_grp, in_team, approved) VALUES (?,?,?,?,?,?)",
            (pool_id, member_id, from_grp, to_grp, int(in_team), int(approved)),
        )

    def plan_row(self, pool_id: str, member_id: str) -> tuple[str, str]:
        row = self.c.execute(
            "SELECT from_grp, to_grp FROM plan WHERE pool_id=? AND member_id=?", (pool_id, member_id)
        ).fetchone()
        return (row["from_grp"], row["to_grp"]) if row else ("", "")

    def set_moves_done(self, pool_id: str, member_id: str, n: int) -> None:
        self.c.execute("UPDATE plan SET moves_done=? WHERE pool_id=? AND member_id=?", (n, pool_id, member_id))

    def approve_plan(self, pool_id: str, member_id: str) -> None:
        self.c.execute("UPDATE plan SET approved=1 WHERE pool_id=? AND member_id=?", (pool_id, member_id))

    def delete_plan(self, pool_id: str) -> None:
        self.c.execute("DELETE FROM plan WHERE pool_id=?", (pool_id,))

    # ---- event ----
    def add_event(self, pool_id: str, text: str) -> None:
        self.c.execute("INSERT INTO event (pool_id, at, text) VALUES (?,?,?)", (pool_id, now(), text))

    def events(self, pool_id: str, limit: int = 40):
        return self.c.execute(
            "SELECT * FROM event WHERE pool_id=? ORDER BY id DESC LIMIT ?", (pool_id, limit)
        ).fetchall()

    # ---- 一键删除 ----
    def purge_all(self) -> None:
        for table in ("event", "plan", "wish", "pool_group", "member", "pool"):
            self.c.execute(f"DELETE FROM {table}")
