"""退出补位：评审人临时退出后的只读修复预览与单事务采用（乐观并发控制）。

* **预览（preview）**：管理员选择一名临时退出的评审人，求解内核在修复模式下
  重排——退出者的评审人节点、容量、机构名额与其全部既有配对（含锁定）一并移除，
  其余锁定保留；在冲突、容量、主审资格与机构多样性硬约束不变的前提下，按
  **“最大化满足人数 → 最少改动既有非锁定配对 → 最低原求解成本”** 的字典序选优，
  允许必要的连锁换位。预览为纯内存计算，**不写入**任何库表。
* **版本指纹（fingerprint）**：绑定稿件、评审人、冲突、锁定、求解参数以及
  **当前非锁定分配**（换位数的比较基线）与退出者 ID；其中任一基线变化，
  预览即过期。
* **采用（adopt）**：在单个事务（``BEGIN IMMEDIATE``）内先复核指纹，一致才
  重算并写入：删除退出者的全部配对（含锁定）、以预览分配替换其余非锁定分配
  （他人锁定保留）并更新 last_solution；指纹不一致返回 409，绝不写入。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from .db import get_setting, set_setting
from .sandbox import PARAM_KEYS, _metrics_of
from .solver import compute_solution


class BackfillError(Exception):
    """业务异常：status_code 为应返回的 HTTP 状态码。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ------------------------------------------------------------- 版本指纹
