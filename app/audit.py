"""分配批次审计：不可变批次快照、批次差异与按批次原子回滚。

批次快照
========
每次**成功求解或锁定变更后**，管理员可调用 ``create_batch`` 把当前分配状态
固化为一个**不可变批次**。快照正文（``assignment_batches.snapshot``，JSON）
完整记录：

* 稿件—评审人**配对**（含 ``locked`` 锁定标记与 ``is_lead`` 主审标记）；
* **冲突约束**（m_id, r_id）；
* 当时的**求解参数**（主题不匹配权重 β、负载权重 α、每篇同机构最多人数 γ）；
* 当时仍存在的**稿件/评审人实体信息**（标题/主题/需求/主审需求、姓名/主题/
  容量/机构/主审资格）：这些是回滚时要恢复的属性，也保证实体被删除后批次
  页面仍可读；
* 上次求解（last_solution）的指标摘要；
* 管理员备注。

批次一经写入**不可修改、不可删除**（应用层不提供任何 UPDATE/DELETE 接口），
``params_hash`` 保存规范化快照 JSON 的 SHA-256，用于完整性校验。

按批次原子回滚
==============
``rollback`` 把当前分配恢复到目标批次：

* **仅恢复当时仍存在的稿件、评审人及冲突约束**：快照中的稿件/评审人按
  ID 把属性恢复为快照值；快照外（批次之后新建）的稿件/评审人**保留**。
  快照中的配对/冲突只作用于双方实体都仍存在的部分；
* **整单拒绝**（``AuditError 409``，事务回滚，绝不部分写入）的情形：
  1. 快照引用的稿件或评审人已被删除（无法核对/恢复其属性与配对）；
  2. 恢复快照锁定状态需要**移除或降级当前仍处于锁定的配对**——锁定是
     管理员显式意图，回滚不得静默解锁（请先在界面上解锁再回滚）；
  3. 快照内某个锁定配对在当前数据下与冲突约束重合（当前锁定冲突）；
  预检返回的每条原因都给出具体稿件/评审人与阻塞类型。
* 通过预检后，在**单个事务（BEGIN IMMEDIATE）**内恢复实体属性、把
  快照冲突集（仅限快照内实体）整体替换到当前冲突矩阵、删除全部非锁定
  配对与快照内实体上的锁定后重放快照锁定，再以**快照自由配对为优先
  保留集**复用求解器（修复模式）重算：能恢复的历史自由配对全部恢复，
  因容量/需求等变化而不可行的部分由求解器按字典序最小改动补齐；
  求解参数（β/α/γ）恢复为快照值。
* 成功后写入一个 **kind='rollback' 的新批次**（``rollback_of`` 指向来源
  批次），**绝不改写或删除任何历史批次**。

``preview_rollback`` 用相同的校验与一个**内存影子库**模拟回滚，只读、
不写入任何真实数据，返回阻塞原因、配对/冲突/参数差异与重算告警。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from .db import set_setting, transaction
from .sandbox import PARAM_KEYS
from .solver import compute_solution

KIND_SNAPSHOT = "snapshot"
KIND_ROLLBACK = "rollback"

# 回滚预检阻塞类型（API 稳定字段）
BLOCK_DELETED_M = "manuscript_deleted"       # 快照稿件已删除
BLOCK_DELETED_R = "reviewer_deleted"         # 快照评审人已删除
BLOCK_LOCK_REMOVE = "locked_pair_removed"    # 回滚会移除当前锁定配对
BLOCK_LOCK_DOWNGRADE = "locked_pair_downgraded"  # 回滚会把当前锁定降为自由分配
BLOCK_LOCK_CONFLICT = "locked_pair_conflict"     # 快照锁定与当前冲突重合


class AuditError(Exception):
    """业务异常：status_code 为应返回的 HTTP 状态码。"""

    def __init__(self, status_code: int, message: str, blockers: list[dict] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.blockers = blockers or []


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------- 快照构建
def build_snapshot(conn: sqlite3.Connection) -> dict:
    """读取当前状态，构建不可变批次快照（Python 对象，尚未落库）。"""
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
    lead_set: set[tuple[int, int]] = set()
    sol = None
    row = conn.execute("SELECT value FROM settings WHERE key = 'last_solution'").fetchone()
    if row is not None and row["value"] is not None:
        sol = json.loads(row["value"])
        lead_set = {tuple(p) for p in sol.get("lead_pairs", [])}

    pairs = [
        {"manuscript_id": r["m_id"], "reviewer_id": r["r_id"],
         "locked": bool(r["locked"]),
         "is_lead": (r["m_id"], r["r_id"]) in lead_set}
        for r in conn.execute(
            "SELECT m_id, r_id, locked FROM assignments ORDER BY m_id, r_id")
    ]

    if sol is not None:
        solution_summary = {
            "solved_at": sol.get("solved_at"),
            "total_cost": sol.get("total_cost", 0),
            "total_assigned": sol.get("total_assigned", 0),
            "total_required": sol.get("total_required", 0),
            "locked_count": sol.get("locked_count", 0),
            "total_lead_assigned": sol.get("total_lead_assigned", 0),
            "total_lead_required": sol.get("total_lead_required", 0),
            "fully_satisfied": sol.get("fully_satisfied", False),
            "unmet": [
                {"manuscript_id": u.get("manuscript_id"), "title": u.get("title"),
                 "required": u.get("required"), "assigned": u.get("assigned"),
                 "deficit": u.get("deficit"), "lead_deficit": u.get("lead_deficit")}
                for u in sol.get("unmet", [])
            ],
        }
    else:
        solution_summary = None

    return {
        "version": 1,
        "params": {key: _read_setting(conn, key) for key in PARAM_KEYS},
        "manuscripts": manuscripts,
        "reviewers": reviewers,
        "conflicts": conflicts,
        "pairs": pairs,
        "solution_summary": solution_summary,
    }


def _read_setting(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return int(row["value"]) if row is not None and row["value"] is not None else None


def snapshot_hash(snapshot: dict) -> str:
    """快照规范化 JSON 的 SHA-256（完整性指纹；与业务指纹互相独立）。"""
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- 创建批次
def create_batch(conn: sqlite3.Connection, note: str = "",
                 kind: str = KIND_SNAPSHOT, rollback_of: int | None = None,
                 snapshot: dict | None = None, created_at: str | None = None) -> dict:
    """把当前（或给定）状态固化为不可变批次。仅 INSERT，从不修改历史批次。"""
    if snapshot is None:
        snapshot = build_snapshot(conn)
    meta = _batch_meta(note, kind, rollback_of, snapshot, created_at)
    with transaction(conn):
        meta["id"] = _insert_batch(conn, meta)
    return serialize_batch_meta(meta)


def _batch_meta(note: str, kind: str, rollback_of: int | None,
                snapshot: dict, created_at: str | None) -> dict:
    """组装批次行元数据（含快照正文与计数）。"""
    pairs = snapshot["pairs"]
    return {
        "created_at": created_at or _now(),
        "kind": kind,
        "note": (note or "").strip(),
        "snapshot": snapshot,
        "params_hash": snapshot_hash(snapshot),
        "rollback_of": rollback_of,
        "pair_count": len(pairs),
        "locked_count": sum(1 for p in pairs if p["locked"]),
        "lead_count": sum(1 for p in pairs if p["is_lead"]),
        "conflict_count": len(snapshot["conflicts"]),
    }


def _insert_batch(conn: sqlite3.Connection, meta: dict) -> int:
    """插入批次行（不自行提交）；供独立创建与回滚事务内部复用。"""
    cur = conn.execute(
        "INSERT INTO assignment_batches"
        "(created_at, kind, note, snapshot, params_hash, rollback_of, "
        " pair_count, locked_count, lead_count, conflict_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (meta["created_at"], meta["kind"], meta["note"],
         json.dumps(meta["snapshot"], ensure_ascii=False), meta["params_hash"],
         meta["rollback_of"], meta["pair_count"], meta["locked_count"],
         meta["lead_count"], meta["conflict_count"]),
    )
    return cur.lastrowid


# ------------------------------------------------------------- 读取
def _get_row(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM assignment_batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise AuditError(404, f"分配批次 #{batch_id} 不存在。")
    return row


def list_batches(conn: sqlite3.Connection) -> list[dict]:
    """批次时间线（仅元数据，不含快照正文；按 id 升序即按时间升序）。"""
    rows = conn.execute(
        "SELECT * FROM assignment_batches ORDER BY id").fetchall()
    return [serialize_batch_meta(r) for r in rows]


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict:
    """批次详情：元数据 + 完整不可变快照。"""
    row = _get_row(conn, batch_id)
    return serialize_batch_full(row)


def serialize_batch_meta(row: sqlite3.Row | dict) -> dict:
    """批次时间线/状态接口使用的元数据（不含可能较大的快照正文）。"""
    d = dict(row)
    return {
        "id": d["id"],
        "created_at": d["created_at"],
        "kind": d["kind"],
        "note": d["note"] or "",
        "params_hash": d["params_hash"],
        "rollback_of": d["rollback_of"],
        "pair_count": d["pair_count"],
        "locked_count": d["locked_count"],
        "lead_count": d["lead_count"],
        "conflict_count": d["conflict_count"],
    }


def serialize_batch_full(row: sqlite3.Row) -> dict:
    meta = serialize_batch_meta(row)
    snapshot = json.loads(row["snapshot"])
    meta["snapshot"] = snapshot
    # 完整性校验：读取后重算哈希，存储正文被外部篡改时可在 UI/API 上发现
    meta["intact"] = snapshot_hash(snapshot) == meta["params_hash"]
    return meta


# ------------------------------------------------------------- 批次差异
def diff_batches(conn: sqlite3.Connection, base_id: int, target_id: int) -> dict:
    """两个批次按时间对比（target 相对 base 的变化：增/删/改）。"""
    base = json.loads(_get_row(conn, base_id)["snapshot"])
    target = json.loads(_get_row(conn, target_id)["snapshot"])
    return _diff_snapshots(base, target)


def _index_entities(items: list[dict]) -> dict[int, dict]:
    return {int(x["id"]): x for x in items}


def _diff_entities(base_items: list[dict], target_items: list[dict],
                   label: str) -> dict:
    bm, tm = _index_entities(base_items), _index_entities(target_items)
    added, removed, changed = [], [], []
    for eid, t in sorted(tm.items()):
        if eid not in bm:
            added.append(t)
        elif bm[eid] != t:
            changed.append({"id": eid, "before": bm[eid], "after": t})
    for eid, b in sorted(bm.items()):
        if eid not in tm:
            removed.append(b)
    return {"added": added, "removed": removed, "changed": changed,
            "label": label}


def _pair_key(p: dict) -> tuple[int, int]:
    return (int(p["manuscript_id"]), int(p["reviewer_id"]))


def _conflict_key(c: list[int] | tuple[int, int]) -> tuple[int, int]:
    return (int(c[0]), int(c[1]))


def _diff_snapshots(base: dict, target: dict) -> dict:
    """计算 target 相对 base 的差异（配对/冲突/参数/实体）。"""
    # 名称取两个快照的并集：新增/移除的配对至少能在一侧找到名称
    m_titles, r_names = {}, {}
    for snap in (base, target):
        for m in snap["manuscripts"]:
            m_titles.setdefault(int(m["id"]), m.get("title"))
        for r in snap["reviewers"]:
            r_names.setdefault(int(r["id"]), r.get("name"))

    def with_names(p: dict) -> dict:
        m_id, r_id = _pair_key(p)
        return {
            "manuscript_id": m_id, "reviewer_id": r_id,
            "locked": p["locked"], "is_lead": p["is_lead"],
            "manuscript_title": m_titles.get(m_id, f"#{m_id}"),
            "reviewer_name": r_names.get(r_id, f"#{r_id}"),
        }

    bp = {_pair_key(p): p for p in base["pairs"]}
    tp = {_pair_key(p): p for p in target["pairs"]}
    pair_added, pair_removed, pair_changed = [], [], []
    for key, t in sorted(tp.items()):
        if key not in bp:
            pair_added.append(with_names(t))
        else:
            b = bp[key]
            if b["locked"] != t["locked"] or b["is_lead"] != t["is_lead"]:
                pair_changed.append({"pair": with_names(t),
                                     "locked_before": b["locked"], "locked_after": t["locked"],
                                     "lead_before": b["is_lead"], "lead_after": t["is_lead"]})
    for key, b in sorted(bp.items()):
        if key not in tp:
            pair_removed.append(with_names(b))

    bc = {_conflict_key(c) for c in base["conflicts"]}
    tc = {_conflict_key(c) for c in target["conflicts"]}
    conflict_added = sorted(tc - bc)
    conflict_removed = sorted(bc - tc)

    param_changes = {
        key: {"before": base["params"].get(key), "after": target["params"].get(key)}
        for key in PARAM_KEYS
        if base["params"].get(key) != target["params"].get(key)
    }
    return {
        "pairs": {"added": pair_added, "removed": pair_removed, "changed": pair_changed},
        "conflicts": {
            "added": [[m, r] for m, r in conflict_added],
            "removed": [[m, r] for m, r in conflict_removed],
        },
        "params": param_changes,
        "manuscripts": _diff_entities(base["manuscripts"], target["manuscripts"], "稿件"),
        "reviewers": _diff_entities(base["reviewers"], target["reviewers"], "评审人"),
    }


# ------------------------------------------------------------- 回滚预检
def _scope(snapshot: dict) -> tuple[set[int], set[int],
                                    set[tuple[int, int]], set[tuple[int, int]],
                                    set[tuple[int, int]], set[tuple[int, int]]]:
    """解析快照范围：(稿件 ID, 评审人 ID, 全部配对, 锁定配对, 自由配对, 冲突)。"""
    m_ids = {int(m["id"]) for m in snapshot["manuscripts"]}
    r_ids = {int(r["id"]) for r in snapshot["reviewers"]}
    all_pairs, locked, free = set(), set(), set()
    for p in snapshot["pairs"]:
        key = (int(p["manuscript_id"]), int(p["reviewer_id"]))
        all_pairs.add(key)
        (locked if p["locked"] else free).add(key)
    conflicts = {_conflict_key(c) for c in snapshot["conflicts"]}
    return m_ids, r_ids, all_pairs, locked, free, conflicts


def _check_blockers(conn: sqlite3.Connection, snapshot: dict
                    ) -> tuple[set[int], set[int], list[dict], set[tuple[int, int]],
                               set[tuple[int, int]], set[tuple[int, int]], set[tuple[int, int]]]:
    """回滚预检：返回 (快照稿件集, 快照评审集, 阻塞原因, 快照全部配对,
    快照锁定配对, 快照自由配对, 快照冲突)。发现任何阻塞即收集，最终整单拒绝。"""
    m_ids, r_ids, all_pairs, locked_pairs, free_pairs, snap_conflicts = _scope(snapshot)

    cur_m = {r["id"] for r in conn.execute("SELECT id FROM manuscripts")}
    cur_r = {r["id"] for r in conn.execute("SELECT id FROM reviewers")}
    deleted_m = sorted(m_ids - cur_m)
    deleted_r = sorted(r_ids - cur_r)

    # 当前锁定配对（以配对为键）
    current_locked = {
        (r["m_id"], r["r_id"])
        for r in conn.execute("SELECT m_id, r_id FROM assignments WHERE locked = 1")
    }
    current_conflicts = {
        (r["m_id"], r["r_id"])
        for r in conn.execute("SELECT m_id, r_id FROM conflicts")
    }

    blockers: list[dict] = []
    m_titles = {m["id"]: m["title"] for m in snapshot["manuscripts"]}
    r_names = {r["id"]: r["name"] for r in snapshot["reviewers"]}
    for m_id in deleted_m:
        blockers.append({
            "code": BLOCK_DELETED_M, "manuscript_id": m_id,
            "message": f"批次中的稿件 #{m_id}"
                       f"（{m_titles.get(m_id, '标题未知')}）已被删除，无法恢复其属性与配对。",
        })
    for r_id in deleted_r:
        blockers.append({
            "code": BLOCK_DELETED_R, "reviewer_id": r_id,
            "message": f"批次中的评审人 #{r_id}"
                       f"（{r_names.get(r_id, '姓名未知')}）已被删除，无法恢复其属性与配对。",
        })

    # 仅当双方实体都仍存在时，锁定/冲突类阻塞才有意义（已删对象已在上面报出）
    for m_id, r_id in sorted(current_locked):
        if m_id not in cur_m or r_id not in cur_r:
            continue
        if (m_id, r_id) not in all_pairs:
            # 快照实体范围内的当前锁定，但快照里没有该配对 → 回滚将移除这把锁
            if m_id in m_ids and r_id in r_ids:
                blockers.append({
                    "code": BLOCK_LOCK_REMOVE, "manuscript_id": m_id, "reviewer_id": r_id,
                    "message": f"当前锁定配对（稿件 #{m_id} / 评审人 #{r_id}）"
                               f"不在目标批次中，回滚会移除该锁定；请先在分配结果页解锁后再回滚。",
                })
        elif (m_id, r_id) not in locked_pairs:
            # 配对在快照中存在但当时不是锁定 → 回滚会把当前锁定降为自由分配
            blockers.append({
                "code": BLOCK_LOCK_DOWNGRADE, "manuscript_id": m_id, "reviewer_id": r_id,
                "message": f"当前锁定配对（稿件 #{m_id} / 评审人 #{r_id}）"
                           f"在目标批次中为非锁定分配，回滚会解除该锁定；请先解锁后再回滚。",
            })

    # 快照锁定配对与当前冲突重合 → 恢复锁定会违反冲突硬约束
    for m_id, r_id in sorted(locked_pairs):
        if m_id in cur_m and r_id in cur_r and (m_id, r_id) in current_conflicts:
            blockers.append({
                "code": BLOCK_LOCK_CONFLICT, "manuscript_id": m_id, "reviewer_id": r_id,
                "message": f"目标批次的锁定配对（稿件 #{m_id} / 评审人 #{r_id}）"
                           f"当前存在作者冲突，不能恢复为锁定；请先解除该冲突或更换目标批次。",
            })

    return (m_ids, r_ids, blockers, all_pairs, locked_pairs, free_pairs,
            snap_conflicts)


def _raise_if_blocked(blockers: list[dict]) -> None:
    if blockers:
        raise AuditError(
            409,
            f"回滚预检发现 {len(blockers)} 项阻塞，已整单拒绝（未写入任何内容）：\n"
            + "；".join(b["message"] for b in blockers),
            blockers=blockers,
        )


# ----------------------------------------------------- 内存影子库（只读模拟）
def _clone_to_memory(conn: sqlite3.Connection) -> sqlite3.Connection:
    """把当前业务表复制到一个内存 SQLite（回滚预检专用，绝不触碰真实库）。"""
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("PRAGMA foreign_keys = ON")
    mem.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE manuscripts (
            id INTEGER PRIMARY KEY, title TEXT, topics TEXT,
            required_reviews INTEGER, required_leads INTEGER);
        CREATE TABLE reviewers (
            id INTEGER PRIMARY KEY, name TEXT, topics TEXT, capacity INTEGER,
            institution TEXT, can_lead INTEGER);
        CREATE TABLE conflicts (m_id INTEGER, r_id INTEGER, PRIMARY KEY (m_id, r_id));
        CREATE TABLE assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, m_id INTEGER, r_id INTEGER,
            locked INTEGER, UNIQUE (m_id, r_id));
        """)
    for table, cols in (
        ("settings", "key, value"),
        ("manuscripts", "id, title, topics, required_reviews, required_leads"),
        ("reviewers", "id, name, topics, capacity, institution, can_lead"),
        ("conflicts", "m_id, r_id"),
        ("assignments", "m_id, r_id, locked"),
    ):
        rows = conn.execute(f"SELECT {cols} FROM {table}").fetchall()
        if rows:
            placeholders = ",".join("?" for _ in rows[0].keys())
            mem.executemany(
                f"INSERT INTO {table}({cols}) VALUES ({placeholders})",
                [tuple(r) for r in rows])
    mem.commit()
    return mem


