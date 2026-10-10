"""用例编排：读数据 → 调 domain 纯逻辑 → 在同一个事务里写回。

Web 层只调用这里；这里不关心 HTTP，也不写 SQL。

两种组织方式
- 意向组（team）：想一起组队的人，组长拉人，各自登记现在的组号和本组人数；组长宣布到齐后进入全站统一分配
  （domain/allocate.py）：所有等待中的意向组放在一起算，让最多人一次如愿、移动最少，意向组之间可以互换位置，
  不参加工具的人谁都不动。分配结果是一个 mode='round' 的池（"一轮"），确认 / 执行 / 完成复用池的流程。
- 整组池（pool，旧方式）：几个组全员加入、互选小队、再生成方案。

并发约定
- 每个写用例都在一个 BEGIN IMMEDIATE 事务里"重新读取最新状态 → 校验 → 写入"，不信任页面上可能已经过期的信息。
- 所有用例都是幂等的：双击、网络重试、多个标签页重复提交，结果都和只提交一次相同。

安全约定
- 代号是用户输入；凡是拼进会用 |safe 渲染的 HTML 片段里（swap_text / todo），必须先 escape()。
"""
import hashlib
import re
import secrets
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from typing import NamedTuple

from markupsafe import escape

from . import config
from .chat import Chat
from .domain import allocate, execution, pairing, solver, teams
from .store import Repo, Store

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉易混淆的 I L O 0 1
GHOST_NAME = "占位（未加入）"
ROUND_GHOST_NAME = "组内其他人（不动）"
_B64 = re.compile(r"^[A-Za-z0-9+/=_-]+$")
PUBKEY_LEN = (60, 200)  # P-256 raw 公钥 base64 是 88 字符；留余量
CIPHER_MAX = 2048  # 密文（base64）上限：一个微信号绰绰有余
ROUND_REV_BASE = 1_000_000  # 轮次的 rev 加这个偏移，和意向组自己的 rev 不会撞上（页面靠 rev 变化刷新）
STAGE_LABEL = {"build": "组建中", "wait": "等待分配", "paused": "已暂停", "confirming": "确认中",
               "executing": "执行中", "done": "已完成"}  # 管理页里意向组的阶段


