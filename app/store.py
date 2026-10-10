"""数据访问层：全项目唯一写 SQL 的地方。

并发策略
- SQLite WAL 模式：读写互不阻塞，多个读可以并行；多个进程（--workers）共用同一个文件也安全。
- 写操作走 tx()：BEGIN IMMEDIATE 一开始就拿写锁，"读状态 → 判断 → 写入"整段原子，避免竞态。
- 读操作走 read()：放在一个只读事务里，多条查询看到同一时刻的快照。
- 每个请求一个短连接，事务尽量短；写锁排队最多等 busy_timeout（10 秒）。
- pool.rev / team.rev 每次有变化就 +1，前端轮询时比对 rev，没变化直接返回 204。

两种组织方式
- team（意向组）：想一起组队的几个人；组长拉人、各自登记现在的组号和本组人数；全站所有等待中的意向组
  放在一起统一分配（见 domain/allocate.py）。分配出的方案是一个 mode='round' 的 pool（一轮），
  确认 / 执行 / 完成都复用池的流程。
- pool（整组池，旧方式）：几个组全员加入、互选小队、再算方案。
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
    created_at TEXT NOT NULL,
    mode       TEXT NOT NULL DEFAULT 'pool'       -- pool：整组池 | round：意向组统一分配出的一轮（没有 open 阶段）
);
CREATE TABLE IF NOT EXISTS team (
    id         TEXT PRIMARY KEY,
    code       TEXT UNIQUE NOT NULL,              -- 邀请码（和池的邀请码同一个命名空间：/j/<code>）
    leader_id  TEXT,                              -- 组长（退出时最早登记的成员接任）
    locked     INTEGER NOT NULL DEFAULT 0,        -- 组长已宣布成员到齐：参与全站统一分配，不能再加入 / 退出
    paused     INTEGER NOT NULL DEFAULT 0,        -- 上一份方案被本组的人否决 / 中止：暂不参与分配，组长点"重新参与"
    pool_id    TEXT REFERENCES pool(id) ON DELETE SET NULL,  -- 正在进行（或已完成）的那一轮
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
    ghost      INTEGER NOT NULL DEFAULT 0,        -- 占位：LM Core 上在组里但不参与这次调整的人，占着位置不动
    e2e_pub    TEXT,                              -- 浏览器生成的 ECDH 公钥（一对一对话端到端加密用；私钥只在浏览器里）
    team_id    TEXT REFERENCES team(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS pool_group (
    pool_id  TEXT NOT NULL REFERENCES pool(id) ON DELETE CASCADE,
    grp      TEXT NOT NULL,                       -- pool 模式下一个组只能在一个池里（代码里查）
    lm_count INTEGER NOT NULL,                    -- 该组在 LM Core 上的人数
    PRIMARY KEY (pool_id, grp)
);
CREATE TABLE IF NOT EXISTS group_count (          -- 各组在 LM Core 上的最新人数（爬虫推送 / 成员填报 / 一轮完成后的结果），全站共享
    grp    TEXT PRIMARY KEY,
    count  INTEGER NOT NULL,
    source TEXT NOT NULL,                         -- crawler | member:<代号> | round
    at     TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS team_event (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL REFERENCES team(id) ON DELETE CASCADE,
    at      TEXT NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS member_pool ON member(pool_id);
CREATE INDEX IF NOT EXISTS event_pool ON event(pool_id, id);
CREATE INDEX IF NOT EXISTS team_event_team ON team_event(team_id, id);
"""