def _apply_restore(dst: sqlite3.Connection, snapshot: dict,
                  m_ids: set[int], r_ids: set[int],
                  locked_pairs: set[tuple[int, int]],
                  snap_conflicts: set[tuple[int, int]]) -> None:
    """把目标批次状态恢复到 dst（影子库或真实库）的实体属性/冲突/锁定/参数。

    仅作用于快照内（批次时存在）的实体；快照外（批次后新建）的实体保留。
    调用方须已通过 _check_blockers 预检（无已删对象、无锁定冲突）。
    """
    # 1) 实体属性恢复（ID 相同即恢复为快照值）
    for m in snapshot["manuscripts"]:
        dst.execute(
            "UPDATE manuscripts SET title = ?, topics = ?, required_reviews = ?, "
            "required_leads = ? WHERE id = ?",
            (m["title"], json.dumps(m["topics"], ensure_ascii=False),
             m["required_reviews"], m["required_leads"], m["id"]))
    for r in snapshot["reviewers"]:
        dst.execute(
            "UPDATE reviewers SET name = ?, topics = ?, capacity = ?, institution = ?, "
            "can_lead = ? WHERE id = ?",
            (r["name"], json.dumps(r["topics"], ensure_ascii=False),
             r["capacity"], r["institution"] or "", 1 if r["can_lead"] else 0, r["id"]))

    # 2) 冲突矩阵：仅对“双方都在快照中”的冲突整体替换为快照冲突集；
    #    涉及批次后新实体（任一方不在快照）的“范围外”冲突原样保留。
    #    按显式配对列表逐条删除，语义即“双方皆在快照范围”，不依赖 IN 子句的
    #    笛卡尔积匹配，范围判定集中在一处、便于审计与维护。
    in_scope_existing = [
        (r["m_id"], r["r_id"])
        for r in dst.execute("SELECT m_id, r_id FROM conflicts")
        if r["m_id"] in m_ids and r["r_id"] in r_ids
    ]
    for m_id, r_id in in_scope_existing:
        dst.execute("DELETE FROM conflicts WHERE m_id = ? AND r_id = ?", (m_id, r_id))
    for m_id, r_id in sorted(snap_conflicts):
        dst.execute(
            "INSERT OR IGNORE INTO conflicts(m_id, r_id) VALUES (?, ?)",
            (m_id, r_id))

    # 3) 配对：删除全部非锁定配对（全局重算）；锁定只处理“双方都在快照中”的
    #    当前锁（涉及批次后新实体的范围外锁保留）——预检已保证快照内当前锁与
    #    快照锁定集一致（不一致会以 lock_remove/lock_downgrade 整单拒绝），
    #    故删除后重放快照锁定净效果等于无变化；同样按显式配对列表删除。
    dst.execute("DELETE FROM assignments WHERE locked = 0")
    scoped_existing_locks = [
        (r["m_id"], r["r_id"])
        for r in dst.execute("SELECT m_id, r_id FROM assignments WHERE locked = 1")
        if r["m_id"] in m_ids and r["r_id"] in r_ids
    ]
    for m_id, r_id in scoped_existing_locks:
        dst.execute(
            "DELETE FROM assignments WHERE m_id = ? AND r_id = ? AND locked = 1",
            (m_id, r_id))
    for m_id, r_id in sorted(locked_pairs):
        dst.execute(
            "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1)",
            (m_id, r_id))

    # 4) 求解参数恢复为快照值
    for key, value in snapshot["params"].items():
        set_setting(dst, key, value)


