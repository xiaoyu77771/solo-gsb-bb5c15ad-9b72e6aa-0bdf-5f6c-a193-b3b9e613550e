"""方案沙盒：临时求解参数的预览与采用（乐观并发控制）。

* **预览（preview）**：用管理员输入的临时参数（主题不匹配权重、负载权重、
  机构上限）在内存中重新求解，**不写入** settings、assignments、锁定项或
  last_solution；返回与当前方案相比的新增/移除评审配对、总成本、未满足人数
  与主审缺口变化，并绑定当前数据版本的指纹。
* **版本指纹（version_fingerprint）**：对稿件、评审人、冲突、锁定分配与
  求解参数（settings 中的三项权重/上限）取规范化 JSON 的 SHA-256；
  任何并发修改都会使指纹变化。
* **采用（adopt）**：在单个事务（``BEGIN IMMEDIATE``）内先复核指纹，
  一致才写入临时参数与预览分配（求解是确定性的，指纹一致时重算结果与预览
  完全相同）并更新 last_solution；指纹不一致返回 409，提示重新预览，
  绝不用过期结果覆盖并发修改。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from .db import get_setting, set_setting
from .solver import compute_solution

# 参与指纹与沙盒的求解参数（settings 键）
PARAM_KEYS = ("mismatch_weight", "load_weight", "institution_cap")


class SandboxError(Exception):
    """业务异常：status_code 为应返回的 HTTP 状态码。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ------------------------------------------------------------- 版本指纹
def version_fingerprint(conn: sqlite3.Connection) -> str:
    """稿件、评审人、冲突、锁定分配与求解参数的版本指纹（SHA-256）。

    非锁定的自由分配是上述数据的确定性求解结果，不参与指纹；
    因此“用相同数据重新求解”不会使既有预览失效。
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
    params = {key: get_setting(conn, key) for key in PARAM_KEYS}
    payload = {
        "manuscripts": manuscripts,
        "reviewers": reviewers,
        "conflicts": conflicts,
        "locks": locks,
        "params": params,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- 预览
def _metrics_of(sol: dict) -> dict:
    """从求解结果中提取对比指标（对旧版 last_solution 缺字段宽容）。"""
    unmet = sol.get("unmet", [])
    return {
        "total_cost": sol.get("total_cost", 0),
        "total_assigned": sol.get("total_assigned", 0),
        "total_required": sol.get("total_required", 0),
        "unmet_people": sum(u.get("deficit", 0) for u in unmet),
        "unmet_manuscripts": len(unmet),
        "lead_deficit": sum(u.get("lead_deficit", 0) for u in unmet),
        "lead_assigned": sol.get("total_lead_assigned", 0),
        "lead_required": sol.get("total_lead_required", 0),
        "fully_satisfied": sol.get("fully_satisfied", False),
    }


def preview(conn: sqlite3.Connection, mismatch_weight: int, load_weight: int,
            institution_cap: int) -> dict:
    """用临时参数生成预览：只读计算，不写任何库表。"""
    fingerprint = version_fingerprint(conn)
    solution, new_pairs = compute_solution(
        conn, mismatch_weight, load_weight, institution_cap)

    # 预览生效后的完整分配 = 现有锁定行（全部保留）+ 新自由分配
    current_rows = conn.execute("SELECT m_id, r_id, locked FROM assignments").fetchall()
    current_pairs = {(r["m_id"], r["r_id"]) for r in current_rows}
    locked_pairs = {(r["m_id"], r["r_id"]) for r in current_rows if r["locked"]}
    preview_pairs = locked_pairs | set(new_pairs)
    added = sorted(preview_pairs - current_pairs)
    removed = sorted(current_pairs - preview_pairs)

    m_titles = {r["id"]: r["title"]
                for r in conn.execute("SELECT id, title FROM manuscripts")}
    r_names = {r["id"]: r["name"]
               for r in conn.execute("SELECT id, name FROM reviewers")}
    lead_set = {tuple(p) for p in solution["lead_pairs"]}

    def pair_info(pair: tuple[int, int]) -> dict:
        m_id, r_id = pair
        return {
            "manuscript_id": m_id,
            "reviewer_id": r_id,
            "manuscript_title": m_titles.get(m_id, f"#{m_id}"),
            "reviewer_name": r_names.get(r_id, f"#{r_id}"),
            "is_lead": (m_id, r_id) in lead_set,
        }

    metrics = _metrics_of(solution)
    current_solution = get_setting(conn, "last_solution")
    current_metrics = _metrics_of(current_solution) if current_solution else None
    delta = None
    if current_metrics is not None:
        delta = {
            key: metrics[key] - current_metrics[key]
            for key in ("total_cost", "total_assigned", "unmet_people", "lead_deficit")
        }

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "fingerprint": fingerprint,
        "params": {
            "mismatch_weight": mismatch_weight,
            "load_weight": load_weight,
            "institution_cap": institution_cap,
        },
        "current_params": {key: get_setting(conn, key) for key in PARAM_KEYS},
        "metrics": metrics,
        "current_metrics": current_metrics,
        "delta": delta,
        "diff": {
            "added": [pair_info(p) for p in added],
            "removed": [pair_info(p) for p in removed],
        },
        "unmet": [
            {key: u[key] for key in (
                "manuscript_id", "title", "required", "assigned", "deficit",
                "lead_required", "lead_assigned", "lead_deficit")}
            for u in solution["unmet"]
        ],
        "warnings": solution["warnings"],
    }


# ----------------------------------------------------------------- 采用
def adopt(conn: sqlite3.Connection, mismatch_weight: int, load_weight: int,
          institution_cap: int, fingerprint: str) -> dict:
    """采用沙盒方案：指纹仍一致时，在单个事务内写入临时参数与预览分配。

    求解是确定性的：指纹一致 ⇒ 数据与参数基线未变 ⇒ 重算结果与预览完全相同。
    指纹不一致（预览期间发生并发修改）返回 409，不写入任何内容。
    """
    # BEGIN IMMEDIATE：立即获取写锁，把“指纹复核 + 重算 + 写入”串行化，
    # 防止校验与写入之间被并发修改插入（过期预览覆盖新数据）。
    conn.execute("BEGIN IMMEDIATE")
    try:
        if version_fingerprint(conn) != fingerprint:
            conn.execute("ROLLBACK")
            raise SandboxError(
                409,
                "生成预览后，稿件、评审人、冲突、锁定或求解参数已发生变化，"
                "该预览已过期；请重新生成预览后再采用，本次未写入任何内容。")

        solution, pairs = compute_solution(
            conn, mismatch_weight, load_weight, institution_cap)
        set_setting(conn, "mismatch_weight", mismatch_weight)
        set_setting(conn, "load_weight", load_weight)
        set_setting(conn, "institution_cap", institution_cap)
        conn.execute("DELETE FROM assignments WHERE locked = 0")
        for m_id, r_id in pairs:
            conn.execute(
                "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 0)",
                (m_id, r_id),
            )
        set_setting(conn, "last_solution", solution)
        conn.execute("COMMIT")
    except SandboxError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return solution