def version_fingerprint(conn: sqlite3.Connection, reviewer_id: int) -> str:
    """稿件、评审人、冲突、锁定、求解参数、当前非锁定分配与退出者 ID 的版本指纹。

    与方案沙盒不同，退出补位的比较基线是“当前非锁定配对集”（换位数的参照），
    因此非锁定分配也参与指纹；重新求解会重写该集合，预览随之过期（但在无并发
    修改时重新预览即可）。
    """
    manuscripts = [
        {"id": r["id"], "title": r["title"],
         "topics": json.loads(r["topics"]),
         "required_reviews": r["required_reviews"],
         "required_leads": r["required_leads"]}
        for r in conn.execute("SELECT * FROM manuscripts ORDER BY id")
    ]
    reviewers = [
        {"id": r["id"], "name": r["name"],
         "topics": json.loads(r["topics"]),
         "capacity": r["capacity"],
         "institution": r["institution"] or "",
         "can_lead": bool(r["can_lead"])}
        for r in conn.execute("SELECT * FROM reviewers ORDER BY id")
    ]
    conflicts = [
        [r["m_id"], r["r_id"]]
        for r in conn.execute("SELECT m_id, r_id FROM conflicts ORDER BY m_id, r_id")
    ]
    locks = [
        [r["m_id"], r["r_id"]]
        for r in conn.execute(
            "SELECT m_id, r_id FROM assignments WHERE locked = 1 ORDER BY m_id, r_id")
    ]
    free_pairs = [
        [r["m_id"], r["r_id"]]
        for r in conn.execute(
            "SELECT m_id, r_id FROM assignments WHERE locked = 0 ORDER BY m_id, r_id")
    ]
    params = {key: get_setting(conn, key) for key in PARAM_KEYS}
    payload = {
        "kind": "exit-backfill",
        "reviewer_id": reviewer_id,
        "manuscripts": manuscripts,
        "reviewers": reviewers,
        "conflicts": conflicts,
        "locks": locks,
        "free_pairs": free_pairs,
        "params": params,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- 预览
def _load_names(conn: sqlite3.Connection) -> tuple[dict, dict]:
    m_titles = {r["id"]: r["title"]
                for r in conn.execute("SELECT id, title FROM manuscripts")}
    r_names = {r["id"]: r["name"]
               for r in conn.execute("SELECT id, name FROM reviewers")}
    return m_titles, r_names


def preview(conn: sqlite3.Connection, reviewer_id: int) -> dict:
    """退出补位只读预览：排除该评审人后在修复模式下重排，不写任何库表。"""
    reviewer = conn.execute(
        "SELECT id, name FROM reviewers WHERE id = ?", (reviewer_id,)).fetchone()
    if reviewer is None:
        raise BackfillError(404, f"评审人 #{reviewer_id} 不存在。")

    beta = int(get_setting(conn, "mismatch_weight"))
    alpha = int(get_setting(conn, "load_weight"))
    org_cap = int(get_setting(conn, "institution_cap"))

    current_rows = [
        (r["m_id"], r["r_id"], bool(r["locked"]))
        for r in conn.execute("SELECT m_id, r_id, locked FROM assignments")
    ]
    current_pairs = {(m_id, r_id) for m_id, r_id, _ in current_rows}
    current_locked = {(m_id, r_id) for m_id, r_id, locked in current_rows if locked}
    current_free = {(m_id, r_id) for m_id, r_id, locked in current_rows if not locked}
    exiting_pairs = {(m_id, r_id) for m_id, r_id in current_pairs if r_id == reviewer_id}
    exiting_locked = exiting_pairs & current_locked

    fingerprint = version_fingerprint(conn, reviewer_id)
    solution, new_pairs = compute_solution(
        conn, beta, alpha, org_cap,
        exclude_reviewer_id=reviewer_id, retain_pairs=current_free)

    # 预览生效后的完整分配 = 他人锁定（保留）+ 修复重排的自由分配；
    # 退出者的配对（含其锁定）全部移除。
    kept_locks = current_locked - exiting_locked
    preview_pairs = kept_locks | set(new_pairs)
    added = sorted(preview_pairs - current_pairs)
    removed = sorted(current_pairs - preview_pairs)

    m_titles, r_names = _load_names(conn)
    lead_set = {tuple(p) for p in solution["lead_pairs"]}
    current_solution = get_setting(conn, "last_solution")
    current_lead_set = {
        tuple(p) for p in (current_solution or {}).get("lead_pairs", [])}

    def pair_info(pair: tuple[int, int]) -> dict:
        m_id, r_id = pair
        return {
            "manuscript_id": m_id,
            "reviewer_id": r_id,
            "manuscript_title": m_titles.get(m_id, f"#{m_id}"),
            "reviewer_name": r_names.get(r_id, f"#{r_id}"),
            # is_lead：在修复结果中是否主审；was_lead：在当前方案中是否主审
            "is_lead": (m_id, r_id) in lead_set,
            "was_lead": (m_id, r_id) in current_lead_set,
            "was_locked": (m_id, r_id) in current_locked,
        }

    metrics = _metrics_of(solution)
    current_metrics = _metrics_of(current_solution) if current_solution else None
    delta = None
    if current_metrics is not None:
        delta = {
            key: metrics[key] - current_metrics[key]
            for key in ("total_cost", "total_assigned", "unmet_people", "lead_deficit")
        }

    # ----------------------------- 逐稿缺口变化（含连锁换位波及到的稿件）
    cur_per_m: dict[int, int] = {}
    cur_lead_per_m: dict[int, int] = {}
    cur_lead_pairs = {tuple(p) for p in (current_solution or {}).get("lead_pairs", [])}
    for m_id, r_id in current_pairs:
        cur_per_m[m_id] = cur_per_m.get(m_id, 0) + 1
        if (m_id, r_id) in cur_lead_pairs:
            cur_lead_per_m[m_id] = cur_lead_per_m.get(m_id, 0) + 1

    new_per_m: dict[int, int] = {}
    new_lead_per_m: dict[int, int] = {}
    for m_id, r_id in preview_pairs:
        new_per_m[m_id] = new_per_m.get(m_id, 0) + 1
        if (m_id, r_id) in lead_set:
            new_lead_per_m[m_id] = new_lead_per_m.get(m_id, 0) + 1

    req_by_m = {r["id"]: r["required_reviews"]
                for r in conn.execute("SELECT id, required_reviews FROM manuscripts")}
    lead_req_by_m = {r["id"]: r["required_leads"]
                     for r in conn.execute("SELECT id, required_leads FROM manuscripts")}
    affected_ms = sorted(
        {m_id for m_id, _ in added} | {m_id for m_id, _ in removed})
    gap_changes = []
    for m_id in affected_ms:
        req = req_by_m.get(m_id, 0)
        lead_req = lead_req_by_m.get(m_id, 0)
        before_n, after_n = cur_per_m.get(m_id, 0), new_per_m.get(m_id, 0)
        before_l, after_l = cur_lead_per_m.get(m_id, 0), new_lead_per_m.get(m_id, 0)
        gap_changes.append({
            "manuscript_id": m_id,
            "manuscript_title": m_titles.get(m_id, f"#{m_id}"),
            "required": req,
            "lead_required": lead_req,
            "assigned_before": before_n,
            "assigned_after": after_n,
            "deficit_before": max(0, req - before_n),
            "deficit_after": max(0, req - after_n),
            "lead_before": before_l,
            "lead_after": after_l,
            "lead_deficit_before": max(0, lead_req - before_l),
            "lead_deficit_after": max(0, lead_req - after_l),
        })

    # ----------------------------- 连锁换位：替补评审人为补位而离开原稿件
    added_by_r: dict[int, set[int]] = {}
    for m_id, r_id in added:
        added_by_r.setdefault(r_id, set()).add(m_id)
    removed_by_r: dict[int, set[int]] = {}
    for m_id, r_id in removed:
        removed_by_r.setdefault(r_id, set()).add(m_id)
    chains = []
    for r_id, added_ms in sorted(added_by_r.items()):
        vacated_ms = removed_by_r.get(r_id, set())
        if vacated_ms:
            chains.append({
                "reviewer_id": r_id,
                "reviewer_name": r_names.get(r_id, f"#{r_id}"),
                "vacates": [pair_info((m_id, r_id)) for m_id in sorted(vacated_ms)],
                "fills": [pair_info((m_id, r_id)) for m_id in sorted(added_ms)],
            })

    # 采用前须知（非求解告警，不写入 solution.warnings）
    notices = []
    if exiting_locked:
        notices.append(
            f"{reviewer['name']} 有 {len(exiting_locked)} 个锁定配对将随退出一并移除"
            f"（锁定不能跨人保留）；其余评审人的锁定全部保留。")
    if exiting_pairs:
        exiting_free = exiting_pairs - exiting_locked
        if exiting_free:
            notices.append(
                f"{reviewer['name']} 当前承担 {len(exiting_pairs)} 个配对"
                f"（其中非锁定 {len(exiting_free)} 个），采用后全部移除。")
    else:
        notices.append(f"{reviewer['name']} 当前没有任何分配，本次修复不会改动配对。")
    if metrics["unmet_people"] > 0:
        notices.append(
            f"受冲突、容量、主审资格与机构上限限制，排除后仍有 "
            f"{metrics['unmet_people']} 个评审人数缺口无法补齐。")

    repair = solution.get("repair") or {}
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "kind": "exit-backfill",
        "fingerprint": fingerprint,
        "reviewer_id": reviewer_id,
        "reviewer_name": reviewer["name"],
        "params": {key: get_setting(conn, key) for key in PARAM_KEYS},
        "exiting": {
            "pair_count": len(exiting_pairs),
            "locked_pair_count": len(exiting_locked),
            "pairs": [pair_info(p) for p in sorted(exiting_pairs)],
        },
        "metrics": metrics,
        "current_metrics": current_metrics,
        "delta": delta,
        "diff": {
            "added": [pair_info(p) for p in added],
            "removed": [pair_info(p) for p in removed],
        },
        "retained_pair_count": repair.get("kept_pair_count", 0),
        "new_pair_count": repair.get("new_pair_count", 0),
        "chain_moves": chains,
        "gap_changes": gap_changes,
        "unmet": [
            {key: u[key] for key in (
                "manuscript_id", "title", "required", "assigned", "deficit",
                "lead_required", "lead_assigned", "lead_deficit")}
            for u in solution["unmet"]
        ],
        "warnings": solution["warnings"],
        "notices": notices,
    }