def _project_rollback(conn: sqlite3.Connection, snapshot: dict,
                      m_ids: set[int], r_ids: set[int],
                      locked_pairs: set[tuple[int, int]],
                      free_pairs: set[tuple[int, int]],
                      snap_conflicts: set[tuple[int, int]]
                      ) -> tuple[dict, list[tuple[int, int]], set[tuple[int, int]]]:
    """在内存影子库上模拟回滚并重算，返回 (重算解, 最终配对, 未能恢复的快照自由配对)。"""
    mem = _clone_to_memory(conn)
    try:
        _apply_restore(mem, snapshot, m_ids, r_ids, locked_pairs, snap_conflicts)
        params = snapshot["params"]
        # 修复模式：快照自由配对为优先保留集（换位费 0），其余配对承担换位惩罚，
        # 从而在硬约束允许时恢复尽可能多的历史自由配对。
        solution, new_pairs = compute_solution(
            mem, params["mismatch_weight"], params["load_weight"],
            params["institution_cap"], retain_pairs=free_pairs)
        final_pairs = locked_pairs | set(new_pairs)
        unrestored = {p for p in free_pairs if p not in final_pairs}
        return solution, sorted(final_pairs), unrestored
    finally:
        mem.close()


def preview_rollback(conn: sqlite3.Connection, batch_id: int) -> dict:
    """只读回滚预检：阻塞原因 + 配对/冲突/参数差异 + 重算告警（不写入真实库）。"""
    row = _get_row(conn, batch_id)
    snapshot = json.loads(row["snapshot"])
    (m_ids, r_ids, blockers, all_pairs, locked_pairs,
     free_pairs, snap_conflicts) = _check_blockers(conn, snapshot)

    result = {
        "batch": serialize_batch_meta(row),
        "blockers": blockers,
        "can_rollback": not blockers,
    }
    if blockers:
        return result

    solution, final_pairs, unrestored = _project_rollback(
        conn, snapshot, m_ids, r_ids, locked_pairs, free_pairs, snap_conflicts)

    current_pairs = {
        (r["m_id"], r["r_id"]) for r in conn.execute("SELECT m_id, r_id FROM assignments")
    }
    current_locked = {
        (r["m_id"], r["r_id"])
        for r in conn.execute("SELECT m_id, r_id FROM assignments WHERE locked = 1")
    }
    current_conflicts = {
        (r["m_id"], r["r_id"]) for r in conn.execute("SELECT m_id, r_id FROM conflicts")
    }
    # 当前解中的实际主审（用于标注被移除配对“回滚前”的主审身份）
    cur_sol_row = conn.execute(
        "SELECT value FROM settings WHERE key='last_solution'").fetchone()
    current_lead: set[tuple[int, int]] = set()
    if cur_sol_row is not None and cur_sol_row["value"] is not None:
        current_lead = {tuple(p) for p in json.loads(cur_sol_row["value"]).get("lead_pairs", [])}
    final_set = set(final_pairs)
    lead_set = {tuple(p) for p in solution.get("lead_pairs", [])}
    m_titles = {m["id"]: m["title"] for m in snapshot["manuscripts"]}
    r_names = {r["id"]: r["name"] for r in snapshot["reviewers"]}
    # 批次后新建实体的名称（差异展示中快照查不到名称时使用）
    m_titles_cur = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM manuscripts")}
    r_names_cur = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM reviewers")}
    m_titles = {**m_titles_cur, **m_titles}
    r_names = {**r_names_cur, **r_names}

    def pair_info(key: tuple[int, int], is_after: bool) -> dict:
        m_id, r_id = key
        return {
            "manuscript_id": m_id, "reviewer_id": r_id,
            "manuscript_title": m_titles.get(m_id, f"#{m_id}"),
            "reviewer_name": r_names.get(r_id, f"#{r_id}"),
            "locked": (key in locked_pairs) if is_after else (key in current_locked),
            "is_lead": (key in lead_set) if is_after else (key in current_lead),
        }

    added = sorted(final_set - current_pairs)
    removed = sorted(current_pairs - final_set)

    cur_params = {key: _read_setting(conn, key) for key in PARAM_KEYS}
    new_params = snapshot["params"]
    param_changes = {
        key: {"before": cur_params.get(key), "after": new_params.get(key)}
        for key in PARAM_KEYS if cur_params.get(key) != new_params.get(key)
    }

    conflict_added = sorted(snap_conflicts - current_conflicts)
    conflict_removed = sorted(current_conflicts - snap_conflicts)
    # 仅统计快照实体范围内的冲突变化（范围外冲突回滚后保持不变）
    conflict_added = [c for c in conflict_added if c[0] in m_ids and c[1] in r_ids]
    conflict_removed = [c for c in conflict_removed if c[0] in m_ids and c[1] in r_ids]

    # 实体属性以“当前 → 批次（回滚后）”方向展示变化
    changed_m = _entity_restore_diff(conn, snapshot, "manuscripts", m_ids)
    changed_r = _entity_restore_diff(conn, snapshot, "reviewers", r_ids)

    result.update({
        "diff": {
            "pairs": {
                "added": [pair_info(k, True) for k in added],
                "removed": [pair_info(k, False) for k in removed],
            },
            "conflicts": {
                "added": [[m, r] for m, r in conflict_added],
                "removed": [[m, r] for m, r in conflict_removed],
            },
            "params": param_changes,
            "manuscripts": changed_m,
            "reviewers": changed_r,
        },
        "restored_pair_count": len(free_pairs) - len(unrestored),
        "unrestored_free_pairs": [pair_info(k, True) for k in sorted(unrestored)],
        "new_locked_pairs": sorted(locked_pairs),
        "warnings": solution.get("warnings", []),
        "metrics": {
            "total_cost": solution.get("total_cost", 0),
            "total_assigned": solution.get("total_assigned", 0),
            "total_required": solution.get("total_required", 0),
            "fully_satisfied": solution.get("fully_satisfied", False),
            "unmet": [
                {"manuscript_id": u.get("manuscript_id"), "title": u.get("title"),
                 "deficit": u.get("deficit"), "lead_deficit": u.get("lead_deficit")}
                for u in solution.get("unmet", [])
            ],
        },
        "created_at": _now(),
    })
    return result


