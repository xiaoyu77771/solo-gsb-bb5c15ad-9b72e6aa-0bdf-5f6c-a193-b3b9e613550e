"""分配批次审计：不可变批次快照、批次差异与按批次原子回滚。

批次快照
========
每次成功求解或锁定变更后，管理员可手动创建一个**不可变批次快照**，记录：

* **稿件—评审人配对**（当前 assignments 全量）；
* **主审标记**（来自最近一次求解结果 ``last_solution.lead_pairs``，
  锁定/解锁后也会立即重解，故快照时刻主审标记与配对一致）；
* **锁定状态**（每条配对的 locked 标记）；
* **求解参数**（主题不匹配权重 β、负载权重 α、每篇同机构最多人数 γ）；
* **备注**（管理员自由文本）。

快照为只增记录（``assignment_batches`` 表，数据库触发器禁止
UPDATE/DELETE），回滚成功后同样**写入一条新批次**（kind='rollback'），
绝不改写任何历史批次。

批次差异
========
``GET /api/batches/{a_id}/diff/{b_id}`` 在服务端对两个批次做配对级差异，
区分：新增配对（b 有 a 无）、移除配对（a 有 b 无）、锁定状态变化、
主审标记变化；并给出求解参数差异。页面可任意选择两个批次按时间对照。

按批次原子回滚
==============
``POST /api/batches/{id}/rollback`` 在单个事务（``BEGIN IMMEDIATE``）内：

1. **整单校验**（任何一项不满足都 409 拒绝、绝不部分写入，
   ``reasons`` 逐条说明原因）：
   * 快照中的稿件/评审人若已被删除，整条配对无法恢复 → 拒绝
     （“仅恢复当时仍存在的稿件、评审人”）；
   * 任何不在快照锁定集合中的当前锁（批次之后新增的锁，或快照配对中
     当时未锁、之后才锁上的锁），回滚都会隐式解锁 → 当前锁定冲突，
     要求管理员先自行解锁后再回滚；
   * 批次中锁定的配对若与当前冲突矩阵重合（批次之后补登了冲突）→
     当前锁定冲突，拒绝；
   * 批次锁定恢复后会使某评审人锁定数超过其当前个人容量、某稿锁定数
     超过其当前所需评审数，或使某稿同机构锁定数超过**批次参数中的
     机构上限**（如批次之后评审人机构被改动）→ 当前锁定冲突，拒绝。
2. 校验通过后在同一事务内：恢复求解参数 → 仅预置快照锁定 → 按恢复后的
   参数重解（确定性，锁定为快照锁定，故自由配对与快照一致）并更新
   last_solution → **写入新批次**
   （kind='rollback'、rolled_back_from=源批次 ID、备注自动注明）。
3. 批次中非锁定配对若与当前冲突矩阵重合，不会进入重解结果
   （冲突边绝不建），这些配对在新批次备注/返回的 ``skipped_conflict_pairs``
   中明确列出（不阻断回滚——它们本就不是锁，且属于“恢复后按当前
   冲突约束求解”的自然结果）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from .db import get_setting, set_setting, transaction
from .solver import compute_solution

# 参与快照与回滚的求解参数（settings 键），与方案沙盒口径一致
PARAM_KEYS = ("mismatch_weight", "load_weight", "institution_cap")

KIND_SNAPSHOT = "snapshot"
KIND_ROLLBACK = "rollback"


class AuditError(Exception):
    """业务异常：status_code 为应返回的 HTTP 状态码。"""

    def __init__(self, status_code: int, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        # 整单拒绝的逐条原因（回滚校验），供前端原样展示
        self.reasons = reasons or []


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------- 快照读写
def _snapshot_state(conn: sqlite3.Connection, note: str, kind: str,
                    rolled_back_from: int | None) -> dict:
    """采集当前 assignments / 主审标记 / 求解参数的快照内容（不落库）。"""
    solution = get_setting(conn, "last_solution")
    lead_set = {tuple(p) for p in (solution or {}).get("lead_pairs", [])}
    rows = conn.execute(
        "SELECT m_id, r_id, locked FROM assignments ORDER BY locked DESC, m_id, r_id"
    ).fetchall()
    pairs = [
        {"manuscript_id": r["m_id"], "reviewer_id": r["r_id"],
         "locked": bool(r["locked"]),
         "is_lead": (r["m_id"], r["r_id"]) in lead_set}
        for r in rows
    ]
    params = {key: get_setting(conn, key) for key in PARAM_KEYS}
    content_hash = hashlib.sha256(
        json.dumps({"pairs": pairs, "params": params},
                   ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "version": 1,
        "pairs": pairs,
        "params": params,
        "note": note,
        "content_hash": content_hash,
    }


def _insert_batch(conn: sqlite3.Connection, snapshot: dict, kind: str,
                  rolled_back_from: int | None, note: str) -> int:
    """插入一条批次（调用方负责事务）。触发器保证此后不可改写/删除。"""
    cur = conn.execute(
        "INSERT INTO assignment_batches(created_at, kind, rolled_back_from, note, snapshot) "
        "VALUES (?, ?, ?, ?, ?)",
        (_now(), kind, rolled_back_from, note,
         json.dumps(snapshot, ensure_ascii=False)),
    )
    return int(cur.lastrowid)


def create_snapshot(conn: sqlite3.Connection, note: str) -> dict:
    """创建不可变批次快照。要求至少成功求解过一次（存在 last_solution）。"""
    note = (note or "").strip()
    if get_setting(conn, "last_solution") is None:
        raise AuditError(
            400, "尚未成功求解，当前没有可固化的分配方案；请先点击“重新求解分配”后再创建批次快照。")
    with transaction(conn):  # 单事务插入；审计表触发器保证不可变
        snapshot = _snapshot_state(conn, note, KIND_SNAPSHOT, None)
        batch_id = _insert_batch(conn, snapshot, KIND_SNAPSHOT, None, note)
    return get_batch(conn, batch_id)


# ------------------------------------------------------------- 查询
def _names(conn: sqlite3.Connection) -> tuple[dict[int, str], dict[int, str]]:
    m_titles = {r["id"]: r["title"]
                for r in conn.execute("SELECT id, title FROM manuscripts")}
    r_names = {r["id"]: r["name"]
               for r in conn.execute("SELECT id, name FROM reviewers")}
    return m_titles, r_names


def _summary(row: sqlite3.Row, snap: dict) -> dict:
    pairs = snap.get("pairs", [])
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "kind": row["kind"],
        "rolled_back_from": row["rolled_back_from"],
        "note": row["note"],
        "pair_count": len(pairs),
        "locked_count": sum(1 for p in pairs if p["locked"]),
        "lead_count": sum(1 for p in pairs if p.get("is_lead")),
        "params": snap.get("params", {}),
        "content_hash": snap.get("content_hash", ""),
    }


def list_batches(conn: sqlite3.Connection) -> list[dict]:
    """批次时间线（按时间倒序：最新在前）。"""
    rows = conn.execute(
        "SELECT * FROM assignment_batches ORDER BY id DESC").fetchall()
    return [_summary(r, json.loads(r["snapshot"])) for r in rows]


def _get_row(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM assignment_batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise AuditError(404, f"批次 #{batch_id} 不存在。")
    return row


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict:
    """批次详情：快照配对附带当前名称（对象已删除时标注 deleted=true）。"""
    row = _get_row(conn, batch_id)
    snap = json.loads(row["snapshot"])
    m_titles, r_names = _names(conn)
    pairs = []
    for p in snap.get("pairs", []):
        m_id, r_id = p["manuscript_id"], p["reviewer_id"]
        pairs.append({
            **p,
            "manuscript_title": m_titles.get(m_id),
            "reviewer_name": r_names.get(r_id),
            "manuscript_deleted": m_id not in m_titles,
            "reviewer_deleted": r_id not in r_names,
        })
    return {
        **_summary(row, snap),
        "pairs": pairs,
        "snapshot_version": snap.get("version", 1),
    }


def batch_diff(conn: sqlite3.Connection, from_id: int, to_id: int) -> dict:
    """两个批次的配对级差异（from → to），供页面按时间对照。"""
    a = get_batch(conn, from_id)
    b = get_batch(conn, to_id)

    def index(batch: dict) -> dict[tuple[int, int], dict]:
        return {(p["manuscript_id"], p["reviewer_id"]): p for p in batch["pairs"]}

    a_map, b_map = index(a), index(b)
    m_titles, r_names = _names(conn)

    def pair_info(m_id: int, r_id: int) -> dict:
        return {
            "manuscript_id": m_id, "reviewer_id": r_id,
            "manuscript_title": m_titles.get(m_id),
            "reviewer_name": r_names.get(r_id),
            "manuscript_deleted": m_id not in m_titles,
            "reviewer_deleted": r_id not in r_names,
        }

    added, removed = [], []
    lock_changed, lead_changed = [], []
    for key in sorted(a_map.keys() | b_map.keys()):
        pa, pb = a_map.get(key), b_map.get(key)
        m_id, r_id = key
        if pa is None:
            added.append({**pair_info(m_id, r_id),
                          "locked": pb["locked"], "is_lead": pb.get("is_lead", False)})
        elif pb is None:
            removed.append({**pair_info(m_id, r_id),
                            "locked": pa["locked"], "is_lead": pa.get("is_lead", False)})
        else:
            if bool(pa["locked"]) != bool(pb["locked"]):
                lock_changed.append({**pair_info(m_id, r_id),
                                     "locked_before": bool(pa["locked"]),
                                     "locked_after": bool(pb["locked"])})
            if bool(pa.get("is_lead")) != bool(pb.get("is_lead")):
                lead_changed.append({**pair_info(m_id, r_id),
                                     "lead_before": bool(pa.get("is_lead")),
                                     "lead_after": bool(pb.get("is_lead"))})

    param_changes = []
    for key in PARAM_KEYS:
        va = a["params"].get(key)
        vb = b["params"].get(key)
        if va != vb:
            param_changes.append({"param": key, "before": va, "after": vb})

    return {
        "created_at": _now(),
        "from_batch": {"id": a["id"], "created_at": a["created_at"],
                       "kind": a["kind"], "note": a["note"]},
        "to_batch": {"id": b["id"], "created_at": b["created_at"],
                     "kind": b["kind"], "note": b["note"]},
        "summary": {
            "pairs_before": len(a["pairs"]), "pairs_after": len(b["pairs"]),
            "added": len(added), "removed": len(removed),
            "lock_changed": len(lock_changed), "lead_changed": len(lead_changed),
        },
        "added": added, "removed": removed,
        "lock_changed": lock_changed, "lead_changed": lead_changed,
        "param_changes": param_changes,
    }


# ------------------------------------------------------------- 原子回滚
def rollback(conn: sqlite3.Connection, batch_id: int) -> dict:
    """按批次原子回滚。校验任一不满足则整单 409 拒绝；成功则写入新批次。"""
    target_row = _get_row(conn, batch_id)
    target = json.loads(target_row["snapshot"])
    pairs: list[dict] = target.get("pairs", [])
    target_params: dict = target.get("params", {})

    # 立即获取写锁，把“校验 + 恢复 + 重解 + 写新批次”串行化。
    conn.execute("BEGIN IMMEDIATE")
    try:
        m_rows = {r["id"]: dict(r)
                  for r in conn.execute("SELECT * FROM manuscripts")}
        r_rows = {r["id"]: dict(r)
                  for r in conn.execute("SELECT * FROM reviewers")}
        conflict_set = {
            (r["m_id"], r["r_id"])
            for r in conn.execute("SELECT m_id, r_id FROM conflicts")
        }
        current_locked = {
            (r["m_id"], r["r_id"])
            for r in conn.execute(
                "SELECT m_id, r_id FROM assignments WHERE locked = 1")
        }
        target_pairs = [(p["manuscript_id"], p["reviewer_id"], bool(p["locked"]))
                        for p in pairs]
        target_pair_set = {(m_id, r_id) for m_id, r_id, _ in target_pairs}
        target_locked = {(m_id, r_id) for m_id, r_id, locked in target_pairs if locked}

        def name_of(m_id: int, r_id: int) -> str:
            return f"《{m_rows[m_id]['title']}》—{r_rows[r_id]['name']}"

        reasons: list[str] = []

        # 1) 快照对象必须仍存在（“仅恢复当时仍存在的稿件、评审人”）。
        missing_ms = sorted({m_id for m_id, _, _ in target_pairs if m_id not in m_rows})
        missing_rv = sorted({r_id for _, r_id, _ in target_pairs if r_id not in r_rows})
        for m_id in missing_ms:
            reasons.append(f"批次中的稿件 #{m_id} 已被删除，其配对无法恢复。")
        for r_id in missing_rv:
            reasons.append(f"批次中的评审人 #{r_id} 已被删除，其配对无法恢复。")

        # 对象缺失时后续基于名称/容量的校验无意义，直接整单拒绝。
        if reasons:
            conn.execute("ROLLBACK")
            raise AuditError(409, "回滚被整单拒绝：批次引用的稿件或评审人已不存在，"
                                 "请确认后改用更新的批次。", reasons)

        # 2) 当前锁定冲突：任何不在快照锁定集合中的当前锁——无论是批次之后新增的
        #    配对，还是快照中存在但当时未锁定、之后才锁上的配对——回滚都会使其
        #    解锁（隐式丢锁）。一律拒绝，管理员须先在页面自行解锁后再回滚。
        extra_locks = sorted(current_locked - target_locked)
        for m_id, r_id in extra_locks:
            if (m_id, r_id) in target_pair_set:
                detail = "该配对在批次中存在但当时未锁定，回滚会解除其当前锁定"
            else:
                detail = "该配对不在批次中（批次之后新增的锁定），回滚会将其解锁"
            reasons.append(
                f"当前锁定 {name_of(m_id, r_id)} 不能被回滚隐式解除：{detail}；"
                f"请先在页面解锁后重试。")

        # 3) 批次锁定与当前冲突矩阵重合 → 锁定冲突，不能恢复。
        for m_id, r_id in sorted(target_locked):
            if (m_id, r_id) in conflict_set:
                reasons.append(
                    f"批次中的锁定配对 {name_of(m_id, r_id)} 与当前作者冲突重合"
                    f"（批次之后补登了冲突）；存在冲突的分配不能锁定，请先移除冲突或选择其他批次。")

        # 4) 恢复锁定后超过评审人当前个人容量。
        lock_count_by_r: dict[int, int] = {}
        for _, r_id in target_locked:
            lock_count_by_r[r_id] = lock_count_by_r.get(r_id, 0) + 1
        for r_id in sorted(lock_count_by_r):
            n = lock_count_by_r[r_id]
            cap = int(r_rows[r_id]["capacity"])
            if n > cap:
                reasons.append(
                    f"恢复批次锁定后，评审人 {r_rows[r_id]['name']} 将有 {n} 个锁定分配，"
                    f"超过其当前个人容量 {cap}；请先提高容量或选择其他批次。")

        # 5) 恢复锁定后超过稿件需求。
        lock_count_by_m: dict[int, int] = {}
        for m_id, _ in target_locked:
            lock_count_by_m[m_id] = lock_count_by_m.get(m_id, 0) + 1
        for m_id in sorted(lock_count_by_m):
            n = lock_count_by_m[m_id]
            req = int(m_rows[m_id]["required_reviews"])
            if n > req:
                reasons.append(
                    f"恢复批次锁定后，稿件《{m_rows[m_id]['title']}》将有 {n} 个锁定分配，"
                    f"超过其当前所需评审数 {req}；请先调整需求或选择其他批次。")

        # 6) 恢复锁定后同机构锁定数超过批次参数中的机构上限（按快照参数恢复）。
        org_cap = int(target_params.get("institution_cap") or 0)
        if org_cap > 0:
            inst_by_r = {r_id: (r["institution"] or "").strip()
                         for r_id, r in r_rows.items()}
            per_m_inst: dict[tuple[int, str], list[int]] = {}
            for m_id, r_id in target_locked:
                inst = inst_by_r.get(r_id, "")
                if inst:
                    per_m_inst.setdefault((m_id, inst), []).append(r_id)
            for (m_id, inst), r_ids in sorted(per_m_inst.items()):
                if len(r_ids) > org_cap:
                    names = "、".join(r_rows[r]["name"] for r in sorted(r_ids))
                    reasons.append(
                        f"恢复批次锁定后，稿件《{m_rows[m_id]['title']}》来自「{inst}」的"
                        f"锁定评审将有 {len(r_ids)} 人（{names}），超过批次参数中每篇同机构"
                        f"最多 {org_cap} 人的限制；请先放宽上限或选择其他批次。")

        if reasons:
            conn.execute("ROLLBACK")
            raise AuditError(
                409, "回滚被整单拒绝：存在已删除对象或当前锁定冲突，未写入任何内容；"
                     "请按下列原因处理后重试。", reasons)

        # ------------------------------------------------ 校验通过，执行恢复
        # 求解参数恢复为批次参数
        for key in PARAM_KEYS:
            set_setting(conn, key, int(target_params[key]))

        # 以快照锁定预置 assignments（自由配对不预置），再按恢复后的参数重解：
        # 锁定即快照锁定，求解确定性，自由配对与快照一致；批次之后补登冲突所
        # 影响的自由配对不会被重新建出（冲突边绝不建立）。
        conn.execute("DELETE FROM assignments")
        for m_id, r_id in sorted(target_locked):
            conn.execute(
                "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1)",
                (m_id, r_id))

        beta = int(target_params["mismatch_weight"])
        alpha = int(target_params["load_weight"])
        org_cap = int(target_params["institution_cap"])
        solution, free_pairs = compute_solution(conn, beta, alpha, org_cap)
        for m_id, r_id in free_pairs:
            conn.execute(
                "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 0)",
                (m_id, r_id))
        set_setting(conn, "last_solution", solution)

        # 批次中非锁定配对若与当前冲突矩阵重合，求解不会重建该边（冲突边绝不建立）：
        # 这是唯一允许“快照配对未恢复”的情形，明确列出且不阻断回滚。
        skipped_conflict = sorted(
            [m_id, r_id]
            for m_id, r_id, locked in target_pairs
            if not locked and (m_id, r_id) in conflict_set
        )

        auto_note = f"回滚自批次 #{batch_id}"
        if target_row["note"]:
            auto_note += f"（源批次备注：{target_row['note']}）"
        if skipped_conflict:
            auto_note += f"；{len(skipped_conflict)} 个非锁定配对因当前作者冲突未恢复"
        snapshot = _snapshot_state(conn, auto_note, KIND_ROLLBACK, batch_id)
        new_id = _insert_batch(conn, snapshot, KIND_ROLLBACK, batch_id, auto_note)
        conn.execute("COMMIT")
    except AuditError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {
        "batch": get_batch(conn, new_id),
        "rolled_back_from": batch_id,
        "skipped_conflict_pairs": skipped_conflict,
        "solution": solution,
    }