ROUND_STATES = ("confirming", "executing")  # 一轮进行中（涉及的组人数在变，别的意向组先不碰这些组）


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
        if "e2e_pub" not in cols:
            c.execute("ALTER TABLE member ADD COLUMN e2e_pub TEXT")
        if "team_id" not in cols:
            c.execute("ALTER TABLE member ADD COLUMN team_id TEXT REFERENCES team(id) ON DELETE SET NULL")
        if "mode" not in {row["name"] for row in c.execute("PRAGMA table_info(pool)")}:
            c.execute("ALTER TABLE pool ADD COLUMN mode TEXT NOT NULL DEFAULT 'pool'")
        sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='pool_group'").fetchone()["sql"]
        if "UNIQUE" in sql.upper():  # 旧库：grp 全局唯一；一轮分配里的组和整组池里的组可能重合，改成按 (pool, grp) 唯一
            c.executescript("""
                CREATE TABLE pool_group_new (
                    pool_id TEXT NOT NULL REFERENCES pool(id) ON DELETE CASCADE,
                    grp TEXT NOT NULL, lm_count INTEGER NOT NULL, PRIMARY KEY (pool_id, grp));
                INSERT INTO pool_group_new SELECT pool_id, grp, lm_count FROM pool_group;
                DROP TABLE pool_group;
                ALTER TABLE pool_group_new RENAME TO pool_group;
            """)
        if "moves_done" not in {row["name"] for row in c.execute("PRAGMA table_info(plan)")}:
            c.execute("ALTER TABLE plan ADD COLUMN moves_done INTEGER NOT NULL DEFAULT 0")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS member_nickname ON member(nickname COLLATE NOCASE)")
        c.execute("CREATE INDEX IF NOT EXISTS member_device ON member(device_hash)")
        c.execute("CREATE INDEX IF NOT EXISTS member_team ON member(team_id)")

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

    def set_member_team(self, member_id: str, team_id: str | None) -> None:
        self.c.execute("UPDATE member SET team_id=? WHERE id=?", (team_id, member_id))

    def set_member_token(self, member_id: str, token_hash: str) -> None:
        self.c.execute("UPDATE member SET token_hash=? WHERE id=?", (token_hash, member_id))

    def set_e2e_pub(self, member_id: str, pub: str) -> None:
        self.c.execute("UPDATE member SET e2e_pub=? WHERE id=?", (pub, member_id))

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

    # ---- team（意向组） ----
    def team(self, team_id: str):
        return self.c.execute("SELECT * FROM team WHERE id=?", (team_id,)).fetchone()

    def team_by_code(self, code: str):
        return self.c.execute("SELECT * FROM team WHERE code=?", (code,)).fetchone()

    def insert_team(self, team_id: str, code: str, leader_id: str) -> None:
        self.c.execute("INSERT INTO team (id, code, leader_id, created_at) VALUES (?,?,?,?)",
                       (team_id, code, leader_id, now()))

    def set_team(self, team_id: str, **fields) -> None:
        """改 locked / paused / leader_id / pool_id 中的任意几个。"""
        allowed = {"locked", "paused", "leader_id", "pool_id"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown team fields: {bad}")
        for k, v in fields.items():
            self.c.execute(f"UPDATE team SET {k}=? WHERE id=?", (int(v) if isinstance(v, bool) else v, team_id))

    def bump_team(self, team_id: str) -> None:
        self.c.execute("UPDATE team SET rev=rev+1 WHERE id=?", (team_id,))

    def bump_waiting_teams(self) -> None:
        """等待分配的意向组页面都刷新一下（全站人数变了，候选组的判断可能变）。"""
        self.c.execute("UPDATE team SET rev=rev+1 WHERE pool_id IS NULL")

    def team_rev(self, team_id: str) -> int | None:
        row = self.c.execute("SELECT rev FROM team WHERE id=?", (team_id,)).fetchone()
        return row["rev"] if row else None

    def delete_team(self, team_id: str) -> None:
        self.c.execute("DELETE FROM team WHERE id=?", (team_id,))

    def team_members(self, team_id: str):
        return self.c.execute(
            "SELECT * FROM member WHERE team_id=? AND ghost=0 ORDER BY created_at", (team_id,)).fetchall()

    def teams_in_pool(self, pool_id: str):
        return self.c.execute("SELECT * FROM team WHERE pool_id=? ORDER BY created_at", (pool_id,)).fetchall()

    def waiting_teams(self):
        """已宣布到齐、没暂停、不在任何一轮里的意向组：全站统一分配的输入。"""
        return self.c.execute(
            "SELECT * FROM team WHERE locked=1 AND paused=0 AND pool_id IS NULL ORDER BY created_at").fetchall()

    def all_teams(self):
        return self.c.execute(
            "SELECT t.*, p.state AS round_state,"
            " (SELECT COUNT(*) FROM member m WHERE m.team_id = t.id AND m.ghost=0) AS members"
            " FROM team t LEFT JOIN pool p ON p.id = t.pool_id ORDER BY t.created_at").fetchall()

    def active_round_groups(self) -> set[str]:
        """正在确认 / 执行的轮次涉及的组：这些组的人数在变，别的意向组先不往里算。"""
        rows = self.c.execute(
            "SELECT g.grp FROM pool_group g JOIN pool p ON p.id = g.pool_id"
            " WHERE p.mode='round' AND p.state IN (?, ?)", ROUND_STATES).fetchall()
        return {row["grp"] for row in rows}

    def add_team_event(self, team_id: str, text: str) -> None:
        self.c.execute("INSERT INTO team_event (team_id, at, text) VALUES (?,?,?)", (team_id, now(), text))

    def team_events(self, team_id: str, limit: int = 40):
        return self.c.execute(
            "SELECT * FROM team_event WHERE team_id=? ORDER BY id DESC LIMIT ?", (team_id, limit)).fetchall()

    # ---- pool ----
    def pool(self, pool_id: str):
        return self.c.execute("SELECT * FROM pool WHERE id=?", (pool_id,)).fetchone()

    def pool_by_code(self, code: str):
        return self.c.execute("SELECT * FROM pool WHERE code=?", (code,)).fetchone()

    def pool_rev(self, pool_id: str) -> int | None:
        row = self.c.execute("SELECT rev FROM pool WHERE id=?", (pool_id,)).fetchone()
        return row["rev"] if row else None

    def insert_pool(self, pool_id: str, code: str, mode: str = "pool") -> None:
        self.c.execute("INSERT INTO pool (id, code, created_at, mode) VALUES (?,?,?,?)",
                       (pool_id, code, now(), mode))

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
        """pool 模式下这个组所在的池（一个组只能在一个整组池里）。意向组的轮次不占组。"""
        return self.c.execute(
            "SELECT g.* FROM pool_group g JOIN pool p ON p.id = g.pool_id WHERE g.grp=? AND p.mode='pool'", (grp,)
        ).fetchone()

    def pool_group(self, pool_id: str, grp: str):
        return self.c.execute("SELECT * FROM pool_group WHERE pool_id=? AND grp=?", (pool_id, grp)).fetchone()

    def insert_pool_group(self, pool_id: str, grp: str, lm_count: int) -> None:
        self.c.execute("INSERT INTO pool_group (pool_id, grp, lm_count) VALUES (?,?,?)", (pool_id, grp, lm_count))

    def set_group_count(self, pool_id: str, grp: str, lm_count: int) -> None:
        self.c.execute("UPDATE pool_group SET lm_count=? WHERE pool_id=? AND grp=?", (lm_count, pool_id, grp))

    def delete_pool_group(self, pool_id: str, grp: str) -> None:
        self.c.execute("DELETE FROM pool_group WHERE pool_id=? AND grp=?", (pool_id, grp))

    def delete_pool_groups(self, pool_id: str) -> None:
        """池完成后释放各组，让它们可以参加新的池。"""
        self.c.execute("DELETE FROM pool_group WHERE pool_id=?", (pool_id,))

    # ---- group_count（全站共享的各组人数） ----
    def group_count(self, grp: str):
        return self.c.execute("SELECT * FROM group_count WHERE grp=?", (grp,)).fetchone()

    def all_group_counts(self):
        return self.c.execute("SELECT * FROM group_count ORDER BY grp").fetchall()

    def upsert_group_count(self, grp: str, count: int, source: str) -> None:
        self.c.execute(
            "INSERT INTO group_count (grp, count, source, at) VALUES (?,?,?,?)"
            " ON CONFLICT(grp) DO UPDATE SET count=excluded.count, source=excluded.source, at=excluded.at",
            (grp, count, source, now()))

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
        for table in ("team_event", "event", "plan", "wish", "pool_group", "member", "team", "pool", "group_count"):
            self.c.execute(f"DELETE FROM {table}")