def _current_entities(conn: sqlite3.Connection, table: str) -> list[dict]:
    if table == "manuscripts":
        return [
            {"id": r["id"], "title": r["title"], "topics": json.loads(r["topics"]),
             "required_reviews": r["required_reviews"], "required_leads": r["required_leads"]}
            for r in conn.execute("SELECT * FROM manuscripts ORDER BY id")
        ]
    return [
        {"id": r["id"], "name": r["name"], "topics": json.loads(r["topics"]),
         "capacity": r["capacity"], "institution": r["institution"] or "",
         "can_lead": bool(r["can_lead"])}
        for r in conn.execute("SELECT * FROM reviewers ORDER BY id")
    ]


def _entity_restore_diff(conn: sqlite3.Connection, snapshot: dict, table: str,
                        scope_ids: set[int]) -> dict:
    """实体属性的“当前 → 回滚后（快照值）”变化清单。"""
    snap_items = _index_entities(snapshot[table])
    cur_items = _index_entities(_current_entities(conn, table))
    changed = []
    for eid in sorted(scope_ids):
        b, a = cur_items.get(eid), snap_items.get(eid)
        if b is not None and a is not None and b != a:
            changed.append({"id": eid, "before": b, "after": a})
    return {"changed": changed, "label": "稿件" if table == "manuscripts" else "评审人"}


