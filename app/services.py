"""用例编排：读数据 → 调 domain 纯逻辑 → 在同一个事务里写回。

Web 层只调用这里；这里不关心 HTTP，也不写 SQL。

并发约定
- 每个写用例都在一个 BEGIN IMMEDIATE 事务里"重新读取最新状态 → 校验 → 写入"，
  不信任页面上可能已经过期的信息。
- 所有用例都是幂等的：双击、网络重试、多个标签页重复提交，结果都和只提交一次相同。
"""
import hashlib
import secrets
import sqlite3
from collections import Counter
from typing import NamedTuple

from . import config
from .domain import execution, pairing, solver, teams
from .chat import Chat
from .store import Repo, Store

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉易混淆的 I L O 0 1


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

    def change_group(self, member_id: str, grp: str) -> None:
        self._check_group(grp)
        with self.store.tx() as r:
            if r.member(member_id)["pool_id"]:
                raise UserError("已经在池里时不能改组号，请先退出池")
            r.set_member_group(member_id, grp)

    def login_as(self, member_id: str) -> str:
        """开发模式：切换身份（给该成员换一个新 token）。"""
        token = secrets.token_urlsafe(24)
        with self.store.tx() as r:
            if not r.member(member_id):
                raise UserError("没有这个成员")
            r.set_member_token(member_id, _hash(token))
        return token

    def identities(self):
        with self.store.read() as r:
            return r.all_members()

    def pool_exists(self, code: str) -> bool:
        with self.store.read() as r:
            return r.pool_by_code(code.strip().upper()) is not None

    # ------------------------------------------------------------------ 池：组建

    def create_pool(self, member_id: str, lm_count: int) -> str:
        with self.store.tx() as r:
            me = r.member(member_id)
            if me["pool_id"]:  # 重复提交：已经建好了
                return r.pool(me["pool_id"])["code"]
            pool_id, code = _new_id(), _new_code()
            r.insert_pool(pool_id, code)
            r.add_event(pool_id, f"{me['nickname']} 创建池")
            self._join(r, me, r.pool(pool_id), lm_count)
        return code

    def join_pool(self, member_id: str, code: str, lm_count: int) -> None:
        with self.store.tx() as r:
            me = r.member(member_id)
            pool = r.pool_by_code(code.strip().upper())
            if not pool:
                raise UserError("邀请码不存在，请检查是否输错")
            if me["pool_id"] == pool["id"]:  # 重复提交
                return
            if me["pool_id"]:
                raise UserError("你已经在另一个池里了，请先退出那个池")
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
                raise UserError(f"{grp} 组在池里已经有 {joined} 人，等于 LM Core 上的人数。请检查你的组号是否填对")
        else:
            if not 1 <= lm_count <= config.CAPACITY:
                raise UserError("组人数应在 1–5 之间")
            r.insert_pool_group(pool["id"], grp, lm_count)
        r.set_member_pool(me["id"], pool["id"])
        r.add_event(pool["id"], f"{me['nickname']}（{grp}）加入")
        r.bump(pool["id"])

    def leave_pool(self, member_id: str) -> None:
        with self.store.tx() as r:
            me = r.member(member_id)
            if not me["pool_id"]:  # 重复提交
                return
            pool = r.pool(me["pool_id"])
            if pool["state"] != "open":
                raise UserError("方案生成以后不能退出池")
            r.delete_wishes_of(member_id)
            r.set_member_pool(member_id, None)
            rest = r.pool_members(pool["id"])
            if not any(m["grp"] == me["grp"] for m in rest):
                r.delete_pool_group(pool["id"], me["grp"])
            if not rest:
                r.delete_pool(pool["id"])
                return
            r.add_event(pool["id"], f"{me['nickname']}（{me['grp']}）退出")
            r.bump(pool["id"])

    def set_wish(self, member_id: str, to_id: str, want: bool) -> None:
        """设置（而不是切换）"想同组"，重复提交结果不变。"""
        with self.store.tx() as r:
            me, pool = self._my_pool(r, member_id)
            if pool["state"] != "open":
                raise UserError("方案已经生成，不能再修改互选")
            other = r.member(to_id)
            if not other or other["pool_id"] != pool["id"] or to_id == member_id:
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

    # ------------------------------------------------------------------ 池：方案

    def make_plan(self, member_id: str) -> None:
        with self.store.tx() as r:
            me, pool = self._my_pool(r, member_id)
            if pool["state"] != "open":  # 别人刚生成了，或者重复提交
                return
            members = r.pool_members(pool["id"])
            groups = r.pool_groups(pool["id"])
            all_teams = teams.build_teams([m["id"] for m in members], r.wishes(pool["id"]))
            blockers = self._blockers(members, groups, all_teams)
            if blockers:
                raise UserError("还不能生成方案：" + "；".join(blockers))
            current = {m["id"]: m["grp"] for m in members}
            try:
                target = solver.solve(current, [g["grp"] for g in groups], all_teams, config.CAPACITY)
            except solver.Infeasible as e:
                raise UserError(f"这个池凑不出方案：{e}") from e
            in_team = {m for t in all_teams if len(t) > 1 for m in t}
            for m in members:
                r.insert_plan(pool["id"], m["id"], m["grp"], target[m["id"]], m["id"] in in_team)
            movers = sum(current[m] != target[m] for m in current)
            if movers == 0:
                r.set_pool_state(pool["id"], "done")
                r.add_event(pool["id"], "当前分组已满足所有互选，无需移动")
            else:
                r.set_pool_state(pool["id"], "confirming")
                r.add_event(pool["id"], f"{me['nickname']} 生成方案：移动 {movers} 人")
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
                raise UserError("方案已经开始执行，不能再退回")
            r.delete_plan(pool["id"])
            r.set_pool_state(pool["id"], "open")
            r.add_event(pool["id"], f"{me['nickname']} 不同意，退回互选")
            r.bump(pool["id"])

    # ------------------------------------------------------------------ 池：执行

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
                r.set_pool_state(pool["id"], "done")
                r.add_event(pool["id"], "全部完成")
            r.bump(pool["id"])
        # 事务提交成功后才发消息；池完成后整池的临时对话直接删除
        if finished:
            self.chat.close_pool(pool["id"])
        elif note:
            self.chat.post(self.chat.open(pool["id"], member_id, partner), member_id, me["nickname"], note)

    def send_quick(self, member_id: str, kind: str, pair: str) -> None:
        """在一对一对话里发一条预设短语。"""
        if kind not in QUICK_NOTES:
            raise UserError("未知的消息")
        chats = dict(self.chat_peers(member_id))
        if pair not in chats:
            raise UserError("这个对话已经结束")
        with self.store.read() as r:
            me = r.member(member_id)
            _, dst = r.plan_row(me["pool_id"], member_id)
        self.chat.post(pair, member_id, me["nickname"], QUICK_NOTES[kind][1].format(dst=dst))

    def chat_peers(self, member_id: str) -> list[tuple[str, str]]:
        """我的一对一对话：只有一个，对象是我当前这次交换的另一个人（交换都做完后保留最后一次的对象）。
        返回 [(对话 id, 对方代号)]；不需要移动、或者单独进空位的人没有对话。"""
        with self.store.read() as r:
            me = r.member(member_id)
            if not me or not me["pool_id"]:
                return []
            pool = r.pool(me["pool_id"])
            if pool["state"] not in ("confirming", "executing"):
                return []
            st = self._exec_state(r, pool["id"])
            names = {m["id"]: m["nickname"] for m in r.pool_members(pool["id"])}
        mine = execution.member_swaps(st.swaps).get(member_id, [])
        i = execution.current_swap(st.swaps, st.done, member_id)
        if i is None and mine:
            i = mine[-1]
        partner = st.swaps[i].partner(member_id) if i is not None else None
        return [(self.chat.open(pool["id"], member_id, partner), names[partner])] if partner else []

    def chat_since(self, member_id: str, after: int) -> list[dict]:
        return self.chat.since(member_id, after)

    # ------------------------------------------------------------------ 查询

    def pool_rev(self, member_id: str) -> int | None:
        with self.store.read() as r:
            me = r.member(member_id)
            return r.pool_rev(me["pool_id"]) if me and me["pool_id"] else None

    def pool_view(self, member_id: str) -> dict | None:
        """池页面需要的全部数据（只读，同一快照）。"""
        with self.store.read() as r:
            me = r.member(member_id)
            if not me or not me["pool_id"]:
                return None
            pool = r.pool(me["pool_id"])
            members = r.pool_members(pool["id"])
            groups = r.pool_groups(pool["id"])
            wishes = r.wishes(pool["id"])
            st = self._exec_state(r, pool["id"])
            events = r.events(pool["id"])

        names = {m["id"]: m["nickname"] for m in members}
        all_teams = teams.build_teams(list(names), wishes)
        view = {
            "pool": pool,
            "me": me,
            "names": names,
            "events": events,
            "lmcore_url": config.LMCORE_URL,
            "teams": [[names[i] for i in t] for t in all_teams if len(t) > 1],
            "my_team": next(([names[i] for i in t] for t in all_teams if member_id in t), []),
        }
        if pool["state"] == "open":
            view.update(self._open_view(me, members, groups, wishes, all_teams))
        elif st.plan:
            view.update(self._plan_view(member_id, names, st, pool["state"]))
        return view

    def _open_view(self, me, members, groups, wishes, all_teams) -> dict:
        mid = me["id"]
        mine = {b for a, b in wishes if a == mid}
        joined = Counter(m["grp"] for m in members)
        missing = {g["grp"]: g["lm_count"] - joined[g["grp"]] for g in groups if joined[g["grp"]] < g["lm_count"]}
        blockers = self._blockers(members, groups, all_teams)
        in_team = any(len(t) > 1 and mid in t for t in all_teams)
        wishes_open = self._all_present(members, groups)
        if len(groups) < MIN_GROUPS:
            todo = f"邀请其他组加入：至少需要 {MIN_GROUPS} 个组。"
        elif me["grp"] in missing:
            todo = f"把邀请链接发到 {me['grp']} 组群：还差 {missing[me['grp']]} 人。"
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
                {"grp": g["grp"], "lm_count": g["lm_count"], "members": [m for m in members if m["grp"] == g["grp"]]}
                for g in groups
            ],
            "wishes_open": wishes_open,
            "my_wishes": mine,
            "wished_by": {a for a, b in wishes if b == mid},
            "blockers": blockers,
            "checks": [
                (len(groups) >= MIN_GROUPS, f"至少 {MIN_GROUPS} 个组（现 {len(groups)} 个）"),
                (not missing, "各组成员到齐" if not missing
                 else "各组成员到齐（" + "，".join(f"{g} 还差 {n} 人" for g, n in missing.items()) + "）"),
                (not teams.oversized(all_teams), "小队不超过 5 人"),
            ],
            "todo": todo,
        }

    def _plan_view(self, member_id, names, st: ExecState, state) -> dict:
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
        new_groups: dict[str, list] = {}
        for row in rows:
            new_groups.setdefault(row["to"], []).append(row)

        mine = by_id[member_id]
        my_action = actions.get(member_id)
        movers = [x for x in rows if x["moved"]]
        not_approved = [x["name"] for x in rows if not x["approved"]]
        if state == "confirming":
            todo = ("确认方案：同意，或不同意（退回互选）。"
                    if not mine["approved"] else f"已同意，等待 {len(not_approved)} 人。")
        elif state == "executing":
            if my_action:
                todo = "轮到你：按下方提示在 LM Core 操作，保存后点确认。"
            elif mine["status"] in ("done", "stay"):
                todo = "你已完成，无需操作。"
            else:
                todo = "等待中。轮到你时此处会提示，不要提前操作。"
        else:
            todo = "已完成。请在 LM Core 核对 Your selection。"
        instruction = None
        if my_action:
            src, dst = st.swaps[my_action[1]].route(member_id)
            instruction = INSTRUCTIONS[my_action[0]].format(cur=src, to=dst)
        return {
            "new_groups": new_groups,
            "mine": mine,
            "swap_text": self._swap_text(mine),
            "teammates": [x["name"] for x in new_groups[mine["to"]] if x["id"] != member_id],
            "my_action": my_action[0] if my_action else None,
            "my_step": st.swaps[my_action[1]].route(member_id) if my_action else None,
            "my_seq": st.done.get(member_id, 0),
            "instruction": instruction,
            "approved_count": len(rows) - len(not_approved),
            "total": len(rows),
            "not_approved": not_approved,
            "movers_total": len(movers),
            "movers_done": sum(x["status"] == "done" for x in movers),
            "swaps": sum(1 for s in st.swaps if s.b),
            "live_counts": dict(sorted(Counter(g for g in st.cur.values() if g).items())),
            "todo": todo,
        }

    @staticmethod
    def _swap_text(row: dict) -> str:
        if not row["moved"]:
            return f"留在 {row['to']} 组，无需操作。"
        parts = [f"与 <b>{name}</b> 互换（你 {src}→{dst}）" if name else f"你 {src}→{dst}（进空位，单独移动）"
                 for src, dst, name in row["steps"]]
        if len(parts) == 1:
            return parts[0] + "。"
        return f"分 {len(parts)} 步：" + "；然后".join(parts) + "。"

    # ------------------------------------------------------------------ 管理

    def stats(self) -> dict:
        with self.store.read() as r:
            pools = r.all_pools()
            return {"members": len(r.all_members()), "pools": Counter(p["state"] for p in pools)}

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
    def _my_pool(r: Repo, member_id: str):
        me = r.member(member_id)
        if not me or not me["pool_id"]:
            raise UserError("你还没有加入任何池")
        return me, r.pool(me["pool_id"])

    @staticmethod
    def _exec_state(r: Repo, pool_id: str) -> ExecState:
        plan = {p["member_id"]: p for p in r.plan(pool_id)}
        moves = {m: (p["from_grp"], p["to_grp"]) for m, p in plan.items() if p["from_grp"] != p["to_grp"]}
        in_team = {m for m, p in plan.items() if p["in_team"]}
        start = Counter(p["from_grp"] for p in plan.values())  # 方案生成时各组人数（池是封闭的）
        final = Counter(p["to_grp"] for p in plan.values())
        free = {g for g in start if max(start[g], final[g]) < config.CAPACITY}  # 执行全程都有空位的组
        return ExecState(
            swaps=pairing.build_swaps(moves, in_team, free),
            cur={m["id"]: m["grp"] for m in r.pool_members(pool_id)},
            done={m: p["moves_done"] for m, p in plan.items()},
            in_team=in_team,
            plan=plan,
        )

    @staticmethod
    def _all_present(members, groups) -> bool:
        """池里至少有 MIN_GROUPS 个组，并且每个组的全部成员都已加入。"""
        joined = Counter(m["grp"] for m in members)
        return len(groups) >= MIN_GROUPS and all(joined[g["grp"]] >= g["lm_count"] for g in groups)

    @staticmethod
    def _blockers(members, groups, all_teams) -> list[str]:
        blockers = []
        if len(groups) < MIN_GROUPS:
            blockers.append(f"至少需要 {MIN_GROUPS} 个组")
        for g in groups:
            joined = sum(m["grp"] == g["grp"] for m in members)
            if joined < g["lm_count"]:
                blockers.append(f"{g['grp']} 组还差 {g['lm_count'] - joined} 人（{joined}/{g['lm_count']}）")
        names = {m["id"]: m["nickname"] for m in members}
        for t in teams.oversized(all_teams):
            blockers.append("小队超过 5 人：" + "、".join(names[i] for i in t) + "（需取消部分互选）")
        return blockers
