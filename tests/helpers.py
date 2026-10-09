"""测试用的池：成员代号是"组号 + 序号"（如 N1、B2），不使用任何人名。"""
from app.services import Service

# 有空位：N 组 5 人，B、C 组各 4 人
VACANCY = {"groups": {"N": 5, "B": 4, "C": 4},
           "teams": [["N1", "N2", "N3", "B1", "C1"], ["B2", "B3", "C2"]]}
# 全满：E、F、G 组各 5 人
FULL = {"groups": {"E": 5, "F": 5, "G": 5},
        "teams": [["E1", "E2", "F1", "G1"], ["F2", "F3", "E3"], ["G2", "G3", "F4"]]}


def build_pool(svc: Service, spec: dict) -> dict[str, str]:
    """登记成员、建池、全员加入并设置互选，返回 代号 → 成员 id。"""
    ids = {}
    for grp, n in spec["groups"].items():
        for i in range(1, n + 1):
            ids[f"{grp}{i}"] = svc.current(svc.register(f"{grp}{i}", grp))["id"]
    first, *rest = ids
    code = svc.create_pool(ids[first], spec["groups"][first[0]])
    for name in rest:
        svc.join_pool(ids[name], code, spec["groups"][name[0]])
    for team in spec["teams"]:
        for a in team:
            for b in team:
                if a != b:
                    svc.set_wish(ids[a], ids[b], True)
    return ids


def start_execution(svc: Service, ids: dict[str, str]) -> None:
    """生成方案并全员同意。"""
    svc.make_plan(next(iter(ids.values())))
    for mid in ids.values():
        svc.approve(mid)