# ------------------------------------------------------------- 执行回滚
def rollback(conn: sqlite3.Connection, batch_id: int, note: str | None = None) -> dict:
    """按目标批次原子回滚；成功后写入 kind='rollback' 的新批次（不改写历史）。"""
    target_row = _get_row(conn, batch_id)
    snapshot = json.loads(target_row["snapshot"])

    # BEGIN IMMEDIATE：立即获取写锁，把“二次预检 + 恢复 + 重算 + 写新批次”
    # 串行化，防止预检后有并发锁定/删除插入。
    conn.execute("BEGIN IMMEDIATE")
    try:
        (m_ids, r_ids, blockers, all_pairs, locked_pairs,
         free_pairs, snap_conflicts) = _check_blockers(conn, snapshot)
        if blockers:
            conn.execute("ROLLBACK")
            _raise_if_blocked(blockers)

        _apply_restore(conn, snapshot, m_ids, r_ids, locked_pairs, snap_conflicts)

        # 以快照自由配对为优先保留集复用求解器（修复模式字典序）：
        # 能恢复的历史自由配对全部恢复，不可行者按最小改动补齐。
        params = snapshot["params"]
        solution, new_pairs = compute_solution(
            conn, params["mismatch_weight"], params["load_weight"],
            params["institution_cap"], retain_pairs=free_pairs)
        for m_id, r_id in new_pairs:
            conn.execute(
                "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 0)",
                (m_id, r_id))
        set_setting(conn, "last_solution", solution)

        # 成功后写入新批次（rollback_of 指向来源批次）；历史批次原样保留。
        # 直接使用 _insert_batch（不自行提交），确保新批次与上面的恢复同一事务。
        new_snapshot = build_snapshot(conn)
        default_note = f"按批次 #{batch_id} 原子回滚后自动创建"
        meta = _batch_meta(note or default_note, KIND_ROLLBACK, batch_id,
                           new_snapshot, _now())
        meta["id"] = _insert_batch(conn, meta)
        new_batch_meta = serialize_batch_meta(meta)
        conn.execute("COMMIT")
    except AuditError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {
        "rollback_batch": new_batch_meta,
        "source_batch_id": batch_id,
        "solution": solution,
        "warnings": solution.get("warnings", []),
    }