class UserError(Exception):
    """可以直接展示给用户的错误。"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_id() -> str:
    return secrets.token_hex(6)


def _new_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))


INSTRUCTIONS = {
    "move": "取消勾选 <b>Group {cur}</b>，勾选 <b>Group {to}</b>，点 <b>Save my choice</b>。",
    "leave": "取消勾选 <b>Group {cur}</b>，点 <b>Save my choice</b>。之后暂时无组，轮到时再加入。",
    "join": "勾选 <b>Group {to}</b>，点 <b>Save my choice</b>。",
}

MIN_GROUPS = 2  # 池里至少要有的组数；所有组全员到齐后才开放互选
MIN_TEAM = 2  # 意向组至少要有的人数
CRAWLER_FRESH = timedelta(hours=2)  # 爬虫数据在这段时间内优先于成员填报

# 一对一临时对话：做完一步自动发给这次交换的对象（不存储）
DID_NOTES = {"move": "我已从 {src} 换到 {dst}。", "leave": "我已退出 {src}。", "join": "我已加入 {dst}。"}
YOUR_TURN = {"move": "轮到你：从 {src} 换到 {dst}。", "leave": "轮到你：先退出 {src}。", "join": "轮到你：加入 {dst}。"}
QUICK_NOTES = {  # 手动发：key → (按钮文字, 消息)
    "ready": ("准备好了", "准备好了。"),
    "wait": ("稍等 5 分钟", "稍等 5 分钟。"),
    "ok": ("收到", "收到。"),
    "help": ("遇到问题", "遇到问题，请在群里联系我。"),
    "full": ("目标组已满", "LM Core 上 {dst} 组显示已满，无法保存。"),
}


class ExecState(NamedTuple):
    """执行阶段的完整状态：方案拆成的两两交换 + 每人此刻所在组 + 每人已完成的移动次数。"""
    swaps: list[pairing.Swap]
    cur: dict[str, str | None]
    done: dict[str, int]
    in_team: set[str]
    plan: dict[str, object]
    names: dict[str, str]  # 方案里所有人（含已退出完成池的人）的显示名


class Service:
    def __init__(self, store: Store, chat: Chat | None = None):
        self.store = store
        self.chat = chat or Chat()

    # ------------------------------------------------------------------ 身份

    def register(self, nickname: str, grp: str, device: str | None = None) -> str:
        """登记代号。代号全局唯一（不区分大小写）；传入 device 时，一台设备只能登记一个代号。"""
        nickname = " ".join(nickname.split())[:20]
        if not nickname:
            raise UserError("请填写代号")
        self._check_group(grp)
        token = secrets.token_urlsafe(24)
        device_hash = _hash(device) if device else None
        try:
            with self.store.tx() as r:
                if device_hash and (owner := r.member_by_device(device_hash)):
                    raise UserError(f"这台设备已登记过代号“{owner['nickname']}”，一台设备只能登记一个")
                if r.nickname_taken(nickname):
                    raise UserError(f"代号“{nickname}”已被使用，请换一个")
                r.insert_member(_new_id(), nickname, grp, _hash(token), device_hash)
        except sqlite3.IntegrityError as e:  # 并发时两人同时抢同一个代号
            raise UserError(f"代号“{nickname}”已被使用，请换一个") from e
        return token

    def current(self, token: str | None):
        if not token:
            return None
        with self.store.read() as r:
            return r.member_by_token(_hash(token))

    def change_group(self, member_id: str, grp: str, lm_count: int | None = None) -> None:
        """改组号。
        - 没加入任何东西：随时可改。
        - 在已完成的池 / 轮里：自动退出再改。
        - 整组池互选阶段暂时无组（中止执行后）：可以改成池内的某个组（表示已在 LM Core 加入了它）。
        - 意向组等待分配时：可以改（顺便填报新组的人数；没填过的组必须填），人数一变就重新判断能否分配。"""
        self._check_group(grp)
        with self.store.tx() as r:
            me = r.member(member_id)
            if me["pool_id"]:
                pool = r.pool(me["pool_id"])
                if pool["state"] == "done":
                    self._leave_any(r, me, pool)
                    me = r.member(member_id)
                elif pool["mode"] == "round":
                    raise UserError("分配进行中不能改组号；出了问题请用“中止执行”")
                elif pool["state"] == "open" and me["grp"] is None:
                    pg = r.pool_group(pool["id"], grp)
                    if not pg:
                        raise UserError("只能改成池内的组（先在 LM Core 加入它）；要去池外的组请先退出池")
                    r.set_member_group(member_id, grp)
                    r.set_group_count(pool["id"], grp, pg["lm_count"] + 1)
                    r.add_event(pool["id"], f"{me['nickname']} 加入 {grp}")
                    r.bump(pool["id"])
                    return
                else:
                    raise UserError("已经在池里时不能改组号，请先退出池")
            if me["team_id"]:
                team = r.team(me["team_id"])
                r.set_member_group(member_id, grp)
                me = r.member(member_id)
                if lm_count is not None:
                    self._report_count(r, me, team, lm_count)
                elif not r.group_count(grp):
                    raise UserError(f"请一并填写 {grp} 组现在的人数")
                r.add_team_event(team["id"], f"{me['nickname']} 现在在 {grp} 组")
                r.bump_team(team["id"])
                self._global_plan(r)
                return
            r.set_member_group(member_id, grp)

    def login_as(self, member_id: str) -> str:
        """开发模式：切换身份（给该成员换一个新 token）。"""
        token = secrets.token_urlsafe(24)
        with self.store.tx() as r:
            me = r.member(member_id)
            if not me or me["ghost"]:
                raise UserError("没有这个成员")
            r.set_member_token(member_id, _hash(token))
        return token

    def identities(self):
        with self.store.read() as r:
            return r.all_members()

    def invite_kind(self, code: str) -> str | None:
        """邀请码是意向组的还是整组池的；不存在返回 None。"""
        code = code.strip().upper()
        with self.store.read() as r:
            if r.team_by_code(code):
                return "team"
            return "pool" if r.pool_by_code(code) else None

    def pool_exists(self, code: str) -> bool:
        return self.invite_kind(code) is not None

    def join(self, member_id: str, code: str, lm_count: int) -> None:
        """按邀请码加入：意向组或整组池。"""
        kind = self.invite_kind(code)
        if kind == "team":
            self.join_team(member_id, code, lm_count)
        elif kind == "pool":
            self.join_pool(member_id, code, lm_count)
        else:
            raise UserError("邀请码不存在，请检查是否输错")

    def leave(self, member_id: str) -> None:
        """退出当前的意向组 / 整组池。"""
        with self.store.read() as r:
            me = r.member(member_id)
        if me and me["team_id"]:
            self.leave_team(member_id)
        else:
            self.leave_pool(member_id)

    # ------------------------------------------------------------------ 意向组

    def create_team(self, member_id: str, lm_count: int) -> str:
        """组长创建意向组，同时登记自己组现在的人数。"""
        with self.store.tx() as r:
            me = self._release_done(r, r.member(member_id))
            if me["team_id"]:  # 重复提交
                return r.team(me["team_id"])["code"]
            if me["pool_id"]:
                raise UserError("你在一个整组池里，请先退出那个池")
            team_id, code = _new_id(), _new_code()
            r.insert_team(team_id, code, me["id"])
            r.add_team_event(team_id, f"{me['nickname']} 创建意向组")
            self._join_team(r, me, r.team(team_id), lm_count)
        return code

    def join_team(self, member_id: str, code: str, lm_count: int) -> None:
        with self.store.tx() as r:
            me = r.member(member_id)
            team = r.team_by_code(code.strip().upper())
            if not team:
                raise UserError("邀请码不存在，请检查是否输错")
            if me["team_id"] == team["id"]:  # 重复提交
                return
            me = self._release_done(r, me)
            if me["team_id"]:
                raise UserError("你已经在另一个意向组里了，请先退出")
            if me["pool_id"]:
                raise UserError("你在一个整组池里，请先退出那个池")
            self._join_team(r, me, team, lm_count)

    def _join_team(self, r: Repo, me, team, lm_count: int) -> None:
        if team["pool_id"]:
            raise UserError("这个意向组已经在分配中，不能再加入")
        if team["locked"]:
            raise UserError("组长已宣布成员到齐，不能再加入；请联系组长重新开放")
        if len(r.team_members(team["id"])) >= config.CAPACITY:
            raise UserError(f"意向组最多 {config.CAPACITY} 人")
        r.set_member_team(me["id"], team["id"])
        self._report_count(r, me, team, lm_count)
        r.add_team_event(team["id"], f"{me['nickname']}（{me['grp']} 组，该组 {lm_count} 人）加入")
        r.bump_team(team["id"])

    def _report_count(self, r: Repo, me, team, count: int) -> None:
        """成员填报自己所在组在 LM Core 上的人数（含不参加的人）。两小时内有爬虫数据就以爬虫为准。"""
        grp = me["grp"]
        if not grp:
            raise UserError("你现在没有组")
        in_grp = sum(1 for m in r.team_members(team["id"]) if m["grp"] == grp)
        if not max(1, in_grp) <= count <= config.CAPACITY:
            raise UserError(f"{grp} 组人数应在 {max(1, in_grp)}–{config.CAPACITY} 之间（本意向组有 {in_grp} 人在该组）")
        row = r.group_count(grp)
        fresh = (row and row["source"] == "crawler"
                 and datetime.fromisoformat(row["at"]) > datetime.now() - CRAWLER_FRESH)
        if not fresh:
            r.upsert_group_count(grp, count, f"member:{me['nickname']}")

    def leave_team(self, member_id: str) -> None:
        with self.store.tx() as r:
            me = r.member(member_id)
            if not me or not me["team_id"]:  # 重复提交
                return
            team = r.team(me["team_id"])
            pool = r.pool(team["pool_id"]) if team["pool_id"] else None
            if pool and pool["state"] != "done":
                raise UserError("分配进行中不能退出；出了问题请用“中止执行”")
            if team["locked"] and not pool:
                raise UserError("组长已宣布成员到齐；要退出请让组长先「重新开放加入」")
            self._leave_team(r, me, team)

    def _leave_team(self, r: Repo, me, team) -> None:
        r.set_member_team(me["id"], None)
        r.set_member_pool(me["id"], None)
        pool = r.pool(team["pool_id"]) if team["pool_id"] else None
        rest = r.team_members(team["id"])
        if not rest:
            r.delete_team(team["id"])
        else:
            if team["leader_id"] == me["id"]:  # 组长退出：最早登记的成员接任
                r.set_team(team["id"], leader_id=rest[0]["id"])
                r.add_team_event(team["id"], f"{rest[0]['nickname']} 接任组长")
            r.add_team_event(team["id"], f"{me['nickname']} 退出")
            r.bump_team(team["id"])
        if pool and not any(not m["ghost"] for m in r.pool_members(pool["id"])):
            r.delete_pool(pool["id"])
            self.chat.close_pool(pool["id"])

    def lock_team(self, member_id: str) -> None:
        """组长宣布成员到齐：之后不能再加入 / 退出，意向组进入全站统一分配。"""
        with self.store.tx() as r:
            me, team = self._my_team(r, member_id, leader=True)
            if team["pool_id"]:
                return
            if team["locked"]:
                return
            members = r.team_members(team["id"])
            if len(members) < MIN_TEAM:
                raise UserError(f"意向组至少 {MIN_TEAM} 人")
            homeless = [m["nickname"] for m in members if m["grp"] is None]
            if homeless:
                raise UserError("还有人暂时无组：" + "、".join(homeless))
            r.set_team(team["id"], locked=True, paused=False)
            r.add_team_event(team["id"], f"{me['nickname']} 宣布成员到齐，等待分配")
            r.bump_team(team["id"])
            self._global_plan(r)

    def unlock_team(self, member_id: str) -> None:
        with self.store.tx() as r:
            me, team = self._my_team(r, member_id, leader=True)
            if team["pool_id"]:
                raise UserError("分配已经开始，不能重新开放；确认阶段可以「不同意」退回")
            if not team["locked"]:
                return
            r.set_team(team["id"], locked=False, paused=False)
            r.add_team_event(team["id"], f"{me['nickname']} 重新开放加入")
            r.bump_team(team["id"])

    def resume_team(self, member_id: str) -> None:
        """方案被本组的人否决 / 中止后，组长让意向组重新参与分配。"""
        with self.store.tx() as r:
            me, team = self._my_team(r, member_id, leader=True)
            if team["pool_id"] or not team["paused"]:
                return
            if not team["locked"]:
                raise UserError("先宣布成员到齐")
            r.set_team(team["id"], paused=False)
            r.add_team_event(team["id"], f"{me['nickname']} 重新参与分配")
            r.bump_team(team["id"])
            self._global_plan(r)

    def update_counts(self, counts: dict, source: str = "crawler") -> dict:
        """爬虫推送各组在 LM Core 上的人数（{组号: 人数}）。写入后立刻跑一次全站分配。
        正在确认 / 执行的轮次涉及的组不受影响（那一轮完成后会按实际结果重新记录）。"""
        clean = {}
        for grp, n in counts.items():
            if grp not in config.GROUPS:
                raise UserError(f"组号不存在：{grp}")
            if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= config.CAPACITY:
                raise UserError(f"{grp} 的人数不是 0–{config.CAPACITY} 的整数")
            clean[grp] = n
        with self.store.tx() as r:
            for grp, n in clean.items():
                r.upsert_group_count(grp, n, source)
            planned = self._global_plan(r)
        return {"updated": len(clean), "planned": planned}

    def group_counts(self) -> list[dict]:
        with self.store.read() as r:
            return [dict(row) for row in r.all_group_counts()]

    def _global_plan(self, r: Repo) -> list[str]:
        """全站统一分配。所有已宣布到齐、没暂停、不在任何一轮里、成员都有组的意向组一起算；
        正在进行的轮次涉及的组先不碰。有解就开一轮（方案进入确认阶段）。返回新开轮次的邀请码。"""
        r.bump_waiting_teams()
        busy = r.active_round_groups()
        counts = {row["grp"]: row["count"] for row in r.all_group_counts()}
        team_members: dict[str, tuple[str, ...]] = {}
        current: dict[str, str] = {}
        rows: dict[str, list] = {}
        for t in r.waiting_teams():
            ms = r.team_members(t["id"])
            grps = {m["grp"] for m in ms}
            if len(ms) < MIN_TEAM or None in grps or grps & busy or not grps <= set(counts):
                continue
            if len(grps) == 1:  # 已经在同一个组：直接完成，不占别人的名额
                self._open_round(r, [t["id"]], {m["id"]: m["grp"] for m in ms}, counts)
                continue
            team_members[t["id"]] = tuple(sorted(m["id"] for m in ms))
            current.update({m["id"]: m["grp"] for m in ms})
            rows[t["id"]] = ms
        if not team_members:
            return []
        usable = {g: n for g, n in counts.items() if g not in busy}
        result = allocate.allocate(team_members, current, usable, config.CAPACITY)
        if not result:
            return []
        code = self._open_round(r, list(result.teams), {m: current[m] for t in result.teams for m in team_members[t]},
                                counts, result.target, result.groups)
        return [code]

    def _open_round(self, r: Repo, team_ids: list[str], current: dict[str, str], counts: dict[str, int],
                    target: dict[str, str] | None = None, groups: tuple[str, ...] | None = None) -> str:
        """把选中的意向组放进一轮：涉及的组建占位（= 组里不参加这次调整的人），写方案，进入确认。"""
        pool_id, code = _new_id(), _new_code()
        r.insert_pool(pool_id, code, mode="round")
        target = target or dict(current)
        groups = tuple(groups or sorted(set(current.values())))
        in_grp = Counter(current.values())
        for g in groups:
            r.insert_pool_group(pool_id, g, counts.get(g, 0))
            self._sync_ghosts(r, pool_id, g, max(0, min(counts.get(g, 0), config.CAPACITY) - in_grp[g]))
        names = []
        for tid in team_ids:
            r.set_team(tid, pool_id=pool_id)
            for m in r.team_members(tid):
                r.set_member_pool(m["id"], pool_id)
                names.append(m["nickname"])
            r.add_team_event(tid, "进入分配：系统生成了方案" + (f"（和另外 {len(team_ids) - 1} 个意向组一起）" if len(team_ids) > 1 else ""))
            r.bump_team(tid)
        members = r.pool_members(pool_id)
        all_teams = [tuple(sorted(m["id"] for m in r.team_members(tid))) for tid in team_ids]
        r.add_event(pool_id, "参与：" + "、".join(names))
        self._write_plan(r, r.pool(pool_id), None, members, r.pool_groups(pool_id), all_teams, target)
        return code

    @staticmethod
    def _sync_ghosts(r: Repo, pool_id: str, grp: str, want: int) -> None:
        """让某个组的占位数等于 want。"""
        ghosts = [m for m in r.pool_members(pool_id) if m["ghost"] and m["grp"] == grp]
        for g in ghosts[want:]:
            r.delete_member(g["id"])
        for _ in range(want - len(ghosts)):
            gid = _new_id()
            r.insert_member(gid, f"占位·{grp}·{gid[:4]}", grp, _hash(secrets.token_urlsafe(24)), pool_id=pool_id, ghost=True)

    def _cancel_round(self, r: Repo, pool, me, recount: bool, text: str) -> None:
        """撤销一轮（否决 / 中止）：方案作废，各意向组回到等待；本组的意向组暂停（组长调整后重新参与），
        其余意向组立刻重新参与分配。recount：按此刻每个人实际所在组重算涉及组的人数（执行中止用）。"""
        members = r.pool_members(pool["id"])
        if recount:
            live = Counter(m["grp"] for m in members if m["grp"])
            for g in r.pool_groups(pool["id"]):
                r.upsert_group_count(g["grp"], live[g["grp"]], f"member:{me['nickname']}")
        homeless = [m["nickname"] for m in members if not m["ghost"] and m["grp"] is None]
        for t in r.teams_in_pool(pool["id"]):
            r.set_team(t["id"], pool_id=None, paused=(t["id"] == me["team_id"]))
            r.add_team_event(t["id"], text + ("；暂无组：" + "、".join(homeless) if homeless else "")
                             + ("。本组暂停分配，组长调整后点「重新参与分配」" if t["id"] == me["team_id"] else "。继续等待分配"))
            r.bump_team(t["id"])
        for m in members:
            if not m["ghost"]:
                r.set_member_pool(m["id"], None)
        r.delete_pool(pool["id"])
        self._global_plan(r)

    @staticmethod
    def _my_team(r: Repo, member_id: str, leader: bool = False):
        me = r.member(member_id)
        if not me or not me["team_id"]:
            raise UserError("你还没有加入任何意向组")
        team = r.team(me["team_id"])
        if leader and team["leader_id"] != member_id:
            raise UserError("只有组长能这样做")
        return me, team

    def _team_view(self, r: Repo, me, team) -> dict:
        """意向组等待分配时的页面数据。"""
        members = r.team_members(team["id"])
        k = len(members)
        counts = {row["grp"]: row for row in r.all_group_counts()}
        busy = r.active_round_groups()
        cap = config.CAPACITY
        cands = []
        for g in sorted({m["grp"] for m in members if m["grp"]}):
            row = counts.get(g)
            team_in = sum(m["grp"] == g for m in members)
            others = max(0, row["count"] - team_in) if row else None
            cands.append({
                "grp": g, "count": row["count"] if row else None, "team_in": team_in, "others": others,
                "free": cap - others if others is not None else None,
                "short": max(0, others + k - cap) if others is not None else None,
                "feasible": others is not None and others + k <= cap,
                "source": row["source"] if row else None, "at": row["at"] if row else None, "busy": g in busy,
            })
        extras = sorted(({"grp": g, "count": row["count"], "free": cap - row["count"]}
                         for g, row in counts.items()
                         if g not in {c["grp"] for c in cands} and g not in busy and row["count"] + k <= cap),
                        key=lambda x: (x["count"], x["grp"]))
        busy_extras = sorted(g for g, row in counts.items() if g in busy and g not in {c["grp"] for c in cands}
                             and row["count"] + k <= cap)  # 本来坐得下、但正被另一轮占着的组
        others_waiting = sum(1 for t in r.waiting_teams() if t["id"] != team["id"])
        homeless = [m for m in members if m["grp"] is None]
        leader = team["leader_id"] == me["id"]
        if me["grp"] is None:
            todo = "你暂时无组：先在 LM Core 加入一个组，然后在下方更新组号和该组现在的人数。"
        elif not team["locked"]:
            todo = (("把邀请链接发给想一起组队的人；都加入并填好组号、本组人数后，点「成员已到齐，等待分配」。"
                     + (f"（至少 {MIN_TEAM} 人）" if k < MIN_TEAM else "")) if leader
                    else "等组长宣布成员到齐。核对一下自己的组号和本组人数是否准确。")
        elif homeless:
            todo = "等待暂无组的人更新组号：" + "、".join(m["nickname"] for m in homeless)
        elif team["paused"]:
            todo = ("上一份方案被否决或中止，本组暂停分配。调整后点「重新参与分配」。" if leader
                    else "上一份方案被否决或中止，本组暂停分配；等组长「重新参与分配」。")
        elif any(c["busy"] for c in cands):
            todo = "、".join(c["grp"] for c in cands if c["busy"]) + " 组正在另一轮分配中，等那一轮完成后再算。"
        elif busy_extras:
            todo = "、".join(busy_extras) + " 组坐得下，但正在另一轮分配中；等那一轮完成后再算。"
        else:
            short = "，".join(f"{c['grp']} 组还差 {c['short']} 个空位" for c in cands if not c["feasible"])
            todo = (f"现在还坐不下：{short}。人数一变（有人退出那些组、爬虫更新、或别的意向组和你们互换位置）就会自动分配"
                    + (f"；另有 {others_waiting} 个意向组在等，系统把大家一起算。" if others_waiting else "。"))
        return {
            "kind": "team", "code": team["code"], "team": team, "stage": "wait" if team["locked"] else "build",
            "is_leader": leader, "locked": bool(team["locked"]), "paused": bool(team["paused"]),
            "members": [{"id": m["id"], "name": m["nickname"], "grp": m["grp"],
                         "count": counts[m["grp"]]["count"] if m["grp"] in counts else None,
                         "leader": m["id"] == team["leader_id"]} for m in members],
            "candidates": cands, "extras": extras, "homeless": homeless, "others_waiting": others_waiting,
            "my_count": counts[me["grp"]]["count"] if me["grp"] in counts else None,
            "can_lock": leader and not team["locked"] and k >= MIN_TEAM and not homeless,
            "todo": escape(todo),
        }

    # ------------------------------------------------------------------ 整组池：组建

    def create_pool(self, member_id: str, lm_count: int) -> str:
        with self.store.tx() as r:
            me = self._release_done(r, r.member(member_id))
            if me["pool_id"]:  # 重复提交：已经建好了
                return r.pool(me["pool_id"])["code"]
            if me["team_id"]:
                raise UserError("你在一个意向组里，请先退出")
            pool_id, code = _new_id(), _new_code()
            r.insert_pool(pool_id, code)
            r.add_event(pool_id, f"{me['nickname']} 创建池")
            self._join(r, me, r.pool(pool_id), lm_count)
        return code

    def join_pool(self, member_id: str, code: str, lm_count: int) -> None:
        with self.store.tx() as r:
            me = r.member(member_id)
            pool = r.pool_by_code(code.strip().upper())
            if not pool or pool["mode"] != "pool":
                raise UserError("邀请码不存在，请检查是否输错")
            if me["pool_id"] == pool["id"]:  # 重复提交
                return
            me = self._release_done(r, me)
            if me["pool_id"]:
                raise UserError("你已经在另一个池里了，请先退出那个池")
            if me["team_id"]:
                raise UserError("你在一个意向组里，请先退出")
            if pool["state"] != "open":
                raise UserError("这个池已经生成了方案，不能再加入")
            self._join(r, me, pool, lm_count)

    def _join(self, r: Repo, me, pool, lm_count: int) -> None:
        grp = me["grp"]
        pg = r.pool_of_group(grp)
        if pg and pg["pool_id"] != pool["id"]:
            raise UserError(f"你们组（{grp}）已经在另一个池里了，一个组只能参加一个池")
        if pg:
            joined = sum(m["grp"] == grp for m in r.pool_members(pool["id"]))
            if joined >= pg["lm_count"]:
                raise UserError(f"{grp} 组在池里已经有 {joined} 人，等于登记的本组人数。"
                                "请检查你的组号是否填对；人数填少了可以让组里的人在池页面更正")
        else:
            if not 1 <= lm_count <= config.CAPACITY:
                raise UserError("组人数应在 1–5 之间")
            r.insert_pool_group(pool["id"], grp, lm_count)
        r.set_member_pool(me["id"], pool["id"])
        r.add_event(pool["id"], f"{me['nickname']}（{grp}）加入")
        r.bump(pool["id"])

    def _release_done(self, r: Repo, me):
        """在已完成的池 / 轮里 → 自动退出，返回刷新后的成员行。"""
        if me["pool_id"]:
            pool = r.pool(me["pool_id"])
            if pool["state"] == "done":
                self._leave_any(r, me, pool)
                return r.member(me["id"])
        return me

    def _leave_any(self, r: Repo, me, pool) -> None:
        if me["team_id"]:
            self._leave_team(r, me, r.team(me["team_id"]))
        else:
            self._leave(r, me, pool)

    def leave_pool(self, member_id: str) -> None:
        """退出池：互选阶段和完成后都可以；方案生成后到完成前不行（执行中请用"中止执行"）。"""
        with self.store.tx() as r:
            me = r.member(member_id)
            if not me or not me["pool_id"]:  # 重复提交
                return
            pool = r.pool(me["pool_id"])
            if pool["state"] not in ("open", "done"):
                raise UserError("方案生成以后不能退出池；执行出了问题可以点“中止执行”退回互选")
            self._leave_any(r, me, pool)

    def _leave(self, r: Repo, me, pool) -> None:
        r.delete_wishes_of(me["id"])
        r.set_member_pool(me["id"], None)
        rest = r.pool_members(pool["id"])
        if pool["state"] == "open" and me["grp"] and not any(m["grp"] == me["grp"] and not m["ghost"] for m in rest):
            r.delete_ghosts(pool["id"], me["grp"])  # 本组没有真人了：占位一起撤掉，组从池里移除
            r.delete_pool_group(pool["id"], me["grp"])
        if not any(not m["ghost"] for m in rest):
            r.delete_pool(pool["id"])
            self.chat.close_pool(pool["id"])
            return
        r.add_event(pool["id"], f"{me['nickname']}（{me['grp'] or '无组'}）退出")
        r.bump(pool["id"])

    def set_group_count(self, member_id: str, lm_count: int) -> None:
        """更正本组在 LM Core 上的人数。
        意向组：任何成员随时更新自己组的人数（两小时内有爬虫数据则以爬虫为准），人数一变就重新判断能否分配。
        整组池：首个加入者填错时用，只能在互选阶段改，不能少于已加入的人数。"""
        with self.store.tx() as r:
            me = r.member(member_id)
            if me and me["team_id"] and not me["pool_id"]:
                team = r.team(me["team_id"])
                self._report_count(r, me, team, lm_count)
                r.add_team_event(team["id"], f"{me['nickname']} 填报 {me['grp']} 组现有 {lm_count} 人")
                r.bump_team(team["id"])
                self._global_plan(r)
                return
            me, pool = self._my_pool(r, member_id)
            if pool["state"] != "open":
                raise UserError("方案已经生成，不能再改本组人数")
            if not me["grp"]:
                raise UserError("你现在没有组")
            joined = sum(m["grp"] == me["grp"] for m in r.pool_members(pool["id"]))
            if not max(1, joined) <= lm_count <= config.CAPACITY:
                raise UserError(f"本组人数应在 {max(1, joined)}–{config.CAPACITY} 之间（已加入 {joined} 人）")
            pg = r.pool_of_group(me["grp"])
            if pg["lm_count"] == lm_count:
                return
            r.set_group_count(pool["id"], me["grp"], lm_count)
            r.add_event(pool["id"], f"{me['nickname']} 把 {me['grp']} 组人数改为 {lm_count}")
            r.bump(pool["id"])

    def add_ghost(self, member_id: str) -> None:
        """给本组加一个占位：组里有人不肯加入时，用占位代表他——占着位置、不动、不参与互选和确认。"""
        with self.store.tx() as r:
            me, pool = self._my_pool(r, member_id, mode="pool")
            if pool["state"] != "open":
                raise UserError("方案已经生成，不能再加占位")
            if not me["grp"]:
                raise UserError("你现在没有组")
            members = r.pool_members(pool["id"])
            joined = sum(m["grp"] == me["grp"] for m in members)
            pg = r.pool_of_group(me["grp"])
            if joined >= pg["lm_count"]:
                raise UserError(f"{me['grp']} 组已经到齐（{joined}/{pg['lm_count']}），不需要占位")
            gid = _new_id()
            r.insert_member(gid, f"占位·{me['grp']}·{gid[:4]}", me["grp"], _hash(secrets.token_urlsafe(24)),
                            pool_id=pool["id"], ghost=True)
            r.add_event(pool["id"], f"{me['nickname']} 为 {me['grp']} 组添加占位（代表没加入的组员）")
            r.bump(pool["id"])

    def remove_ghost(self, member_id: str, ghost_id: str) -> None:
        with self.store.tx() as r:
            me, pool = self._my_pool(r, member_id, mode="pool")
            if pool["state"] != "open":
                raise UserError("方案已经生成，不能再改占位")
            g = r.member(ghost_id)
            if not g or not g["ghost"] or g["pool_id"] != pool["id"]:  # 已经删过了
                return
            if g["grp"] != me["grp"]:
                raise UserError("只能删除本组的占位")
            r.delete_member(ghost_id)
            r.add_event(pool["id"], f"{me['nickname']} 删除 {me['grp']} 组的一个占位")
            r.bump(pool["id"])

    def set_wish(self, member_id: str, to_id: str, want: bool) -> None:
        """设置（而不是切换）"想同组"，重复提交结果不变。"""
        with self.store.tx() as r:
            me, pool = self._my_pool(r, member_id, mode="pool")
            if pool["state"] != "open":
                raise UserError("方案已经生成，不能再修改互选")
            other = r.member(to_id)
            if not other or other["pool_id"] != pool["id"] or to_id == member_id or other["ghost"]:
                raise UserError("只能选择同一个池里的其他人")
            if want == r.has_wish(member_id, to_id):
                return
            if want and not self._all_present(r.pool_members(pool["id"]), r.pool_groups(pool["id"])):
                raise UserError("池里所有组全员到齐后才开放互选")
            if want:
                r.insert_wish(pool["id"], member_id, to_id)
                if r.has_wish(to_id, member_id):
                    r.add_event(pool["id"], f"{me['nickname']} 与 {other['nickname']} 互选")
            else:
                r.delete_wish(member_id, to_id)
            r.bump(pool["id"])

    # ------------------------------------------------------------------ 方案

    def make_plan(self, member_id: str) -> None:
        with self.store.tx() as r:
            me, pool = self._my_pool(r, member_id, mode="pool")
            if pool["state"] != "open":  # 别人刚生成了，或者重复提交
                return
            members = r.pool_members(pool["id"])
            groups = r.pool_groups(pool["id"])
            humans = [m for m in members if not m["ghost"]]
            all_teams = teams.build_teams([m["id"] for m in humans], r.wishes(pool["id"]))
            blockers = self._blockers(members, groups, all_teams)
            if blockers:
                raise UserError("还不能生成方案：" + "；".join(blockers))
            self._write_plan(r, pool, me, members, groups, all_teams)

    def _write_plan(self, r: Repo, pool, actor, members, groups, all_teams, target: dict | None = None) -> None:
        """算方案并写入：占位固定不动，其余人按小队分配。actor 为 None 表示系统自动生成。"""
        humans = [m for m in members if not m["ghost"]]
        current = {m["id"]: m["grp"] for m in humans}
        if target is None:
            fixed = Counter(m["grp"] for m in members if m["ghost"])
            try:
                target = solver.solve(current, [g["grp"] for g in groups], all_teams, config.CAPACITY, fixed)
            except solver.Infeasible as e:
                raise UserError(f"凑不出方案：{e}") from e
        in_team = {m for t in all_teams if len(t) > 1 for m in t}
        for m in members:
            if m["ghost"]:
                r.insert_plan(pool["id"], m["id"], m["grp"], m["grp"], False, approved=True)
            else:
                r.insert_plan(pool["id"], m["id"], m["grp"], target[m["id"]], m["id"] in in_team)
        movers = sum(current[m] != target[m] for m in current)
        if movers == 0:
            self._finish(r, pool, "当前分组已满足要求，无需移动")
        else:
            r.set_pool_state(pool["id"], "confirming")
            r.add_event(pool["id"], f"{actor['nickname'] if actor else '系统'}生成方案：移动 {movers} 人")
        r.bump(pool["id"])

    def approve(self, member_id: str) -> None:
        with self.store.tx() as r:
            me, pool = self._my_pool(r, member_id)
            if pool["state"] in ("executing", "done"):  # 已经全员同意了
                return
            if pool["state"] != "confirming":
                raise UserError("方案已经被退回，请看刷新后的页面")
            plan = {p["member_id"]: p for p in r.plan(pool["id"])}
            if plan[member_id]["approved"]:  # 重复提交
                return
            r.approve_plan(pool["id"], member_id)
            r.add_event(pool["id"], f"{me['nickname']} 同意")
            if all(p["approved"] or mid == member_id for mid, p in plan.items()):
                r.set_pool_state(pool["id"], "executing")
                r.add_event(pool["id"], "全员同意，开始执行")
            r.bump(pool["id"])

    def reject(self, member_id: str) -> None:
        with self.store.tx() as r:
            me, pool = self._my_pool(r, member_id)
            if pool["state"] == "open":  # 已经被退回了
                return
            if pool["state"] != "confirming":
                raise UserError("方案已经开始执行，不能再退回；执行出了问题请用“中止执行”")
            if pool["mode"] == "round":
                self._cancel_round(r, pool, me, False, f"{me['nickname']} 不同意，方案作废")
            else:
                r.delete_plan(pool["id"])
                r.set_pool_state(pool["id"], "open")
                r.add_event(pool["id"], f"{me['nickname']} 不同意，退回互选")
                r.bump(pool["id"])
        if pool["mode"] == "round":
            self.chat.close_pool(pool["id"])

    def abort(self, member_id: str) -> None:
        """执行中出了问题（有人操作错、失联、目标组被外面的人占了）：中止执行。
        整组池：退回互选，方案作废、互选保留；各组人数改成此刻实际在组里的人数；已经退出组的人暂时无组，
        要在 LM Core 加入池内某个组后在池页面更新组号，之后才能重新生成方案。
        意向组的一轮：方案作废，各组人数按此刻实际情况记录，各意向组回到等待（本组暂停，组长调整后重新参与）。"""
        with self.store.tx() as r:
            me, pool = self._my_pool(r, member_id)
            if pool["state"] == "open":  # 已经有人中止了
                return
            if pool["state"] != "executing":
                raise UserError("只有执行中才能中止")
            if pool["mode"] == "round":
                self._cancel_round(r, pool, me, True, f"{me['nickname']} 中止执行，方案作废；各组人数已按此刻实际情况记录")
            else:
                members = r.pool_members(pool["id"])
                counts = Counter(m["grp"] for m in members if m["grp"])
                for g in r.pool_groups(pool["id"]):
                    r.set_group_count(pool["id"], g["grp"], counts[g["grp"]])
                homeless = [m["nickname"] for m in members if m["grp"] is None]
                r.delete_plan(pool["id"])
                r.set_pool_state(pool["id"], "open")
                r.add_event(pool["id"], f"{me['nickname']} 中止执行，退回互选；各组人数已按当前状态更新"
                            + (f"；暂无组：{'、'.join(homeless)}" if homeless else ""))
                r.bump(pool["id"])
        self.chat.close_pool(pool["id"])

    # ------------------------------------------------------------------ 执行

    def act(self, member_id: str, action: str, seq: int | None = None) -> None:
        """确认自己在 LM Core 上完成了一步。seq = 页面渲染时我已完成的移动次数，用来识别重复提交。"""
        note = None
        with self.store.tx() as r:
            me, pool = self._my_pool(r, member_id)
            if pool["state"] == "done":
                return
            if pool["state"] != "executing":
                raise UserError("还没开始执行")
            st = self._exec_state(r, pool["id"])
            if seq is not None and st.done.get(member_id, 0) != seq:  # 这一步已经确认过了
                return
            if action == "leave" and st.cur[member_id] is None:  # 重复提交
                return
            acts = execution.next_actions(st.swaps, st.cur, st.done, st.in_team, config.CAPACITY)
            if acts.get(member_id, ("",))[0] != action:
                raise UserError("这一步现在还不能做（可能别人刚刚操作过），请看刷新后的提示")
            i = acts[member_id][1]
            swap = st.swaps[i]
            src, dst = swap.route(member_id)
            cur, done = execution.apply(st.swaps, st.cur, st.done, member_id, action, i)
            r.set_member_group(member_id, cur[member_id])
            r.set_moves_done(pool["id"], member_id, done.get(member_id, 0))
            r.add_event(pool["id"], {
                "move": f"{me['nickname']} {src}→{dst}",
                "leave": f"{me['nickname']} 退出 {src}（暂无组）",
                "join": f"{me['nickname']} 加入 {dst}",
            }[action])
            if partner := swap.partner(member_id):  # 告诉这次交换的对象：我做完了，接下来轮到谁
                note = DID_NOTES[action].format(src=src, dst=dst)
                nxt = execution.next_actions(st.swaps, cur, done, st.in_team, config.CAPACITY).get(partner)
                if nxt and nxt[1] == i:
                    psrc, pdst = swap.route(partner)
                    note += YOUR_TURN[nxt[0]].format(src=psrc, dst=pdst)
                elif execution.swap_done(i, st.swaps, done):
                    note += "这次交换完成。"
            finished = execution.is_done(st.swaps, done)
            if finished:
                self._finish(r, pool, "全部完成")
            r.bump(pool["id"])
        # 事务提交成功后才发消息；池完成后整池的临时对话直接删除
        if finished:
            self.chat.close_pool(pool["id"])
        elif note:
            self.chat.post(self.chat.open(pool["id"], member_id, partner), member_id, me["nickname"], note)

    def _finish(self, r: Repo, pool, text: str) -> None:
        """完成：各组释放，成员随时可以退出；方案和记录保留供查看。
        意向组的一轮：按方案结果记录涉及组的人数，然后立刻给等待中的其他意向组再算一次。"""
        pool_id = pool["id"]
        r.set_pool_state(pool_id, "done")
        if pool["mode"] == "round":
            final = Counter(p["to_grp"] for p in r.plan(pool_id))
            for g in r.pool_groups(pool_id):
                r.upsert_group_count(g["grp"], final[g["grp"]], "round")
            for t in r.teams_in_pool(pool_id):
                r.add_team_event(t["id"], text)
                r.bump_team(t["id"])
        r.delete_pool_groups(pool_id)
        r.add_event(pool_id, text + ("。退出意向组后可以再来一轮" if pool["mode"] == "round"
                                    else "。各组已释放，退出池后可以参加新的池"))
        if pool["mode"] == "round":
            self._global_plan(r)

    def send_quick(self, member_id: str, kind: str, pair: str) -> None:
        """在一对一对话里发一条预设短语。"""
        if kind not in QUICK_NOTES:
            raise UserError("未知的消息")
        if pair not in {p for p, _, _ in self.chat_peers(member_id)}:
            raise UserError("这个对话已经结束")
        with self.store.read() as r:
            me = r.member(member_id)
            st = self._exec_state(r, me["pool_id"])
        i = execution.current_swap(st.swaps, st.done, member_id)
        dst = st.swaps[i].route(member_id)[1] if i is not None else st.plan[member_id]["to_grp"]
        self.chat.post(pair, member_id, me["nickname"], QUICK_NOTES[kind][1].format(dst=dst))

    def send_cipher(self, member_id: str, pair: str, iv: str, ct: str) -> None:
        """转发一条端到端加密的消息（「加我微信细说」）。服务器只校验格式和大小，看不到内容。"""
        if not (_B64.match(iv or "") and _B64.match(ct or "") and len(iv) <= 24 and len(ct) <= CIPHER_MAX):
            raise UserError("消息格式不对")
        if pair not in {p for p, _, _ in self.chat_peers(member_id)}:
            raise UserError("这个对话已经结束")
        with self.store.read() as r:
            me = r.member(member_id)
        self.chat.post(pair, member_id, me["nickname"], cipher={"iv": iv, "ct": ct})

    def set_pubkey(self, member_id: str, pub: str) -> None:
        """登记浏览器生成的 ECDH 公钥。变了才 bump（让对方的页面拿到新公钥）；每次打开页面都会调，没变就什么都不做。"""
        pub = (pub or "").strip()
        if not (_B64.match(pub) and PUBKEY_LEN[0] <= len(pub) <= PUBKEY_LEN[1]):
            raise UserError("公钥格式不对")
        with self.store.tx() as r:
            me = r.member(member_id)
            if not me or me["e2e_pub"] == pub:
                return
            r.set_e2e_pub(member_id, pub)
            if me["pool_id"]:
                r.bump(me["pool_id"])

    def chat_peers(self, member_id: str) -> list[tuple[str, str, str | None]]:
        """我的一对一对话：只有一个，对象是我当前这次交换的另一个人（交换都做完后保留最后一次的对象）。
        返回 [(对话 id, 对方代号, 对方的端到端公钥或 None)]；不需要移动、或者单独进空位的人没有对话。"""
        with self.store.read() as r:
            me = r.member(member_id)
            if not me or not me["pool_id"]:
                return []
            pool = r.pool(me["pool_id"])
            if pool["state"] not in ("confirming", "executing"):
                return []
            st = self._exec_state(r, pool["id"])
            mine = execution.member_swaps(st.swaps).get(member_id, [])
            i = execution.current_swap(st.swaps, st.done, member_id)
            if i is None and mine:
                i = mine[-1]
            partner = st.swaps[i].partner(member_id) if i is not None else None
            peer = r.member(partner) if partner else None
        if not partner:
            return []
        return [(self.chat.open(pool["id"], member_id, partner), st.names[partner], peer["e2e_pub"] if peer else None)]

    def chat_since(self, member_id: str, after: int) -> list[dict]:
        """只返回当前对话里的新消息（换过搭档的人不会收到旧搭档发来、但页面上已经没有入口的消息）。"""
        msgs = self.chat.since(member_id, after)
        if not msgs:
            return []
        allowed = {p for p, _, _ in self.chat_peers(member_id)}
        return [m for m in msgs if m["pair"] in allowed]

    # ------------------------------------------------------------------ 查询

    def pool_rev(self, member_id: str) -> int | None:
        """页面轮询用的版本号：在轮 / 池里看池的 rev（轮加偏移），只在意向组里看意向组的 rev。"""
        with self.store.read() as r:
            me = r.member(member_id)
            if not me:
                return None
            if me["pool_id"]:
                pool = r.pool(me["pool_id"])
                return pool["rev"] + (ROUND_REV_BASE if pool["mode"] == "round" else 0)
            return r.team_rev(me["team_id"]) if me["team_id"] else None

    def pool_view(self, member_id: str) -> dict | None:
        """页面需要的全部数据（只读，同一快照）：意向组等待中 / 整组池互选中 / 方案确认、执行、完成。"""
        with self.store.read() as r:
            me = r.member(member_id)
            if not me or not (me["pool_id"] or me["team_id"]):
                return None
            team = r.team(me["team_id"]) if me["team_id"] else None
            if not me["pool_id"]:
                view = {"pool": {"state": "open", "rev": team["rev"], "mode": "round"}, "rev": team["rev"], "me": me,
                        "events": r.team_events(team["id"]), "lmcore_url": config.LMCORE_URL, "teams": [], "my_team": []}
                view.update(self._team_view(r, me, team))
                return view
            pool = r.pool(me["pool_id"])
            members = r.pool_members(pool["id"])
            groups = r.pool_groups(pool["id"])
            wishes = r.wishes(pool["id"])
            st = self._exec_state(r, pool["id"])
            events = list(r.events(pool["id"]))
            if team:
                events = sorted(events + list(r.team_events(team["id"])), key=lambda e: (e["at"], e["id"]), reverse=True)[:40]
            teammates = [m["nickname"] for m in r.team_members(team["id"])] if team else []

        names = {m["id"]: _display(m, pool["mode"]) for m in members}
        all_teams = teams.build_teams([m["id"] for m in members if not m["ghost"]], wishes)
        view = {
            "pool": pool, "me": me, "names": names, "events": events, "lmcore_url": config.LMCORE_URL,
            "rev": pool["rev"] + (ROUND_REV_BASE if pool["mode"] == "round" else 0),
            "kind": "team" if pool["mode"] == "round" else "pool", "code": team["code"] if team else pool["code"],
            "team": team, "stage": pool["state"], "is_leader": bool(team and team["leader_id"] == member_id),
            "teams": [[names[i] for i in t] for t in all_teams if len(t) > 1],
            "my_team": teammates or next(([names[i] for i in t] for t in all_teams if member_id in t), []),
        }
        if pool["state"] == "open":
            view.update(self._open_view(me, members, groups, wishes, all_teams))
        elif st.plan:
            view.update(self._plan_view(member_id, st, pool["state"]))
        return view

    def _open_view(self, me, members, groups, wishes, all_teams) -> dict:
        mid = me["id"]
        mine = {b for a, b in wishes if a == mid}
        joined = Counter(m["grp"] for m in members if m["grp"])
        missing = {g["grp"]: g["lm_count"] - joined[g["grp"]] for g in groups if joined[g["grp"]] < g["lm_count"]}
        homeless = [m for m in members if m["grp"] is None]
        blockers = self._blockers(members, groups, all_teams)
        in_team = any(len(t) > 1 and mid in t for t in all_teams)
        wishes_open = self._all_present(members, groups)
        if me["grp"] is None:
            todo = "你暂时无组：先在 LM Core 加入池内的某个组，然后在下方更新组号。"
        elif len(groups) < MIN_GROUPS:
            todo = f"邀请其他组加入：至少需要 {MIN_GROUPS} 个组。"
        elif me["grp"] in missing:
            todo = (f"把邀请链接发到 {me['grp']} 组群：还差 {missing[me['grp']]} 人。"
                    "有人不肯加入可以添加占位，人数填错可以更正。")
        elif homeless:
            todo = "等待暂无组的人更新组号：" + "、".join(m["nickname"] for m in homeless)
        elif not wishes_open:
            todo = "等待所有组到齐后开放互选：" + "，".join(f"{g} 还差 {n} 人" for g, n in missing.items())
        elif not mine and not in_team:
            todo = "勾选想同组的人（双方互选才生效）。不选则由系统安排。"
        elif missing:
            todo = "等待其他组加入：" + "，".join(f"{g} 还差 {n} 人" for g, n in missing.items())
        elif blockers:
            todo = blockers[0]
        else:
            todo = "条件已满足，可以生成方案。"
        return {
            "groups": [
                {"grp": g["grp"], "lm_count": g["lm_count"],
                 "members": [m for m in members if m["grp"] == g["grp"]], "mine": g["grp"] == me["grp"]}
                for g in groups
            ],
            "homeless": homeless,
            "pool_group_names": [g["grp"] for g in groups],
            "wishes_open": wishes_open,
            "my_wishes": mine,
            "wished_by": {a for a, b in wishes if b == mid},
            "blockers": blockers,
            "checks": [
                (len(groups) >= MIN_GROUPS, f"至少 {MIN_GROUPS} 个组（现 {len(groups)} 个）"),
                (not missing and not homeless, "各组成员到齐" if not missing
                 else "各组成员到齐（" + "，".join(f"{g} 还差 {n} 人" for g, n in missing.items()) + "）"),
                (not teams.oversized(all_teams), "小队不超过 5 人"),
            ],
            "todo": escape(todo),
        }

    def _plan_view(self, member_id, st: ExecState, state) -> dict:
        names = st.names
        actions = (execution.next_actions(st.swaps, st.cur, st.done, st.in_team, config.CAPACITY)
                   if state == "executing" else {})
        order = execution.member_swaps(st.swaps)
        rows = []
        for mid, p in st.plan.items():
            moved = p["from_grp"] != p["to_grp"]
            mine = order.get(mid, [])
            if not moved:
                status = "stay"
            elif st.done.get(mid, 0) >= len(mine):
                status = "done"
            elif mid in actions:
                status = actions[mid][0]
            elif st.cur[mid] is None:
                status = "homeless"
            else:
                status = "wait"
            steps = [(*st.swaps[i].route(mid), names.get(st.swaps[i].partner(mid))) for i in mine]
            now = execution.current_swap(st.swaps, st.done, mid)
            rows.append({
                "id": mid, "name": names[mid], "from": p["from_grp"], "to": p["to_grp"], "cur": st.cur[mid],
                "moved": moved, "approved": bool(p["approved"]), "status": status, "steps": steps,
                "partners": [name for _, _, name in steps if name],
                "partner": names.get(st.swaps[now].partner(mid)) if now is not None else None,
            })
        rows.sort(key=lambda x: (x["to"], not x["moved"], x["name"]))
        by_id = {x["id"]: x for x in rows}
        humans = [x for x in rows if x["name"] not in (GHOST_NAME, ROUND_GHOST_NAME)]
        new_groups: dict[str, list] = {}
        for row in humans:
            new_groups.setdefault(row["to"], []).append(row)
        for grp, n in Counter(x["to"] for x in rows if x["name"] in (GHOST_NAME, ROUND_GHOST_NAME)).items():
            label = next(x["name"] for x in rows if x["to"] == grp and x["name"] in (GHOST_NAME, ROUND_GHOST_NAME))
            new_groups.setdefault(grp, []).append({  # 占位 / 组内其他人：合成一行
                "id": f"ghosts-{grp}", "name": label + (f" ×{n}" if n > 1 else ""), "from": grp, "to": grp, "cur": grp,
                "moved": False, "approved": True, "status": "stay", "steps": [], "partners": [], "partner": None,
                "ghost": True, "count": n})
        new_groups = dict(sorted(new_groups.items()))
        group_sizes = {g: sum(x.get("count", 1) for x in rs) for g, rs in new_groups.items()}  # 真人 + 占位的人数

        mine = by_id[member_id]
        my_action = actions.get(member_id)
        movers = [x for x in rows if x["moved"]]
        not_approved = [x["name"] for x in humans if not x["approved"]]
        if state == "confirming":
            todo = ("确认方案：同意，或不同意（退回）。"
                    if not mine["approved"] else f"已同意，等待 {len(not_approved)} 人。")
        elif state == "executing":
            if my_action:
                todo = "轮到你：按下方提示在 LM Core 操作，保存后点确认。"
            elif mine["status"] in ("done", "stay"):
                todo = "你已完成，无需操作。"
            else:
                todo = "等待中。轮到你时此处会提示，不要提前操作。"
        else:
            todo = "已完成。请在 LM Core 核对 Your selection。想再换一轮请先退出。"
        instruction = None
        if my_action:
            src, dst = st.swaps[my_action[1]].route(member_id)
            instruction = INSTRUCTIONS[my_action[0]].format(cur=src, to=dst)
        return {
            "new_groups": new_groups,
            "group_sizes": group_sizes,
            "mine": mine,
            "swap_text": self._swap_text(mine),
            "teammates": [x["name"] for x in new_groups[mine["to"]] if x["id"] != member_id],  # 占位合成一项
            "my_action": my_action[0] if my_action else None,
            "my_step": st.swaps[my_action[1]].route(member_id) if my_action else None,
            "my_seq": st.done.get(member_id, 0),
            "instruction": instruction,
            "approved_count": len(humans) - len(not_approved),  # 只算真人：占位不需要表态
            "total": len(humans),
            "not_approved": not_approved,
            "movers_total": len(movers),
            "movers_done": sum(x["status"] == "done" for x in movers),
            "swaps": sum(1 for s in st.swaps if s.b),
            "live_counts": dict(sorted(Counter(g for g in st.cur.values() if g).items())),
            "todo": escape(todo),
        }

    @staticmethod
    def _swap_text(row: dict) -> str:
        """给模板用 |safe 渲染的 HTML 片段：代号是用户输入，必须 escape。"""
        if not row["moved"]:
            return f"留在 {escape(row['to'])} 组，无需操作。"
        parts = [f"与 <b>{escape(name)}</b> 互换（你 {escape(src)}→{escape(dst)}）" if name
                 else f"你 {escape(src)}→{escape(dst)}（进空位，单独移动）"
                 for src, dst, name in row["steps"]]
        if len(parts) == 1:
            return parts[0] + "。"
        return f"分 {len(parts)} 步：" + "；然后".join(parts) + "。"

    # ------------------------------------------------------------------ 管理

    def stats(self) -> dict:
        with self.store.read() as r:
            pools = r.all_pools()
            return {"members": len(r.all_members()), "pools": Counter(p["state"] for p in pools),
                    "pool_list": pools, "team_list": r.all_teams(), "counts": r.all_group_counts()}

    def progress(self) -> dict:
        """管理页：全站换组进度。每个意向组在哪个阶段、同意 / 到位几个人、为什么还在等；每一轮的进度；
        各组人数和数据来源；最近动态。"""
        with self.store.read() as r:
            counts = {row["grp"]: row for row in r.all_group_counts()}
            busy = r.active_round_groups()
            pools = {p["id"]: p for p in r.round_pools()}
            round_info = {pid: self._round_progress(r, p) for pid, p in pools.items()}
            stage_tot = {k: {"teams": 0, "people": 0} for k in STAGE_LABEL}
            teams, in_teams, team_of = [], 0, {}
            for t in r.all_teams():
                ms = r.team_members(t["id"])
                in_teams += len(ms)
                for m in ms:
                    team_of[m["id"]] = t["code"]
                pool = pools.get(t["pool_id"]) if t["pool_id"] else None
                stage = pool["state"] if pool else ("paused" if t["paused"] else "wait") if t["locked"] else "build"
                stage_tot[stage]["teams"] += 1
                stage_tot[stage]["people"] += len(ms)
                info = round_info.get(t["pool_id"]) if pool else None
                plan = info["plan"] if info else {}
                mids = [m["id"] for m in ms]
                targets = Counter(plan[mid]["to_grp"] for mid in mids if mid in plan)
                movers = [mid for mid in mids if mid in plan and plan[mid]["from_grp"] != plan[mid]["to_grp"]]
                note = ""
                if stage in ("build", "wait", "paused") and ms:
                    leader = next((m for m in ms if m["id"] == t["leader_id"]), ms[0])
                    note = str(self._team_view(r, leader, t)["todo"])
                leader_row = r.member(t["leader_id"]) if t["leader_id"] else None
                events = r.team_events(t["id"], 1)
                teams.append({
                    "id": t["id"], "code": t["code"], "leader": leader_row["nickname"] if leader_row else "—",
                    "members": [f"{m['nickname']}@{m['grp'] or '无组'}" for m in ms], "size": len(ms),
                    "stage": stage, "stage_label": STAGE_LABEL[stage], "round": pool["code"] if pool else None,
                    "target": targets.most_common(1)[0][0] if targets else None,
                    "approved": sum(1 for mid in mids if mid in plan and plan[mid]["approved"]) if stage == "confirming" else None,
                    "moved": sum(1 for mid in movers if mid in info["done_ids"]) if info else None,
                    "movers": len(movers) if info else None,
                    "note": note, "updated_at": events[0]["at"] if events else t["created_at"],
                })
            rounds = []
            for pid, p in pools.items():
                info = round_info[pid]
                rounds.append({
                    "id": pid, "code": p["code"], "state": p["state"], "state_label": STAGE_LABEL.get(p["state"], p["state"]),
                    "teams": [t["code"] for t in r.teams_in_pool(pid)], "groups": info["groups"],
                    "approved": info["approved"], "humans": info["humans"],
                    "moved": info["moved"], "movers": info["movers"], "created_at": p["created_at"],
                })
            members = r.all_members()
            in_grp: dict[str, list[str]] = {}
            for m in members:
                if m["team_id"] and m["grp"]:
                    in_grp.setdefault(m["grp"], []).append(f"{m['nickname']}（{team_of.get(m['id'], '?')}）")
            groups = [{
                "grp": g, "count": counts[g]["count"] if g in counts else None,
                "source": counts[g]["source"] if g in counts else None, "at": counts[g]["at"] if g in counts else None,
                "busy": g in busy, "members": in_grp.get(g, []),
            } for g in sorted(set(counts) | set(in_grp), key=lambda x: (len(x), x))]
            events = sorted(
                [{"at": e["at"], "text": e["text"], "code": e["code"], "kind": "team"} for e in r.recent_team_events()]
                + [{"at": e["at"], "text": e["text"], "code": e["code"], "kind": "round"} for e in r.recent_round_events()],
                key=lambda e: e["at"], reverse=True)[:30]
        done = [x for x in rounds if x["state"] == "done"]
        executing = [x for x in rounds if x["state"] == "executing"]
        return {
            "members": len(members), "in_teams": in_teams, "stages": stage_tot,
            "summary": {
                "done_rounds": len(done), "done_teams": sum(len(x["teams"]) for x in done),
                "done_people": sum(stage_tot["done"]["people"] for _ in [0]), "done_moved": sum(x["movers"] for x in done),
                "executing_rounds": len(executing), "executing_moved": sum(x["moved"] for x in executing),
                "executing_movers": sum(x["movers"] for x in executing),
            },
            "teams": teams, "rounds": rounds, "groups": groups, "events": events,
        }

    def _round_progress(self, r: Repo, pool) -> dict:
        """一轮的进度：方案、真人同意数、需要移动的人数、已到位的人。"""
        st = self._exec_state(r, pool["id"])
        order = execution.member_swaps(st.swaps)
        members = r.plan_members(pool["id"]) if st.plan else r.pool_members(pool["id"])
        humans = [m for m in members if not m["ghost"]]
        movers = [m["id"] for m in humans if m["id"] in st.plan and st.plan[m["id"]]["from_grp"] != st.plan[m["id"]]["to_grp"]]
        done_ids = {mid for mid in movers if st.done.get(mid, 0) >= len(order.get(mid, []))}
        groups = sorted({p["to_grp"] for p in st.plan.values()} | {p["from_grp"] for p in st.plan.values()}) if st.plan \
            else [g["grp"] for g in r.pool_groups(pool["id"])]
        return {"plan": st.plan, "humans": len(humans), "approved": sum(1 for m in humans if m["id"] in st.plan and st.plan[m["id"]]["approved"]),
                "movers": len(movers), "moved": len(done_ids), "done_ids": done_ids, "groups": groups}

    def delete_pool(self, pool_id: str) -> None:
        """管理员删除一个池 / 轮：成员回到未加入状态，方案、互选、记录全部删除。轮里的意向组回到等待。"""
        with self.store.tx() as r:
            pool = r.pool(pool_id)
            if pool:
                for t in r.teams_in_pool(pool_id):
                    r.set_team(t["id"], pool_id=None)
                    r.add_team_event(t["id"], "管理员删除了这一轮")
                    r.bump_team(t["id"])
                r.delete_pool(pool_id)
        self.chat.close_pool(pool_id)

    def delete_team(self, team_id: str) -> None:
        """管理员删除一个意向组：成员回到未加入状态；正在进行的那一轮一并删除。"""
        with self.store.tx() as r:
            team = r.team(team_id)
            if not team:
                return
            pool_id = team["pool_id"]
            if pool_id:
                for t in r.teams_in_pool(pool_id):
                    r.set_team(t["id"], pool_id=None)
                    if t["id"] != team_id:
                        r.add_team_event(t["id"], "管理员删除了这一轮")
                        r.bump_team(t["id"])
                for m in r.pool_members(pool_id):
                    if not m["ghost"]:
                        r.set_member_pool(m["id"], None)
                r.delete_pool(pool_id)
            r.delete_team(team_id)
        if pool_id:
            self.chat.close_pool(pool_id)

    def purge(self) -> None:
        with self.store.tx() as r:
            r.purge_all()
        self.store.vacuum()
        self.chat.clear()

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _check_group(grp: str) -> None:
        if grp not in config.GROUPS:
            raise UserError("组号不存在")

    @staticmethod
    def _my_pool(r: Repo, member_id: str, mode: str | None = None):
        me = r.member(member_id)
        if not me or not me["pool_id"]:
            raise UserError("你还没有加入任何池")
        pool = r.pool(me["pool_id"])
        if mode and pool["mode"] != mode:
            raise UserError("意向组的方案由系统统一分配，没有互选 / 占位这些操作")
        return me, pool

    @staticmethod
    def _exec_state(r: Repo, pool_id: str) -> ExecState:
        pool = r.pool(pool_id)
        plan = {p["member_id"]: p for p in r.plan(pool_id)}
        moves = {m: (p["from_grp"], p["to_grp"]) for m, p in plan.items() if p["from_grp"] != p["to_grp"]}
        in_team = {m for m, p in plan.items() if p["in_team"]}
        start = Counter(p["from_grp"] for p in plan.values())  # 方案生成时各组人数（池是封闭的）
        final = Counter(p["to_grp"] for p in plan.values())
        free = {g for g in start if max(start[g], final[g]) < config.CAPACITY}  # 执行全程都有空位的组
        members = r.plan_members(pool_id) if plan else r.pool_members(pool_id)
        # 完成后退出池的人：此刻的组以方案里的目标组为准（他可能已经去了别的池）
        cur = {m["id"]: m["grp"] if m["pool_id"] == pool_id else plan[m["id"]]["to_grp"] for m in members}
        return ExecState(
            swaps=pairing.build_swaps(moves, in_team, free),
            cur=cur,
            done={m: p["moves_done"] for m, p in plan.items()},
            in_team=in_team,
            plan=plan,
            names={m["id"]: _display(m, pool["mode"] if pool else "pool") for m in members},
        )

    @staticmethod
    def _all_present(members, groups) -> bool:
        """池里至少有 MIN_GROUPS 个组，每个组的全部成员（含占位）都已加入，并且没有人暂时无组。"""
        joined = Counter(m["grp"] for m in members if m["grp"])
        return (len(groups) >= MIN_GROUPS and all(m["grp"] for m in members)
                and all(joined[g["grp"]] >= g["lm_count"] for g in groups))

    @staticmethod
    def _blockers(members, groups, all_teams) -> list[str]:
        blockers = []
        if len(groups) < MIN_GROUPS:
            blockers.append(f"至少需要 {MIN_GROUPS} 个组")
        for g in groups:
            joined = sum(m["grp"] == g["grp"] for m in members)
            if joined < g["lm_count"]:
                blockers.append(f"{g['grp']} 组还差 {g['lm_count'] - joined} 人（{joined}/{g['lm_count']}）")
        homeless = [m["nickname"] for m in members if m["grp"] is None]
        if homeless:
            blockers.append("暂无组（需先在 LM Core 加入池内的组并更新组号）：" + "、".join(homeless))
        names = {m["id"]: _display(m) for m in members}
        for t in teams.oversized(all_teams):
            blockers.append("小队超过 5 人：" + "、".join(names[i] for i in t) + "（需取消部分互选）")
        return blockers


def _display(m, mode: str = "pool") -> str:
    if not m["ghost"]:
        return m["nickname"]
    return ROUND_GHOST_NAME if mode == "round" else GHOST_NAME