# ----------------------------------------------------------------- 采用
def adopt(conn: sqlite3.Connection, reviewer_id: int, fingerprint: str) -> dict:
    """采用退出补位方案：指纹仍一致时，在单个事务内写入修复后的分配。

    求解是确定性的：指纹一致 ⇒ 基线（稿件/评审人/冲突/锁定/设置/当前非锁定
    分配/退出者）未变 ⇒ 重算结果与预览完全相同。指纹不一致返回 409，不写入。
    """
    # BEGIN IMMEDIATE：立即获取写锁，把“指纹复核 + 重算 + 写入”串行化，
    # 防止校验与写入之间被并发修改插入（过期预览覆盖新数据）。
    conn.execute("BEGIN IMMEDIATE")
    try:
        reviewer = conn.execute(
            "SELECT id, name FROM reviewers WHERE id = ?", (reviewer_id,)).fetchone()
        if reviewer is None:
            conn.execute("ROLLBACK")
            raise BackfillError(404, f"评审人 #{reviewer_id} 不存在。")
        if version_fingerprint(conn, reviewer_id) != fingerprint:
            conn.execute("ROLLBACK")
            raise BackfillError(
                409,
                "生成修复预览后，稿件、评审人、冲突、锁定、求解参数或当前分配已发生变化，"
                "该预览已过期；请重新生成预览后再采用，本次未写入任何内容。")

        beta = int(get_setting(conn, "mismatch_weight"))
        alpha = int(get_setting(conn, "load_weight"))
        org_cap = int(get_setting(conn, "institution_cap"))
        current_free = {
            (r["m_id"], r["r_id"])
            for r in conn.execute(
                "SELECT m_id, r_id FROM assignments WHERE locked = 0")
        }
        solution, pairs = compute_solution(
            conn, beta, alpha, org_cap,
            exclude_reviewer_id=reviewer_id, retain_pairs=current_free)

        # 先删退出者的全部配对（含锁定），再以修复分配替换其余非锁定分配；
        # 他人锁定一律保留。求解参数（设置）不变。
        conn.execute("DELETE FROM assignments WHERE r_id = ?", (reviewer_id,))
        conn.execute("DELETE FROM assignments WHERE locked = 0")
        for m_id, r_id in pairs:
            conn.execute(
                "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 0)",
                (m_id, r_id),
            )
        set_setting(conn, "last_solution", solution)
        conn.execute("COMMIT")
    except BackfillError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return solution
