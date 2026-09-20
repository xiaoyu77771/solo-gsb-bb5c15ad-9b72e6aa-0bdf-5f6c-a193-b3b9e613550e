"""评审分配求解器：最小费用最大流（两阶段：先主审、后普通评审）。

网络模型
========
节点：源点 S -> 稿件 M_i -> [机构节点 O_{i,a} ->] 评审人 R_j -> 汇点 T

* S -> M_i：容量 = 该稿本轮尚缺的人数（需求扣除已锁定与上一阶段分配）
* M_i -> R_j / M_i -> O_{i,a} -> R_j：容量 1；冲突边、与既有分配重复的边一律不建。
  当开启“每篇稿件同机构最多人数 gamma”（gamma>0）且评审人填有机构时，
  分配边必须经过机构节点，M_i -> O_{i,a} 的容量 = gamma - 该稿该机构的已占用数，
  从而把机构多样性做成硬约束；机构名额被占满（或历史锁定已超限）时，
  该机构节点不再有入边 -> 本稿不会再分配该机构的评审人。
  未填写机构的评审人不受此限，直接 M_i -> R_j。
* R_j -> T：把容量拆成若干条单位“负载槽位”并行边，
  第 k 个槽位费用 = alpha * k（已占用 k = 锁定数 + 1 起算），
  从而把“评审负载偏差”做成凸的边际成本，流会天然趋向均衡
* M_i -> R_j / O_{i,a} -> R_j 边费用 = beta * 该评审未覆盖的稿件主题数

主审资格硬约束（两阶段求解）
============================
稿件可设“需主审人数” L_i（0~2 且不超过所需评审数，0 即关闭该稿的主审约束），
只有标记“可任主审”的评审人才有资格担任主审（资格为硬约束）：

1. 合资格的有效锁定按评审人 ID 升序优先抵扣主审名额；
2. 第一阶段：仅对合资格候选建网，把剩余主审名额作为需求求最大流——
   主审与普通人选走同一套稿件需求、个人容量槽位与机构名额，
   同一人对同一稿件至多分配一次；
3. 第二阶段：把第一阶段的主审分配视同锁定（预占需求/容量/机构名额），
   对其余候选建网求普通分配。第一阶段的流是全局网络中的可行流，
   在其残量网络上继续增广必能达到全局最大流，因此“主审优先”
   不会降低最终的总分配人数（最大可行分配）；
4. 主审仍不足时不阻塞求解：该稿列入未满足，分别给出主审缺口与总人数缺口。

锁定的既有分配不进入网络，直接预占需求、容量与机构名额并计入负载和费用。
冲突边根本不会被加入图中，因此绝不可能被使用。

退出补位（修复模式）
====================
``compute_solution`` 支持两个仅用于“退出补位”的可选参数：

* ``exclude_reviewer_id``：临时退出的评审人——其评审人节点、容量、机构名额与
  既有锁定全部从本轮问题中移除（锁定随退出一并失效，区别于“无效锁定”告警）；
* ``retain_pairs``：当前非锁定（自由）配对集合。属于该集合的分配边加 0 “换位
  费用”，否则加一个严格大于任意可行解原费用极差的常数 ``churn_weight``。

由于 MCMF 求的是“最大流中的最小费用”，边费用 = 换位费 + 原费用（β 主题费 +
α 负载费）后，最优解严格按以下字典序比较：

1. **最大化满足人数**（最大流不变，仍是第一优先）；
2. **最少改动既有非锁定配对**（一条新配对的换位费 > 任何原费用总差，故人数相同
   时保留尽可能多的既有配对，允许必要的连锁换位）；
3. **最低原求解成本**（换位数相同的方案之间，再按原 MCMF 费用取优）。

报告的 ``total_cost`` 始终只计原费用（与常规求解口径一致），换位费仅用于选优。

缺口解困（单步临时调整试算）
============================
``compute_solution`` 另外支持三个仅用于“缺口解困建议”的只读临时调整参数，
三者都是对输入数据的内存覆盖，**绝不写回任何库表**：

* ``capacity_override``：``{评审人 ID: 临时容量}``，模拟“某评审人容量临时 +1”；
* ``org_cap_override``：临时机构上限（整数），模拟“当前同机构上限放宽 1”；
* ``extra_lead_reviewers``：评审人 ID 集合，模拟“为某评审人临时补充主审资格”。

这些覆盖只放宽约束（扩大可行域），故重算结果的总人数与主审人数不可能变少，
解困建议据此枚举所有单步调整并只保留严格减少缺口者。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .db import get_setting, set_setting, transaction
from .mcmf import MinCostMaxFlow


def _rows(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    return conn.execute(sql).fetchall()


def institution_lock_exceeds(cap: int, same_inst_locked: int, is_new_lock: bool) -> bool:
    """纯函数：新增锁定是否会使同机构人数超过每稿上限。

    cap=0 表示关闭机构多样性约束；仅“新增一行锁定”（此前不存在该分配）才占新名额，
    把已有自由分配转为锁定不增加人数。
    """
    if cap <= 0 or not is_new_lock:
        return False
    return same_inst_locked + 1 > cap


def compute_solution(conn: sqlite3.Connection, beta: int, alpha: int, org_cap: int,
                     exclude_reviewer_id: int | None = None,
                     retain_pairs: set[tuple[int, int]] | None = None,
                     capacity_override: dict[int, int] | None = None,
                     org_cap_override: int | None = None,
                     extra_lead_reviewers: set[int] | None = None
                     ) -> tuple[dict, list[tuple[int, int]]]:
    """纯计算：用给定参数求解并返回 (solution, 新自由分配对)，**不写任何库表**。

    方案沙盒的预览与本模块的正式求解共用此函数，保证两者口径完全一致；
    由于求解是确定性的，采用预览时在指纹校验通过后重算即得到与预览相同的分配。

    退出补位（修复模式）：
    * ``exclude_reviewer_id`` 非空时，该评审人视为临时退出：不建节点、其锁定
      与既有配对全部移除（不产生无效锁定告警）；
    * ``retain_pairs`` 非空时进入换位最小化：属于该集合的分配边加 0 换位费，
      其余边加 churn_weight（严格大于原费用极差），实现“先最大化满足人数、
      再最少改动既有非锁定配对、最后最低原费用”的字典序最优。

    缺口解困（单步临时调整，全部为只读内存覆盖，绝不写回）：
    * ``capacity_override``：临时替换评审人个人容量（仅放宽试算用）；
    * ``org_cap_override``：临时替换每篇同机构最多人数；
    * ``extra_lead_reviewers``：临时授予主审资格的评审人 ID 集合。
    """
    capacity_override = capacity_override or {}
    if org_cap_override is not None:
        org_cap = org_cap_override
    extra_lead_set = set(extra_lead_reviewers or ())

    manuscripts = [dict(r) for r in _rows(
        conn, "SELECT * FROM manuscripts ORDER BY id")]
    all_reviewers = [dict(r) for r in _rows(
        conn, "SELECT * FROM reviewers ORDER BY id")]
    # 缺口解困：容量临时调整在内存中覆盖（绝不写回 reviewers 表）
    for r in all_reviewers:
        if r["id"] in capacity_override:
            r["capacity"] = capacity_override[r["id"]]
    # 退出补位：退出者的节点/容量/机构名额整体退出本轮问题
    reviewers = [r for r in all_reviewers if r["id"] != exclude_reviewer_id]

    conflict_pairs: set[tuple[int, int]] = {
        (r["m_id"], r["r_id"])
        for r in _rows(conn, "SELECT m_id, r_id FROM conflicts")
    }
    lock_rows = _rows(conn, "SELECT m_id, r_id FROM assignments WHERE locked = 1 ORDER BY m_id, r_id")
    if exclude_reviewer_id is not None:
        # 退出者的锁定随退出一并移除（用户显式操作，不按“无效锁定”告警）
        lock_rows = [r for r in lock_rows if r["r_id"] != exclude_reviewer_id]

    m_map = {m["id"]: m for m in manuscripts}
    r_map = {r["id"]: r for r in reviewers}
    m_by_id = {m["id"]: {**m, "topics": json.loads(m["topics"])} for m in manuscripts}
    r_by_id = {r["id"]: {**r, "topics": json.loads(r["topics"])} for r in reviewers}

    warnings: list[dict] = []

    # ---------------------------------------------------------------- 锁定校验
    # 有效锁：稿件/评审人均存在、无冲突、且未超过该稿所需评审数。
    # 无效锁保留在表中（由用户解锁），但不参与本次求解。
    locks_by_m: dict[int, list[int]] = {}
    for row in lock_rows:
        m_id, r_id = row["m_id"], row["r_id"]
        if m_id not in m_map or r_id not in r_map:
            warnings.append({
                "code": "lock_missing_entity",
                "message": f"锁定分配（稿件 #{m_id} / 评审人 #{r_id}）指向已删除对象，已忽略。",
                "manuscript_id": m_id, "reviewer_id": r_id,
            })
            continue
        if (m_id, r_id) in conflict_pairs:
            warnings.append({
                "code": "lock_conflict",
                "message": f"《{m_map[m_id]['title']}》与 {r_map[r_id]['name']} 存在作者冲突，该锁定被忽略，请先解除冲突或解锁。",
                "manuscript_id": m_id, "reviewer_id": r_id,
            })
            continue
        locks_by_m.setdefault(m_id, []).append(r_id)

    valid_locks: list[tuple[int, int]] = []
    for m_id, r_ids in locks_by_m.items():
        req = m_map[m_id]["required_reviews"]
        if len(r_ids) > req:
            for extra in r_ids[req:]:
                warnings.append({
                    "code": "lock_exceeds_required",
                    "message": f"《{m_map[m_id]['title']}》锁定了 {len(r_ids)} 人，超过每稿 {req} 人的需求，"
                               f"超出的锁定（{r_map[extra]['name']}）本次不计入。",
                    "manuscript_id": m_id, "reviewer_id": extra,
                })
        valid_locks.extend((m_id, r_id) for r_id in r_ids[:req])

    locked_pairs: set[tuple[int, int]] = set(valid_locks)
    locked_count: dict[int, int] = {}
    for _, r_id in valid_locks:
        locked_count[r_id] = locked_count.get(r_id, 0) + 1

    # 锁定数超过个人容量：强制保留（既有分配），剩余容量为 0，并给出警告。
    free_capacity: dict[int, int] = {}
    for r in reviewers:
        cap, lc = r["capacity"], locked_count.get(r["id"], 0)
        if lc > cap:
            warnings.append({
                "code": "reviewer_over_capacity",
                "message": f"{r['name']} 已有 {lc} 个锁定分配，超过个人容量 {cap}；"
                           f"锁定分配强制保留，本次不再向其安排新稿。",
                "reviewer_id": r["id"],
            })
            free_capacity[r["id"]] = 0
        else:
            free_capacity[r["id"]] = cap - lc

    # 机构名额：锁定分配先占用每篇稿件的同机构名额。
    # 历史锁定已超过机构上限时：保留锁定、发出警告、本次不再向该稿分配该机构的人。
    # 未填写机构（空串）的评审人不参与机构统计。
    inst_by_r: dict[int, str] = {r["id"]: (r["institution"] or "").strip() for r in reviewers}
    locked_inst_count: dict[tuple[int, str], int] = {}
    for m_id, r_id in valid_locks:
        inst = inst_by_r.get(r_id, "")
        if inst:
            locked_inst_count[(m_id, inst)] = locked_inst_count.get((m_id, inst), 0) + 1

    inst_over: set[tuple[int, str]] = set()  # 历史锁定已超限/占满机构名额的（稿件, 机构）
    if org_cap > 0:
        for (m_id, inst), cnt in sorted(locked_inst_count.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            if cnt > org_cap:
                inst_over.add((m_id, inst))
                over_names = [
                    r_map[rid]["name"]
                    for mm, rid in valid_locks
                    if mm == m_id and inst_by_r.get(rid, "") == inst
                ]
                warnings.append({
                    "code": "institution_lock_over_cap",
                    "message": f"《{m_map[m_id]['title']}》来自「{inst}」的锁定评审已有 {cnt} 人，"
                               f"超过每篇同机构最多 {org_cap} 人的限制"
                               f"（{('、'.join(over_names))}）；锁定分配强制保留，本次不再安排该机构的其他评审人。",
                    "manuscript_id": m_id, "institution": inst,
                    "locked_count": cnt, "cap": org_cap,
                })

    # ------------------------------------------------- 锁定部分的费用（确定性）
    def uncovered(m_id: int, r_id: int) -> int:
        return len(set(m_by_id[m_id]["topics"]) - set(r_by_id[r_id]["topics"]))

    edge_costs: dict[str, dict] = {}
    locked_cost = 0
    lock_slot_seen: dict[int, int] = {}
    for m_id, r_id in valid_locks:
        k = lock_slot_seen.get(r_id, 0) + 1
        lock_slot_seen[r_id] = k
        mm = beta * uncovered(m_id, r_id)
        ll = alpha * k
        locked_cost += mm + ll
        edge_costs[f"{m_id}:{r_id}"] = {
            "mismatch_cost": mm, "load_cost": ll,
            "covered": sorted(set(m_by_id[m_id]["topics"]) & set(r_by_id[r_id]["topics"])),
            "uncovered_topics": sorted(set(m_by_id[m_id]["topics"]) - set(r_by_id[r_id]["topics"])),
        }

    locked_by_m_count: dict[int, int] = {}
    for m_id, _ in valid_locks:
        locked_by_m_count[m_id] = locked_by_m_count.get(m_id, 0) + 1

    # ------------------------------------------------- 主审资格与锁定抵扣
    # 主审资格为硬约束：只有 can_lead 的评审人可任主审。
    # 缺口解困：extra_lead_reviewers 中的评审人在本轮试算中临时获得主审资格（只读）。
    # 合资格的有效锁定按评审人 ID 升序优先抵扣主审名额（locks_by_m 的 r_ids 已按 ID 升序）。
    can_lead: dict[int, bool] = {
        r["id"]: bool(r["can_lead"]) or r["id"] in extra_lead_set
        for r in reviewers
    }
    lead_required: dict[int, int] = {
        m["id"]: max(0, min(m["required_leads"], m["required_reviews"]))
        for m in manuscripts
    }
    lead_locked_pairs: set[tuple[int, int]] = set()
    lead_locked_by_m: dict[int, int] = {}
    for m_id, r_ids in locks_by_m.items():
        req = m_map[m_id]["required_reviews"]
        need = lead_required.get(m_id, 0)
        n = 0
        for r_id in r_ids[:req]:  # 与 valid_locks 口径一致的有效锁定
            if n >= need:
                break
            if can_lead.get(r_id):
                lead_locked_pairs.add((m_id, r_id))
                n += 1
        if n:
            lead_locked_by_m[m_id] = n

    # ------------------------------------------------- 退出补位：换位惩罚
    # 字典序选优（仅在给出 retain_pairs 时启用）：保留既有非锁定配对的分配边
    # 换位费为 0，其余边为 churn_weight。churn_weight 严格大于任意两个可行解
    # 的原费用总差，保证“人数 → 换位数 → 原费用”的严格字典序（详见模块文档）。
    repair_mode = retain_pairs is not None
    retain_set: set[tuple[int, int]] = set(retain_pairs or ())
    if exclude_reviewer_id is not None:
        retain_set = {(m_id, r_id) for m_id, r_id in retain_set
                      if r_id != exclude_reviewer_id}
    if repair_mode:
        t_max = max((len(m_by_id[m["id"]]["topics"]) for m in manuscripts), default=0)
        c_max = max((r["capacity"] for r in reviewers), default=0)
        # 严格大于任意可行解原费用极差：换 1 对的代价 > 全部配对原费用之差
        churn_weight = (
            sum(m["required_reviews"] for m in manuscripts)
            * (beta * t_max + alpha * c_max) + 1)
    else:
        churn_weight = 0

    # ------------------------------------------------- 单轮建网与求解
    def run_round(pairs: list[tuple[int, int]],
                  need_by_m: dict[int, int],
                  slot_base: dict[int, int],
                  slot_cap: dict[int, int],
                  inst_pre_used: dict[tuple[int, str], int],
                  ) -> tuple[list[tuple[int, int]], int, int, dict[int, list[int]]]:
        """建一轮流网络并求解。

        pairs：本轮可建分配边的 (稿件, 评审人)；need_by_m：各稿本轮需求；
        slot_base/slot_cap：各评审人已占槽位数 / 本轮可用槽位数（负载费用连续计）；
        inst_pre_used：机构名额的既有占用（预算 = org_cap - 占用，<=0 不建机构节点）。
        返回 (本轮分配对, 本轮原费用, 本轮换位费, 各评审人使用的槽位序号列表)。
        """
        pairs = [(m_id, r_id) for m_id, r_id in pairs if need_by_m.get(m_id, 0) > 0]
        m_ids = sorted(need_by_m)
        r_ids = sorted({r_id for _, r_id in pairs})
        S = 0
        m_node = {m_id: 1 + i for i, m_id in enumerate(m_ids)}
        r_base = 1 + len(m_ids)
        r_node = {r_id: r_base + i for i, r_id in enumerate(r_ids)}

        # 机构节点 O_{m,inst}：仅当开启机构上限、评审人填有机构且预算未耗尽时建立
        # （历史锁定已超限的在 inst_over 中，预算 <= 0，自然不建）。
        org_keys: set[tuple[int, str]] = set()
        if org_cap > 0:
            for m_id, r_id in pairs:
                inst = inst_by_r[r_id]
                if inst and org_cap - inst_pre_used.get((m_id, inst), 0) > 0:
                    org_keys.add((m_id, inst))
        org_list = sorted(org_keys)
        org_base = r_base + len(r_ids)
        org_node = {key: org_base + i for i, key in enumerate(org_list)}
        T = org_base + len(org_list)
        net = MinCostMaxFlow(T + 1)

        for m_id in m_ids:
            need = need_by_m[m_id]
            if need > 0:
                net.add_edge(S, m_node[m_id], need, 0,
                             {"type": "source", "m_id": m_id, "cap": need})

        # M_i -> O_{m,inst}：容量 = 机构上限 - 该稿该机构的既有占用。
        for (m_id, inst), node in org_node.items():
            cap = org_cap - inst_pre_used.get((m_id, inst), 0)
            net.add_edge(m_node[m_id], node, cap, 0,
                         {"type": "institution", "m_id": m_id, "institution": inst, "cap": cap})

        for m_id, r_id in pairs:
            inst = inst_by_r[r_id]
            # 原费用 = β × 未覆盖主题数；修复模式下新配对再加 churn_weight 换位费
            cost = beta * uncovered(m_id, r_id)
            if repair_mode and (m_id, r_id) not in retain_set:
                cost += churn_weight
            assign_tag = {"type": "assign", "m_id": m_id, "r_id": r_id, "cap": 1}
            if org_cap > 0 and inst:
                node = org_node.get((m_id, inst))
                if node is None:
                    continue  # 机构预算耗尽：不再分配该机构任何人
                net.add_edge(node, r_node[r_id], 1, cost, assign_tag)
            else:
                # 机构上限关闭、或评审人未填写机构（不占用机构名额）：直接建边。
                net.add_edge(m_node[m_id], r_node[r_id], 1, cost, assign_tag)

        for r_id in r_ids:
            base = slot_base.get(r_id, 0)
            for k in range(1, slot_cap.get(r_id, 0) + 1):
                slot = base + k  # 全局负载槽位序号，与既有占用连续计费
                net.add_edge(r_node[r_id], T, 1, alpha * slot,
                             {"type": "slot", "r_id": r_id, "slot": slot, "cap": 1})

        _, round_cost = net.solve(S, T)

        used_pairs = sorted(
            (tag["m_id"], tag["r_id"])
            for tag, used in net.used_tags()
            if tag["type"] == "assign" and used > 0
        )
        slots_by_r: dict[int, list[int]] = {}
        for tag, used in net.used_tags():
            if tag["type"] == "slot" and used > 0:
                slots_by_r.setdefault(tag["r_id"], []).append(tag["slot"])
        if repair_mode:
            # total 含换位费；按选中的配对重算原费用，换位费 = 新配对数 × 权重
            round_churn = sum(1 for p in used_pairs if p not in retain_set) * churn_weight
            # 槽位费用按各评审人已用槽位顺序对应（与 note_edge_costs 同口径）
            ms_by_reviewer: dict[int, list[int]] = {}
            for m_id, r_id in used_pairs:
                ms_by_reviewer.setdefault(r_id, []).append(m_id)
            round_base = 0
            for r_id, ms in ms_by_reviewer.items():
                slots = sorted(slots_by_r.get(r_id, []))
                for idx, m_id in enumerate(sorted(ms)):
                    slot = slots[idx] if idx < len(slots) else slot_base.get(r_id, 0) + idx + 1
                    round_base += beta * uncovered(m_id, r_id) + alpha * slot
        else:
            round_base = round_cost
            round_churn = 0
        return used_pairs, round_base, round_churn, slots_by_r

    def note_edge_costs(pairs: list[tuple[int, int]],
                        slots_by_r: dict[int, list[int]],
                        slot_base: dict[int, int]) -> None:
        """把一轮分配的费用拆解登记到 edge_costs（槽位按评审人分组后顺序对应）。"""
        ms_by_reviewer: dict[int, list[int]] = {}
        for m_id, r_id in pairs:
            ms_by_reviewer.setdefault(r_id, []).append(m_id)
        for r_id, ms in ms_by_reviewer.items():
            slots = sorted(slots_by_r.get(r_id, []))
            for idx, m_id in enumerate(sorted(ms)):
                slot = slots[idx] if idx < len(slots) else (slot_base.get(r_id, 0) + idx + 1)
                mm = beta * uncovered(m_id, r_id)
                ll = alpha * slot
                edge_costs[f"{m_id}:{r_id}"] = {
                    "mismatch_cost": mm, "load_cost": ll,
                    "covered": sorted(set(m_by_id[m_id]["topics"]) & set(r_by_id[r_id]["topics"])),
                    "uncovered_topics": sorted(set(m_by_id[m_id]["topics"]) - set(r_by_id[r_id]["topics"])),
                }

    # 可建“分配边”的（稿件, 评审人）：冲突边绝不加入；锁定边已预占，也不重复建立。
    base_eligible: list[tuple[int, int]] = []
    for m in manuscripts:
        for r in reviewers:
            m_id, r_id = m["id"], r["id"]
            if (m_id, r_id) in conflict_pairs or (m_id, r_id) in locked_pairs:
                continue
            base_eligible.append((m_id, r_id))

    # ------------------------------------- 第一阶段：自由主审（仅合资格候选）
    # 主审占用稿件需求、个人容量与机构名额；不足部分留给第二阶段用普通分配补齐人数。
    lead_need: dict[int, int] = {}
    for m in manuscripts:
        m_id = m["id"]
        remaining = max(0, m["required_reviews"] - locked_by_m_count.get(m_id, 0))
        need = min(lead_required.get(m_id, 0) - lead_locked_by_m.get(m_id, 0), remaining)
        if need > 0:
            lead_need[m_id] = need

    lead_pairs: list[tuple[int, int]] = []
    lead_cost = 0
    lead_churn = 0
    lead_slot_count: dict[int, int] = {}
    if lead_need:
        lead_candidates = [
            (m_id, r_id) for m_id, r_id in base_eligible
            if m_id in lead_need and can_lead.get(r_id)
        ]
        lead_pairs, lead_cost, lead_churn, lead_slots = run_round(
            lead_candidates, lead_need, locked_count, free_capacity, locked_inst_count)
        note_edge_costs(lead_pairs, lead_slots, locked_count)
        for r_id, slots in lead_slots.items():
            lead_slot_count[r_id] = len(slots)

    lead_pair_set: set[tuple[int, int]] = set(lead_pairs)
    lead_assigned_by_m: dict[int, int] = {}
    lead_inst_used: dict[tuple[int, str], int] = {}
    for m_id, r_id in lead_pairs:
        lead_assigned_by_m[m_id] = lead_assigned_by_m.get(m_id, 0) + 1
        inst = inst_by_r.get(r_id, "")
        if inst:
            lead_inst_used[(m_id, inst)] = lead_inst_used.get((m_id, inst), 0) + 1

    # ------------------------------------- 第二阶段：普通分配（主审已预占）
    remaining_need: dict[int, int] = {}
    for m in manuscripts:
        m_id = m["id"]
        need = max(0, m["required_reviews"]
                   - locked_by_m_count.get(m_id, 0)
                   - lead_assigned_by_m.get(m_id, 0))
        if need > 0:
            remaining_need[m_id] = need

    remaining_capacity = {
        r["id"]: max(0, free_capacity[r["id"]] - lead_slot_count.get(r["id"], 0))
        for r in reviewers
    }
    slot_base2 = {r["id"]: locked_count.get(r["id"], 0) + lead_slot_count.get(r["id"], 0)
                  for r in reviewers}
    inst_pre_used2 = dict(locked_inst_count)
    for key, n in lead_inst_used.items():
        inst_pre_used2[key] = inst_pre_used2.get(key, 0) + n

    free_candidates = [p for p in base_eligible if p not in lead_pair_set]
    free_pairs, free_cost, free_churn, free_slots = run_round(
        free_candidates, remaining_need, slot_base2, remaining_capacity, inst_pre_used2)
    note_edge_costs(free_pairs, free_slots, slot_base2)

    # --------------------------------------------------------- 未满足稿件分析
    assigned_by_m: dict[int, int] = {}
    for m_id, _ in valid_locks:
        assigned_by_m[m_id] = assigned_by_m.get(m_id, 0) + 1
    for m_id, _ in lead_pairs + free_pairs:
        assigned_by_m[m_id] = assigned_by_m.get(m_id, 0) + 1

    # 实际主审 = 合资格锁定抵扣（按评审人 ID 升序）+ 第一阶段自由主审补齐。
    actual_lead_pairs: set[tuple[int, int]] = lead_locked_pairs | lead_pair_set
    lead_got_by_m: dict[int, int] = {}
    for m_id, _ in actual_lead_pairs:
        lead_got_by_m[m_id] = lead_got_by_m.get(m_id, 0) + 1

    # 本稿已占用（锁定或新分配）的评审不能重复计入候选（每稿-每人最多一次）。
    assigned_pairs: set[tuple[int, int]] = locked_pairs | lead_pair_set | set(free_pairs)

    # 每篇稿件最终的机构占用（锁定 + 新分配；未填机构归入空串，不参与上限统计）。
    inst_used: dict[int, dict[str, int]] = {}
    for m_id, r_id in valid_locks + lead_pairs + free_pairs:
        inst = inst_by_r.get(r_id, "")
        if inst:
            inst_used.setdefault(m_id, {})[inst] = inst_used.setdefault(m_id, {}).get(inst, 0) + 1

    total_capacity = sum(r["capacity"] for r in reviewers)
    total_required = sum(m["required_reviews"] for m in manuscripts)

    unmet: list[dict] = []
    for m in manuscripts:
        m_id = m["id"]
        got = assigned_by_m.get(m_id, 0)
        deficit = m["required_reviews"] - got
        lead_req = lead_required.get(m_id, 0)
        lead_got = lead_got_by_m.get(m_id, 0)
        lead_deficit = max(0, lead_req - lead_got)
        if deficit <= 0 and lead_deficit <= 0:
            continue

        reasons: list[str] = []
        candidates = []
        blocked_by_conflict = 0
        blocked_by_institution = 0
        assigned_here = 0
        candidate_capacity = 0
        effective_capacity = 0
        inst_blocked_names: dict[str, int] = {}
        # 主审候选池统计（合资格评审人的阻断原因分解）
        lead_pool = 0
        lead_blocked_conflict = 0
        lead_assigned_here = 0
        lead_no_capacity = 0
        lead_blocked_institution = 0
        lead_available = 0
        for r in reviewers:
            r_id = r["id"]
            qualified = can_lead.get(r_id, False)
            if qualified:
                lead_pool += 1
            if (m_id, r_id) in conflict_pairs:
                blocked_by_conflict += 1
                if qualified:
                    lead_blocked_conflict += 1
                continue
            if (m_id, r_id) in assigned_pairs:
                assigned_here += 1  # 已计入 got（锁定或本次新分配），不能重复评审本稿
                if qualified:
                    lead_assigned_here += 1
                continue
            rem = remaining_capacity[r_id]
            miss = uncovered(m_id, r_id)
            inst = inst_by_r.get(r_id, "")
            # 开启机构上限时：该稿该机构名额已满（含历史锁定超限）则该候选被硬性排除。
            inst_full = bool(
                org_cap > 0 and inst
                and ((m_id, inst) in inst_over or inst_used.get(m_id, {}).get(inst, 0) >= org_cap)
            )
            candidate_capacity += rem
            if inst_full:
                blocked_by_institution += 1
                inst_blocked_names[inst] = inst_blocked_names.get(inst, 0) + 1
            if rem > 0 and not inst_full:
                effective_capacity += rem
            if qualified:
                if inst_full:
                    lead_blocked_institution += 1
                elif rem <= 0:
                    lead_no_capacity += 1
                else:
                    lead_available += 1
            candidates.append({
                "reviewer_id": r_id, "name": r["name"],
                "institution": inst or None,
                "can_lead": qualified,
                "remaining_capacity": rem,
                "uncovered_topics": miss,
                "topic_match": "full" if miss == 0 else "partial",
                "blocked_by_institution": inst_full,
            })

        if lead_deficit > 0:
            parts = []
            if lead_blocked_conflict:
                parts.append(f"{lead_blocked_conflict} 名因作者冲突被禁止")
            if lead_assigned_here:
                parts.append(f"{lead_assigned_here} 名已评审本稿（不可重复）")
            if lead_blocked_institution:
                parts.append(f"{lead_blocked_institution} 名被机构上限排除")
            if lead_no_capacity:
                parts.append(f"{lead_no_capacity} 名剩余容量为 0")
            detail = "，".join(parts) if parts else ""
            reasons.append(
                f"主审缺口：本稿需主审 {lead_req} 名，实际认定 {lead_got} 名，缺 {lead_deficit} 名。"
                f"可任主审的评审人共 {lead_pool} 名"
                + (f"，其中{detail}，" if detail else "，")
                + f"当前可补充主审者 {lead_available} 名；"
                f"可在「评审人」中为更多评审人勾选“可任主审”、解除相关冲突或提高其容量。"
            )
        if blocked_by_conflict:
            reasons.append(f"{len(reviewers)} 名评审人中有 {blocked_by_conflict} 名因作者冲突被禁止评审本稿。")
        if blocked_by_institution:
            detail = "、".join(
                f"「{inst}」{n} 名" for inst, n in sorted(inst_blocked_names.items()))
            has_over_lock = any((m_id, inst) in inst_over for inst in inst_blocked_names)
            if has_over_lock:
                reasons.append(
                    f"另有 {blocked_by_institution} 名非冲突候选被机构上限排除（{detail}）："
                    f"该机构在本稿的锁定分配已超过每篇同机构最多 {org_cap} 人，锁定保留且不再安排同机构评审人。"
                )
            else:
                reasons.append(
                    f"另有 {blocked_by_institution} 名非冲突候选因机构多样性上限被排除（{detail}）："
                    f"本稿来自该机构的评审（含锁定）已达每篇同机构最多 {org_cap} 人。"
                )
        if deficit > 0:
            # 注意：主题不匹配只是高费用（软约束），不禁止分配；
            # 这里分层展示，便于人工复核未满足的真正原因。
            effective_candidates = [c for c in candidates
                                    if c["remaining_capacity"] > 0 and not c["blocked_by_institution"]]
            compatible_cap = sum(c["remaining_capacity"] for c in effective_candidates
                                 if c["topic_match"] == "full")
            compatible_n = sum(1 for c in effective_candidates if c["topic_match"] == "full")
            any_remaining = any(c["remaining_capacity"] > 0 for c in candidates)
            if not candidates:
                # 非冲突评审都已在本稿（锁定或本次新分配），无人可再补充
                reasons.append(
                    f"无冲突评审人共 {assigned_here} 名，均已评审本稿，无法重复安排；"
                    f"可在「作者冲突」中解除冲突，或在「评审人」中增加相容评审。"
                )
            elif not any_remaining:
                reasons.append("其余评审人剩余容量均为 0（个人容量已被锁定或其他稿件占满）。")
            elif effective_capacity < deficit:
                reasons.append(
                    f"扣除冲突与机构上限后，可用评审人的剩余总容量为 {effective_capacity}，小于本稿缺口 {deficit}；"
                    f"其中主题完全相容者 {compatible_n} 名、剩余容量合计 {compatible_cap}。"
                    f"可在「评审人」中提高容量、解除相关冲突，或在设置中放宽每篇同机构人数上限。"
                )
            else:
                reasons.append(
                    f"扣除冲突与机构上限后，可用评审人剩余总容量为 {effective_capacity}（不小于缺口 {deficit}），"
                    f"但这些容量与其他稿件共享（每稿-每人最多一次），在全局最小费用竞争下本稿仍缺 {deficit}；"
                    f"主题完全相容者 {compatible_n} 名、剩余容量合计 {compatible_cap}，"
                    f"可考虑增加方向匹配的评审人或提高其容量。"
                )
            if total_capacity < total_required:
                reasons.append(
                    f"全局性容量不足：全部评审人容量之和 {total_capacity} < 全部稿件需求之和 {total_required}。"
                )

        unmet.append({
            "manuscript_id": m_id,
            "title": m["title"],
            "required": m["required_reviews"],
            "assigned": got,
            "deficit": deficit,
            "lead_required": lead_req,
            "lead_assigned": lead_got,
            "lead_deficit": lead_deficit,
            "reasons": reasons,
            "details": {
                "reviewer_count": len(reviewers),
                "blocked_by_conflict": blocked_by_conflict,
                "blocked_by_institution": blocked_by_institution,
                "assigned_here": assigned_here,
                "candidate_remaining_capacity": candidate_capacity,
                "effective_remaining_capacity": effective_capacity,
                "lead_pool": lead_pool,
                "lead_available": lead_available,
                "candidates": candidates,
            },
        })

    # 每篇稿件的机构构成（用于分配结果展示；未填机构的评审人归入「未填写机构」）。
    composition: dict[int, list[dict]] = {}
    for m in manuscripts:
        m_id = m["id"]
        groups: dict[str, dict] = {}
        for mm, r_id in valid_locks:
            if mm != m_id:
                continue
            inst = inst_by_r.get(r_id, "") or ""
            g = groups.setdefault(inst, {"institution": inst or None, "count": 0,
                                        "locked": 0, "over_cap": False})
            g["count"] += 1
            g["locked"] += 1
        for mm, r_id in lead_pairs + free_pairs:
            if mm != m_id:
                continue
            inst = inst_by_r.get(r_id, "") or ""
            g = groups.setdefault(inst, {"institution": inst or None, "count": 0,
                                        "locked": 0, "over_cap": False})
            g["count"] += 1
        if org_cap > 0:
            for inst, g in groups.items():
                if inst and g["count"] > org_cap:
                    g["over_cap"] = True
        ordered = sorted(groups.values(),
                         key=lambda g: (g["institution"] is None, g["institution"] or ""))
        composition[m_id] = ordered

    # ------------------------------------------------- 退出补位统计（仅修复模式）
    repair_info = None
    if repair_mode:
        all_new_pairs = lead_pair_set | set(free_pairs)
        new_pair_set = {(m, r) for m, r in all_new_pairs if (m, r) not in retain_set}
        kept_pair_set = all_new_pairs & retain_set
        repair_info = {
            "excluded_reviewer_id": exclude_reviewer_id,
            # 换位：被替换的既有非锁定配对数；保留：本轮继续沿用的既有非锁定配对数
            "new_pair_count": len(new_pair_set),
            "kept_pair_count": len(kept_pair_set),
            "new_pairs": sorted([list(p) for p in new_pair_set]),
            "kept_pairs": sorted([list(p) for p in kept_pair_set]),
            # 仅用于验证字典序权重；total_cost 始终只计原费用
            "churn_cost": lead_churn + free_churn,
        }

    solution = {
        "solved_at": datetime.now().isoformat(timespec="seconds"),
        "mismatch_weight": beta,
        "load_weight": alpha,
        "institution_cap": org_cap,
        "total_required": total_required,
        "total_assigned": len(valid_locks) + len(lead_pairs) + len(free_pairs),
        "total_capacity": total_capacity,
        "locked_count": len(valid_locks),
        "total_lead_required": sum(lead_required.values()),
        "total_lead_assigned": len(actual_lead_pairs),
        # 实际主审（稿件, 评审人）对：结果页据此标识主审
        "lead_pairs": sorted([list(p) for p in actual_lead_pairs]),
        "total_cost": locked_cost + lead_cost + free_cost,
        "locked_cost": locked_cost,
        "free_cost": lead_cost + free_cost,
        "fully_satisfied": len(unmet) == 0,
        "unmet": unmet,
        "warnings": warnings,
        "edge_costs": edge_costs,
        "institutions": composition,
        # 退出补位：仅修复模式下非 None（常规求解与方案沙盒不输出）
        "repair": repair_info,
    }
    return solution, lead_pairs + free_pairs


def solve(conn: sqlite3.Connection) -> dict:
    """按当前设置求解：单事务内替换全部非锁定分配并记录 last_solution。"""
    beta = int(get_setting(conn, "mismatch_weight"))   # 主题不匹配权重
    alpha = int(get_setting(conn, "load_weight"))      # 负载偏差权重
    org_cap = int(get_setting(conn, "institution_cap"))  # 每稿同机构最多人数（0=关闭）
    solution, pairs = compute_solution(conn, beta, alpha, org_cap)
    with transaction(conn):
        conn.execute("DELETE FROM assignments WHERE locked = 0")
        for m_id, r_id in pairs:
            conn.execute(
                "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 0)",
                (m_id, r_id),
            )
        set_setting(conn, "last_solution", solution)
    return solution
