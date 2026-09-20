"""缺口解困建议：枚举“单步临时调整”并只读重算，给出可减少缺口的建议。

当前分配存在人数缺口或主审缺口时，管理员可调用本模块的 ``suggest``：
系统枚举以下三类**单步**调整，每类每个对象各试算一次（每个建议恰好做一件事）：

1. **某评审人容量临时 +1**（逐人枚举）；
2. **当前同机构上限临时放宽 1**（仅在机构上限开启时；放宽指 γ → γ+1，
   关闭状态 γ=0 没有明确的“放宽 1”语义，故不枚举）；
3. **某评审人临时补充主审资格**（仅枚举当前不具备主审资格者）。

每次试算都复用求解内核 ``compute_solution`` 的只读临时覆盖参数
（``capacity_override`` / ``org_cap_override`` / ``extra_lead_reviewers``），
**绝不写入** settings、reviewers、assignments、锁定或 last_solution。

筛选与排序
==========
* 仅保留**严格减少缺口**的调整：总缺口人数（未满足人数）下降或主审缺口下降，
  且二者都不得上升（临时调整只放宽约束，理论上不会上升，仍做显式校验）；
* 排序（依次）：**总缺口减少量降序 → 主审缺口减少量降序 →
  改动配对数升序 → 成本增量升序**，再以调整类型与对象 ID 做确定性平局打破；
* 每条建议展示受影响稿件及新旧配对（旧配对 / 保留配对 / 新配对）、
  逐稿缺口对照、成本增量与求解警告。
* 当前无缺口，或没有任何单步调整能改善缺口时，返回空建议清单并给出明确说明。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .db import get_setting
from .sandbox import PARAM_KEYS, _metrics_of
from .solver import compute_solution


# 调整类型代码（API 稳定字段）
TYPE_CAPACITY = "capacity_plus_one"
TYPE_INSTITUTION = "institution_cap_plus_one"
TYPE_LEAD_QUALIFY = "grant_lead_qualification"


# ------------------------------------------------------------- 工具函数
def _load_names(conn: sqlite3.Connection) -> tuple[dict, dict]:
    m_titles = {r["id"]: r["title"]
                for r in conn.execute("SELECT id, title FROM manuscripts")}
    r_names = {r["id"]: r["name"]
               for r in conn.execute("SELECT id, name FROM reviewers")}
    return m_titles, r_names


def _per_manuscript(pairs: set[tuple[int, int]], lead_set: set[tuple[int, int]]
                    ) -> tuple[dict[int, int], dict[int, int]]:
    """返回 (每稿人数, 每稿主审数)。"""
    per_m: dict[int, int] = {}
    lead_per_m: dict[int, int] = {}
    for m_id, r_id in pairs:
        per_m[m_id] = per_m.get(m_id, 0) + 1
        if (m_id, r_id) in lead_set:
            lead_per_m[m_id] = lead_per_m.get(m_id, 0) + 1
    return per_m, lead_per_m


def _suggestion_rank(s: dict) -> tuple:
    """排序键：总缺口减少 ↓、主审缺口减少 ↓、改动配对数 ↑、成本增量 ↑、
    类型顺序、对象 ID（全部确定性）。"""
    return (
        -s["deficit_reduction"],
        -s["lead_deficit_reduction"],
        s["changed_pair_count"],
        s["cost_delta"],
        s["adjustment"]["type"],
        s["adjustment"].get("reviewer_id", 0),
    )


# ----------------------------------------------------------------- 建议枚举
def suggest(conn: sqlite3.Connection) -> dict:
    """枚举全部单步临时调整并只读重算，返回能改善缺口的建议清单。"""
    beta = int(get_setting(conn, "mismatch_weight"))
    alpha = int(get_setting(conn, "load_weight"))
    org_cap = int(get_setting(conn, "institution_cap"))

    params = {key: get_setting(conn, key) for key in PARAM_KEYS}

    # 基线：用当前数据/参数只读重算一次。所有“减少量 / 改动配对 / 成本增量”
    # 都与该基线对比（而非可能已过期的 assignments 表内容），保证口径一致。
    base_sol, base_free = compute_solution(conn, beta, alpha, org_cap)
    base_metrics = _metrics_of(base_sol)
    locked_pairs = {
        (row["m_id"], row["r_id"])
        for row in conn.execute("SELECT m_id, r_id FROM assignments WHERE locked = 1")
    }
    base_pairs = locked_pairs | set(base_free)
    base_lead = {tuple(p) for p in base_sol["lead_pairs"]}
    base_per_m, base_lead_per_m = _per_manuscript(base_pairs, base_lead)

    reviewers = [dict(r) for r in conn.execute(
        "SELECT id, name, capacity, institution, can_lead "
        "FROM reviewers ORDER BY id")]
    manuscripts = [dict(r) for r in conn.execute(
        "SELECT id, title, required_reviews, required_leads FROM manuscripts ORDER BY id")]
    req_by_m = {m["id"]: m["required_reviews"] for m in manuscripts}
    lead_req_by_m = {m["id"]: max(0, min(m["required_leads"], m["required_reviews"]))
                     for m in manuscripts}
    m_titles, r_names = _load_names(conn)

    no_gap = (base_metrics["unmet_people"] == 0
              and base_metrics["lead_deficit"] == 0)
    base_unmet_brief = [
        {key: u[key] for key in (
            "manuscript_id", "title", "required", "assigned", "deficit",
            "lead_required", "lead_assigned", "lead_deficit")}
        for u in base_sol["unmet"]
    ]

    def empty_payload(message: str, candidate_count: int = 0) -> dict:
        return {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "kind": "gap-relief-suggestions",
            "has_gap": not no_gap,
            "params": params,
            "baseline_metrics": base_metrics,
            "candidate_count": candidate_count,
            "suggestions": [],
            "message": message,
            "baseline_unmet": base_unmet_brief,
        }

    if no_gap:
        return empty_payload(
            "当前分配不存在人数缺口或主审缺口，无需缺口解困建议。")

    # ------------------------------------------------- 枚举单步调整
    candidates: list[dict] = []
    for r in reviewers:
        candidates.append({
            "type": TYPE_CAPACITY,
            "reviewer_id": r["id"],
            "reviewer_name": r["name"],
            "capacity_from": r["capacity"],
            "capacity_to": r["capacity"] + 1,
            "capacity_override": {r["id"]: r["capacity"] + 1},
            "org_cap_override": None,
            "extra_lead_reviewers": set(),
        })
    if org_cap > 0:
        candidates.append({
            "type": TYPE_INSTITUTION,
            "reviewer_id": None,
            "reviewer_name": None,
            "org_cap_from": org_cap,
            "org_cap_to": org_cap + 1,
            "capacity_override": {},
            "org_cap_override": org_cap + 1,
            "extra_lead_reviewers": set(),
        })
    for r in reviewers:
        if not r["can_lead"]:
            candidates.append({
                "type": TYPE_LEAD_QUALIFY,
                "reviewer_id": r["id"],
                "reviewer_name": r["name"],
                "capacity_override": {},
                "org_cap_override": None,
                "extra_lead_reviewers": {r["id"]},
            })

    def pair_info(pair: tuple[int, int], lead_set: set[tuple[int, int]]) -> dict:
        m_id, r_id = pair
        return {
            "manuscript_id": m_id,
            "reviewer_id": r_id,
            "manuscript_title": m_titles.get(m_id, f"#{m_id}"),
            "reviewer_name": r_names.get(r_id, f"#{r_id}"),
            "is_lead": (m_id, r_id) in lead_set,
            "was_lead": (m_id, r_id) in base_lead,
            "is_locked": (m_id, r_id) in locked_pairs,
        }

    suggestions: list[dict] = []
    for cand in candidates:
        sol, free_pairs = compute_solution(
            conn, beta, alpha, org_cap,
            capacity_override=cand["capacity_override"],
            org_cap_override=cand["org_cap_override"],
            extra_lead_reviewers=cand["extra_lead_reviewers"])
        metrics = _metrics_of(sol)

        deficit_cut = base_metrics["unmet_people"] - metrics["unmet_people"]
        lead_cut = base_metrics["lead_deficit"] - metrics["lead_deficit"]
        # 只保留严格改善者；只放宽约束，两个缺口都不应上升，仍显式校验。
        worse = (metrics["unmet_people"] > base_metrics["unmet_people"]
                 or metrics["lead_deficit"] > base_metrics["lead_deficit"])
        if worse or (deficit_cut <= 0 and lead_cut <= 0):
            continue

        trial_pairs = locked_pairs | set(free_pairs)
        trial_lead = {tuple(p) for p in sol["lead_pairs"]}
        added = trial_pairs - base_pairs
        removed = base_pairs - trial_pairs
        changed_n = len(added | removed)

        trial_per_m, trial_lead_per_m = _per_manuscript(trial_pairs, trial_lead)
        # 受影响稿件：配对集合变化，或主审构成变化（如既有配对被“补充资格”认定为主审，
        # 配对不变但主审缺口消除）——后者同样要在新旧配对中体现。
        affected_ids = sorted(
            {m_id for m_id, _ in added} | {m_id for m_id, _ in removed}
            | {m_id for m_id, n in trial_lead_per_m.items()
               if base_lead_per_m.get(m_id, 0) != n}
            | {m_id for m_id, n in base_lead_per_m.items()
               if trial_lead_per_m.get(m_id, 0) != n})
        affected = []
        for m_id in affected_ids:
            kept = sorted(p for p in (base_pairs & trial_pairs) if p[0] == m_id)
            old_p = sorted(p for p in removed if p[0] == m_id)
            new_p = sorted(p for p in added if p[0] == m_id)
            lead_before = base_lead_per_m.get(m_id, 0)
            lead_after = trial_lead_per_m.get(m_id, 0)
            affected.append({
                "manuscript_id": m_id,
                "manuscript_title": m_titles.get(m_id, f"#{m_id}"),
                "required": req_by_m.get(m_id, 0),
                "lead_required": lead_req_by_m.get(m_id, 0),
                "assigned_before": base_per_m.get(m_id, 0),
                "assigned_after": trial_per_m.get(m_id, 0),
                "deficit_before": max(0, req_by_m.get(m_id, 0) - base_per_m.get(m_id, 0)),
                "deficit_after": max(0, req_by_m.get(m_id, 0) - trial_per_m.get(m_id, 0)),
                "lead_before": lead_before,
                "lead_after": lead_after,
                "lead_deficit_before": max(0, lead_req_by_m.get(m_id, 0) - lead_before),
                "lead_deficit_after": max(0, lead_req_by_m.get(m_id, 0) - lead_after),
                "old_pairs": [pair_info(p, trial_lead) for p in old_p],
                "kept_pairs": [pair_info(p, trial_lead) for p in kept],
                "new_pairs": [pair_info(p, trial_lead) for p in new_p],
            })

        if cand["type"] == TYPE_CAPACITY:
            description = (
                f"将评审人「{cand['reviewer_name']}」的个人容量临时 +1"
                f"（{cand['capacity_from']} → {cand['capacity_to']}）后重算")
        elif cand["type"] == TYPE_INSTITUTION:
            description = (
                f"将每篇稿件同机构最多人数临时放宽 1"
                f"（{cand['org_cap_from']} → {cand['org_cap_to']}）后重算")
        else:
            description = (
                f"为评审人「{cand['reviewer_name']}」临时补充主审资格后重算")

        suggestions.append({
            "adjustment": {
                "type": cand["type"],
                "description": description,
                "reviewer_id": cand["reviewer_id"],
                "reviewer_name": cand["reviewer_name"],
                "capacity_from": cand.get("capacity_from"),
                "capacity_to": cand.get("capacity_to"),
                "institution_cap_from": cand.get("org_cap_from"),
                "institution_cap_to": cand.get("org_cap_to"),
            },
            "deficit_reduction": deficit_cut,
            "lead_deficit_reduction": lead_cut,
            "changed_pair_count": changed_n,
            "cost_delta": metrics["total_cost"] - base_metrics["total_cost"],
            "fully_resolves": (metrics["unmet_people"] == 0
                               and metrics["lead_deficit"] == 0),
            "metrics": metrics,
            "delta": {
                key: metrics[key] - base_metrics[key]
                for key in ("total_cost", "total_assigned", "unmet_people", "lead_deficit")
            },
            "affected_manuscripts": affected,
            "diff": {
                "added": [pair_info(p, trial_lead) for p in sorted(added)],
                "removed": [pair_info(p, trial_lead) for p in sorted(removed)],
            },
            "warnings": sol["warnings"],
        })

    suggestions.sort(key=_suggestion_rank)

    if not suggestions:
        return empty_payload(
            "当前分配存在缺口，但枚举全部“容量临时 +1 / 同机构上限放宽 1 / "
            "临时补充主审资格”的单步调整后，没有任何一项能减少总缺口或主审缺口；"
            "可能需要组合多项调整、增加相容评审人或解除冲突。",
            candidate_count=len(candidates))

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "kind": "gap-relief-suggestions",
        "has_gap": True,
        "params": params,
        "baseline_metrics": base_metrics,
        "candidate_count": len(candidates),
        "suggestions": suggestions,
        "message": (
            f"共枚举 {len(candidates)} 个单步调整，其中 {len(suggestions)} 个可减少缺口；"
            "全部试算均为只读，未改动设置、评审人、分配、锁定或上次求解结果。"
            "排序：总缺口减少量 → 主审缺口减少量 → 改动配对数 → 成本增量。"),
        "baseline_unmet": base_unmet_brief,
    }
