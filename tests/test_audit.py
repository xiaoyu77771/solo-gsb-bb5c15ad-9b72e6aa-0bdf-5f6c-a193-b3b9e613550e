"""分配批次审计测试（仅标准库，风格与 test_sandbox / test_backfill 一致）。

运行：python -m tests.test_audit

覆盖：
* 快照内容：配对/主审标记/锁定/冲突/求解参数/实体信息/备注/完整性哈希；
* 快照不可变：接口层只允许新增，批次表不被回滚改写；
* 批次差异：配对增删、锁定/主审状态变化、冲突与参数变化、实体增删改；
* 回滚预检只读：绝不写库；阻塞原因（已删除稿件/评审人、当前锁定移除/降级、
  目标锁定与当前冲突重合）逐条给出并 can_rollback=False；
* 原子回滚：恢复实体属性/配对锁定/冲突/参数，批次之后新建实体保留，
  不可行的历史自由配对由求解器补齐，成功后写入新 rollback 批次且历史不改写；
* 整单拒绝时不产生任何数据变化、不新增批次；
* demo/reset 清空批次；旧库迁移幂等获得 assignment_batches 表。
"""
from __future__ import annotations

import json
import os
import tempfile

# 测试前指定独立数据库
_tmp = tempfile.mkdtemp(prefix="review-audit-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app.db import _connect, get_setting, init_db, set_setting  # noqa: E402
from app.audit import (  # noqa: E402
    AuditError,
    BLOCK_DELETED_M,
    BLOCK_DELETED_R,
    BLOCK_LOCK_CONFLICT,
    BLOCK_LOCK_DOWNGRADE,
    BLOCK_LOCK_REMOVE,
    KIND_ROLLBACK,
    KIND_SNAPSHOT,
    create_batch,
    diff_batches,
    get_batch,
    list_batches,
    preview_rollback,
    rollback,
    snapshot_hash,
)
from app.solver import solve  # noqa: E402


# ------------------------------------------------------------- 测试夹具
def _reset(conn) -> None:
    conn.execute("DELETE FROM assignment_batches")
    conn.execute("DELETE FROM assignments")
    conn.execute("DELETE FROM conflicts")
    conn.execute("DELETE FROM conflict_declarations")
    conn.execute("DELETE FROM manuscripts")
    conn.execute("DELETE FROM reviewers")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
    conn.commit()


def _add_m(conn, title="M", req=2, leads=1, m_id=None) -> int:
    cur = conn.execute(
        "INSERT INTO manuscripts(id, title, topics, required_reviews, required_leads) "
        "VALUES (?, ?, ?, ?, ?)",
        (m_id, title, json.dumps(["t"], ensure_ascii=False), req, leads))
    return cur.lastrowid if m_id is None else m_id


def _add_r(conn, name="r", cap=4, lead=0, inst="", r_id=None) -> int:
    cur = conn.execute(
        "INSERT INTO reviewers(id, name, topics, capacity, institution, can_lead) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (r_id, name, json.dumps(["t"], ensure_ascii=False), cap, inst, 1 if lead else 0))
    return cur.lastrowid if r_id is None else r_id


def _assign(conn, m, r, locked=0) -> None:
    conn.execute(
        "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, ?)",
        (m, r, locked))


def _pairs(conn) -> set[tuple[int, int]]:
    return {(r["m_id"], r["r_id"]) for r in conn.execute("SELECT m_id, r_id FROM assignments")}


def _locked(conn) -> set[tuple[int, int]]:
    return {(r["m_id"], r["r_id"])
            for r in conn.execute("SELECT m_id, r_id FROM assignments WHERE locked=1")}


def _conflicts(conn) -> set[tuple[int, int]]:
    return {(r["m_id"], r["r_id"]) for r in conn.execute("SELECT m_id, r_id FROM conflicts")}


def _expect_audit_error(fn, status: int, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except AuditError as e:
        assert e.status_code == status, e.message
        return e
    raise AssertionError(f"应当抛出 AuditError({status})")


def _scenario(conn):
    """1 篇需求 2/主审 1 的稿件 + 4 名评审（r1 可主审）。返回 (m, ids)。"""
    _reset(conn)
    m = _add_m(conn, "M1", req=2, leads=1)
    ids = [_add_r(conn, name, lead=int(name == "r1"))
           for name in ("r1", "r2", "r3", "r4")]
    conn.commit()
    solve(conn)
    return m, ids


# ----------------------------------------------------------------- 快照内容
def test_snapshot_contents_and_hash() -> None:
    init_db(seed_demo=False)
    conn = _connect()
    try:
        m, ids = _scenario(conn)
        _assign(conn, m, ids[3], locked=1)  # 额外锁 r4
        conn.commit()
        sol = solve(conn)
        meta = create_batch(conn, note="首批")
        assert meta["kind"] == KIND_SNAPSHOT and meta["note"] == "首批"
        assert meta["pair_count"] >= 2 and meta["locked_count"] == 1
        assert meta["lead_count"] >= 1 and meta["rollback_of"] is None

        full = get_batch(conn, meta["id"])
        snap = full["snapshot"]
        assert full["intact"] is True  # 哈希与正文一致
        assert snapshot_hash(snap) == full["params_hash"]
        # 参数完整
        for key in ("mismatch_weight", "load_weight", "institution_cap"):
            assert key in snap["params"]
        # 配对带 locked / is_lead 标记
        locked_pairs = [tuple((pp["manuscript_id"], pp["reviewer_id"]))
                        for pp in snap["pairs"] if pp["locked"]]
        assert locked_pairs == [(m, ids[3])]
        lead_pairs = {(pp["manuscript_id"], pp["reviewer_id"]) for pp in snap["pairs"] if pp["is_lead"]}
        assert lead_pairs == {tuple(x) for x in sol["lead_pairs"]}
        # 实体信息入库（即使将来删除也可读）
        assert {x["id"] for x in snap["manuscripts"]} == {m}
        assert {x["id"] for x in snap["reviewers"]} == set(ids)
        assert snap["manuscripts"][0]["title"] == "M1"
        # 求解指标摘要
        assert snap["solution_summary"]["total_assigned"] == sol["total_assigned"]
    finally:
        conn.close()


def test_get_missing_batch_404() -> None:
    conn = _connect()
    try:
        _expect_audit_error(get_batch, 404, conn, 99999)
        _expect_audit_error(preview_rollback, 404, conn, 99999)
    finally:
        conn.close()


# ----------------------------------------------------------------- 批次差异
def test_diff_batches_pair_conflict_params() -> None:
    conn = _connect()
    try:
        m, ids = _scenario(conn)
        b1 = create_batch(conn, "b1")
        before = set(_pairs(conn))

        # 加冲突（r2）→ 重解 → 新批次
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m, ids[1]))
        conn.commit()
        solve(conn)
        set_setting(conn, "mismatch_weight", 42)
        b2 = create_batch(conn, "b2")

        d = diff_batches(conn, b1["id"], b2["id"])
        assert [c for c in d["conflicts"]["added"]] == [[m, ids[1]]]
        assert d["conflicts"]["removed"] == []
        # r2 配对应被移除（它与本稿冲突）
        removed = {(p["manuscript_id"], p["reviewer_id"]) for p in d["pairs"]["removed"]}
        assert (m, ids[1]) in removed
        # 参数变化
        assert d["params"]["mismatch_weight"]["before"] != 42
        assert d["params"]["mismatch_weight"]["after"] == 42
        # 基准批次自身未被差异计算改动
        assert get_batch(conn, b1["id"])["snapshot"]["params"]["mismatch_weight"] != 42
        assert before  # 仅为静态检查占位
    finally:
        conn.close()


def test_diff_detects_lock_and_lead_status_change() -> None:
    conn = _connect()
    try:
        m, ids = _scenario(conn)
        b1 = create_batch(conn)
        # 锁一个当前存在的自由配对（本稿的任一非锁定配对）
        free_pairs = [(mm, rr) for (mm, rr) in _pairs(conn)
                      if (mm, rr) not in _locked(conn) and mm == m]
        assert free_pairs
        target = free_pairs[0]
        conn.execute(
            "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1) "
            "ON CONFLICT(m_id, r_id) DO UPDATE SET locked=1",
            (target[0], target[1]))
        conn.commit()
        solve(conn)
        b2 = create_batch(conn)
        d = diff_batches(conn, b1["id"], b2["id"])
        changed = {(c["pair"]["manuscript_id"], c["pair"]["reviewer_id"]): c
                   for c in d["pairs"]["changed"]}
        assert target in changed
        assert changed[target]["locked_before"] is False
        assert changed[target]["locked_after"] is True
    finally:
        conn.close()


def test_diff_detects_entity_add_remove_change() -> None:
    conn = _connect()
    try:
        m, ids = _scenario(conn)
        b1 = create_batch(conn)
        # 改标题、加评审人、删评审人 r4（先确保其无配对/锁定）
        conn.execute("UPDATE manuscripts SET title = ? WHERE id = ?", ("M1-改名", m))
        conn.execute("DELETE FROM reviewers WHERE id = ?", (ids[3],))
        _add_r(conn, "r5-new")
        conn.commit()
        b2 = create_batch(conn)
        d = diff_batches(conn, b1["id"], b2["id"])
        assert any(x["id"] == ids[3] for x in d["reviewers"]["removed"])
        assert any(x["name"] == "r5-new" for x in d["reviewers"]["added"])
        titles = {c["id"]: (c["before"]["title"], c["after"]["title"])
                  for c in d["manuscripts"]["changed"]}
        assert titles[m] == ("M1", "M1-改名")
    finally:
        conn.close()


# ------------------------------------------------------------- 预检只读与阻塞
def test_preview_is_read_only() -> None:
    conn = _connect()
    try:
        m, ids = _scenario(conn)
        b1 = create_batch(conn)
        # 修改当前数据后做预检
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m, ids[1]))
        conn.commit()
        before = {
            "pairs": _pairs(conn), "locked": _locked(conn), "conflicts": _conflicts(conn),
            "beta": get_setting(conn, "mismatch_weight"),
            "batches": len(list_batches(conn)),
        }
        pv = preview_rollback(conn, b1["id"])
        assert pv["can_rollback"] is True
        assert _pairs(conn) == before["pairs"]
        assert _locked(conn) == before["locked"]
        assert _conflicts(conn) == before["conflicts"]
        assert get_setting(conn, "mismatch_weight") == before["beta"]
        assert len(list_batches(conn)) == before["batches"]
    finally:
        conn.close()


def test_block_deleted_manuscript_and_reviewer() -> None:
    conn = _connect()
    try:
        m, ids = _scenario(conn)
        b1 = create_batch(conn)
        conn.execute("DELETE FROM manuscripts WHERE id = ?", (m,))
        conn.commit()
        pv = preview_rollback(conn, b1["id"])
        assert pv["can_rollback"] is False
        codes = {b["code"] for b in pv["blockers"]}
        assert BLOCK_DELETED_M in codes
        assert any(b["manuscript_id"] == m for b in pv["blockers"])

        # 执行同样整单 409，且不新增批次
        n_before = len(list_batches(conn))
        err = _expect_audit_error(rollback, 409, conn, b1["id"])
        assert any(b["code"] == BLOCK_DELETED_M for b in err.blockers)
        assert len(list_batches(conn)) == n_before

        # 恢复稿件后改为删除评审人
        _add_m(conn, "M1-另建", m_id=m + 100)  # 不同 ID 不算恢复
        conn.execute("DELETE FROM reviewers WHERE id = ?", (ids[0],))
        conn.commit()
        pv2 = preview_rollback(conn, b1["id"])
        codes2 = {b["code"] for b in pv2["blockers"]}
        assert BLOCK_DELETED_M in codes2  # 原 ID 仍缺失
        assert BLOCK_DELETED_R in codes2
    finally:
        conn.close()


def test_block_current_lock_removed_or_downgraded() -> None:
    conn = _connect()
    try:
        m, ids = _scenario(conn)
        b1 = create_batch(conn)  # 当时 r4 无配对，r2 为自由配对

        # 新增一把批次外锁定（r4）→ 回滚会移除该锁定
        _assign(conn, m, ids[3], locked=1)
        conn.commit()
        pv = preview_rollback(conn, b1["id"])
        assert [b["code"] for b in pv["blockers"]] == [BLOCK_LOCK_REMOVE]
        _expect_audit_error(rollback, 409, conn, b1["id"])

        # 解锁 r4 改为锁住批次内的自由配对 r2 → 回滚会降级
        conn.execute("DELETE FROM assignments WHERE m_id=? AND r_id=? AND locked=1",
                     (m, ids[3]))
        conn.execute(
            "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1) "
            "ON CONFLICT(m_id, r_id) DO UPDATE SET locked=1", (m, ids[1]))
        conn.commit()
        solve(conn)
        pv2 = preview_rollback(conn, b1["id"])
        assert [b["code"] for b in pv2["blockers"]] == [BLOCK_LOCK_DOWNGRADE]
    finally:
        conn.close()


def test_block_target_lock_vs_current_conflict() -> None:
    conn = _connect()
    try:
        m, ids = _scenario(conn)
        # 造一个含 r3 锁定的目标批次
        _assign(conn, m, ids[2], locked=1)
        conn.commit()
        solve(conn)
        b1 = create_batch(conn)

        # 之后解锁 r3 并对 (m, r3) 登记冲突
        conn.execute("DELETE FROM assignments WHERE m_id=? AND r_id=? AND locked=1",
                     (m, ids[2]))
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m, ids[2]))
        conn.commit()
        solve(conn)
        pv = preview_rollback(conn, b1["id"])
        assert any(b["code"] == BLOCK_LOCK_CONFLICT
                   and b["manuscript_id"] == m and b["reviewer_id"] == ids[2]
                   for b in pv["blockers"])
        _expect_audit_error(rollback, 409, conn, b1["id"])
    finally:
        conn.close()


# ------------------------------------------------------------- 原子回滚
def test_rollback_restores_pairs_conflicts_params_and_attributes() -> None:
    conn = _connect()
    try:
        m, ids = _scenario(conn)
        set_setting(conn, "mismatch_weight", 10)
        b1 = create_batch(conn, "基线")
        b1_pairs = set(_pairs(conn))

        # ------ 之后发生一系列变更：属性、冲突、参数、配对 ------
        conn.execute("UPDATE manuscripts SET title=? WHERE id=?", ("M1-改", m))
        conn.execute("UPDATE reviewers SET capacity=1 WHERE id=?", (ids[0],))
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m, ids[1]))
        conn.commit()
        set_setting(conn, "mismatch_weight", 77)
        solve(conn)
        assert _pairs(conn) != b1_pairs
        assert get_setting(conn, "mismatch_weight") == 77
        n_batches_before = len(list_batches(conn))

        # 预检通过
        pv = preview_rollback(conn, b1["id"])
        assert pv["can_rollback"] is True
        assert pv["diff"]["params"]["mismatch_weight"]["after"] == 10
        assert any(c["id"] == m for c in pv["diff"]["manuscripts"]["changed"])

        res = rollback(conn, b1["id"], note="回滚验证")
        # 新批次写入且指向来源
        nb = res["rollback_batch"]
        assert nb["kind"] == KIND_ROLLBACK and nb["rollback_of"] == b1["id"]
        assert nb["note"] == "回滚验证"
        assert len(list_batches(conn)) == n_batches_before + 1
        # 历史批次未被改写
        old = get_batch(conn, b1["id"])
        assert old["note"] == "基线" and old["kind"] == KIND_SNAPSHOT
        # 状态恢复
        assert conn.execute("SELECT title FROM manuscripts WHERE id=?", (m,)).fetchone()["title"] == "M1"
        assert conn.execute("SELECT capacity FROM reviewers WHERE id=?", (ids[0],)).fetchone()["capacity"] == 4
        assert _conflicts(conn) == set()
        assert get_setting(conn, "mismatch_weight") == 10
        # 历史自由配对恢复（全部可行）
        assert _pairs(conn) == b1_pairs
    finally:
        conn.close()


def test_rollback_keeps_entities_created_after_batch() -> None:
    conn = _connect()
    try:
        m, ids = _scenario(conn)
        b1 = create_batch(conn)
        # 批次之后新建稿件（需 1 名）与评审人
        m2 = _add_m(conn, "M2-新", req=1, leads=0)
        r5 = _add_r(conn, "r5-新", cap=2)
        conn.commit()
        solve(conn)
        m2_pairs_before = {(mm, rr) for (mm, rr) in _pairs(conn) if mm == m2}
        assert m2_pairs_before  # 新稿件已被安排

        # 顺手改老稿件标题，回滚后应恢复；新实体保留
        conn.execute("UPDATE manuscripts SET title='M1-改' WHERE id=?", (m,))
        conn.commit()

        res = rollback(conn, b1["id"])
        assert conn.execute("SELECT title FROM manuscripts WHERE id=?", (m2,)).fetchone()["title"] == "M2-新"
        assert conn.execute("SELECT name FROM reviewers WHERE id=?", (r5,)).fetchone()["name"] == "r5-新"
        assert conn.execute("SELECT title FROM manuscripts WHERE id=?", (m,)).fetchone()["title"] == "M1"
        # 新批次快照包含批次后的新实体
        snap = get_batch(conn, res["rollback_batch"]["id"])["snapshot"]
        snap_m_ids = {x["id"] for x in snap["manuscripts"]}
        assert {m, m2} <= snap_m_ids
        # 新稿件在全局重算后仍被安排（锁定优先恢复后求解器补全）
        m2_pairs_after = {(mm, rr) for (mm, rr) in _pairs(conn) if mm == m2}
        assert m2_pairs_after == m2_pairs_before or m2_pairs_after
    finally:
        conn.close()


def test_rollback_lock_preserved_and_history_not_rewritten() -> None:
    conn = _connect()
    try:
        m, ids = _scenario(conn)
        # 目标批次含一把 r3 锁
        _assign(conn, m, ids[2], locked=1)
        conn.commit()
        solve(conn)
        b1 = create_batch(conn)
        b1_snapshot = json.dumps(get_batch(conn, b1["id"])["snapshot"], sort_keys=True)

        # 之后解锁 r3 并自由重解
        conn.execute("DELETE FROM assignments WHERE m_id=? AND r_id=? AND locked=1",
                     (m, ids[2]))
        conn.commit()
        solve(conn)
        assert (m, ids[2]) not in _locked(conn)

        res = rollback(conn, b1["id"])
        # 锁定恢复
        assert (m, ids[2]) in _locked(conn)
        # 历史批次快照正文逐字节未变
        assert json.dumps(get_batch(conn, b1["id"])["snapshot"], sort_keys=True) == b1_snapshot
        # 回滚批次中 r3 标记为锁定
        new_snap = get_batch(conn, res["rollback_batch"]["id"])["snapshot"]
        p = [x for x in new_snap["pairs"] if x["manuscript_id"] == m and x["reviewer_id"] == ids[2]]
        assert p and p[0]["locked"] is True
    finally:
        conn.close()


def test_rollback_preserves_out_of_scope_locks_in_both_directions() -> None:
    """涉及批次后新实体的范围外锁必须保留，且两个交叉方向都覆盖：
    （快照稿件, 新评审人）与（新稿件, 快照评审人）。"""
    conn = _connect()
    try:
        _reset(conn)
        # 两篇快照稿件 M1/M2（各需 1 名），两名快照评审 r1/r2
        m1 = _add_m(conn, "M1", req=1, leads=0, m_id=1)
        m2 = _add_m(conn, "M2", req=1, leads=0, m_id=2)
        _add_r(conn, "r1", cap=4, r_id=1)
        _add_r(conn, "r2", cap=4, r_id=2)
        conn.commit()
        solve(conn)
        b1 = create_batch(conn)

        # 批次之后新增两名评审 r5/r6，并在“快照稿件 × 新评审人”上锁 4 把
        r5 = _add_r(conn, "r5-新", cap=4, r_id=5)
        r6 = _add_r(conn, "r6-新", cap=4, r_id=6)
        conn.commit()
        out_locks = {(m1, r5), (m1, r6), (m2, r5), (m2, r6)}
        for pair in out_locks:
            conn.execute("INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1)", pair)
        conn.commit()

        # 这些锁都不在快照中，但其评审人不在快照范围 → 不构成锁定移除阻塞
        pv = preview_rollback(conn, b1["id"])
        assert pv["can_rollback"] is True
        assert not any(b["code"] == BLOCK_LOCK_REMOVE for b in pv["blockers"])
        rollback(conn, b1["id"])
        for pair in out_locks:
            assert pair in _locked(conn)

        # 另一交叉方向：（批次后新稿件, 快照评审人）的范围外锁同样保留
        _reset(conn)
        m1 = _add_m(conn, "M1", req=1, leads=0, m_id=1)
        _add_r(conn, "r1", cap=4, r_id=1)
        conn.commit()
        solve(conn)
        b2 = create_batch(conn)
        m_new = _add_m(conn, "M-new", req=1, leads=0, m_id=9)
        r_new = _add_r(conn, "r-new", cap=4, r_id=9)
        conn.commit()
        conn.execute("INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1)", (m1, r_new))
        conn.execute("INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1)", (m_new, 1))
        conn.commit()
        pv2 = preview_rollback(conn, b2["id"])
        assert pv2["can_rollback"] is True
        rollback(conn, b2["id"])
        assert (m1, r_new) in _locked(conn) and (m_new, 1) in _locked(conn)
    finally:
        conn.close()


def test_rollback_infeasible_free_pair_recomputed_without_reject() -> None:
    """批次后新评审人的锁定占用稿名额：历史自由配对少一个可恢复，不整单拒绝，
    由求解器保留可恢复部分并最小改动补齐（范围外锁定保留、不构成锁定阻塞）。"""
    conn = _connect()
    try:
        m, ids = _scenario(conn)  # M1 需求 2，批次中 2 个自由配对
        b1 = create_batch(conn)
        snap_pairs = {(p["manuscript_id"], p["reviewer_id"])
                      for p in get_batch(conn, b1["id"])["snapshot"]["pairs"]}
        assert len(snap_pairs) == 2

        # 批次之后：新建评审人 r5，并把其锁定到 M1（范围外锁：r5 不在快照中）
        r5 = _add_r(conn, "r5-后建", cap=2)
        conn.commit()
        conn.execute(
            "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1)", (m, r5))
        conn.commit()
        solve(conn)

        pv = preview_rollback(conn, b1["id"])
        assert pv["can_rollback"] is True
        # r5 的锁不构成移除阻塞（其不在快照范围内），但会占掉 M1 一个名额
        assert not any(b["code"] == BLOCK_LOCK_REMOVE for b in pv["blockers"])
        # 历史自由配对 2 个，名额只剩 1 个 → 恰好 1 个无法恢复
        assert len(pv["unrestored_free_pairs"]) == 1
        assert (m, r5) in _locked(conn)  # 预检只读，锁仍在

        res = rollback(conn, b1["id"])
        # 回滚不报错；范围外锁保留，M1 总分配仍为 2（1 锁 + 1 历史自由配对）
        assert (m, r5) in _locked(conn)
        final = _pairs(conn)
        m_pairs = {rr for (mm, rr) in final if mm == m}
        assert len(m_pairs) == 2 and r5 in m_pairs
        restored = (m_pairs - {r5}) & {rr for (mm, rr) in snap_pairs if mm == m}
        assert len(restored) == 1
        assert res["rollback_batch"]["kind"] == KIND_ROLLBACK
    finally:
        conn.close()


def test_rejected_rollback_changes_nothing() -> None:
    conn = _connect()
    try:
        m, ids = _scenario(conn)
        b1 = create_batch(conn)
        # 制造锁定阻塞并改动数据
        _assign(conn, m, ids[3], locked=1)
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m, ids[1]))
        conn.commit()
        solve(conn)
        pairs_before, locked_before = _pairs(conn), _locked(conn)
        conflicts_before, batches_before = _conflicts(conn), len(list_batches(conn))

        err = _expect_audit_error(rollback, 409, conn, b1["id"])
        assert err.blockers  # 带结构化原因
        assert _pairs(conn) == pairs_before
        assert _locked(conn) == locked_before
        assert _conflicts(conn) == conflicts_before
        assert len(list_batches(conn)) == batches_before
    finally:
        conn.close()


def test_batches_cleared_on_reset_and_demo() -> None:
    """重置/演示数据会清空批次（批次绑定实体 ID，重置后旧批次不可回滚）。"""
    try:
        from app.main import load_demo, reset_all
    except ModuleNotFoundError:
        print("  （跳过接口层测试：未安装 fastapi/pydantic）")
        return
    conn = _connect()
    try:
        _scenario(conn)
        create_batch(conn)
        assert len(list_batches(conn)) == 1
        st = reset_all(conn)
        assert st["batches"] == []
        st2 = load_demo(conn)
        assert st2["batches"] == []
    finally:
        conn.close()


def test_migration_creates_batches_table_on_old_db() -> None:
    """旧库（无 assignment_batches 表）启动迁移后幂等获得该表。"""
    import sqlite3
    path = os.path.join(tempfile.mkdtemp(prefix="review-audit-mig-"), "old.db")
    from app import db
    old_path = db.DB_PATH
    db.DB_PATH = path
    try:
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE reviewers (id INTEGER PRIMARY KEY, name TEXT, topics TEXT, "
                  "capacity INTEGER, institution TEXT, can_lead INTEGER)")
        c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE manuscripts (id INTEGER PRIMARY KEY, title TEXT, topics TEXT, "
                  "required_reviews INTEGER, required_leads INTEGER)")
        c.execute("CREATE TABLE conflicts (m_id INTEGER, r_id INTEGER)")
        c.execute("CREATE TABLE assignments (id INTEGER PRIMARY KEY, m_id INTEGER, r_id INTEGER, locked INTEGER)")
        c.commit()
        c.close()

        init_db(seed_demo=False)
        c2 = sqlite3.connect(path)
        tables = {r[0] for r in c2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "assignment_batches" in tables
        cols = {r[1] for r in c2.execute("PRAGMA table_info(assignment_batches)")}
        assert {"id", "created_at", "kind", "note", "snapshot", "params_hash",
                "rollback_of", "pair_count", "locked_count", "lead_count",
                "conflict_count"} <= cols
        c2.close()
    finally:
        db.DB_PATH = old_path


def test_api_layer() -> None:
    """接口层：创建/列表/详情/差异/预检 200 结构；回滚拒绝由异常处理器映射为 409。"""
    try:
        from app.main import (
            audit_error_handler, create_batch as api_create_batch,
            diff_batches as api_diff, get_batch as api_get,
            list_batches as api_list, rollback_batch, rollback_preview,
        )
        from app.schemas import BatchCreateIn, BatchRollbackIn
    except ModuleNotFoundError:
        print("  （跳过接口层测试：未安装 fastapi/pydantic）")
        return

    conn = _connect()
    try:
        m, ids = _scenario(conn)
        meta = api_create_batch(BatchCreateIn(note="接口批次"), conn)
        bid = meta["batch"]["id"]
        assert meta["batches"] is not None  # 返回完整状态含时间线
        assert any(b["id"] == bid for b in api_list(conn)["batches"])
        full = api_get(bid, conn)
        assert full["intact"] is True

        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m, ids[1]))
        conn.commit()
        solve(conn)
        meta2 = api_create_batch(BatchCreateIn(note="第二批"), conn)
        d = api_diff(bid, meta2["batch"]["id"], conn)
        assert d["conflicts"]["added"] == [[m, ids[1]]]

        # 删稿件后预检 200 且 can_rollback=False；执行 409，处理器带 blockers
        conn.execute("DELETE FROM manuscripts WHERE id=?", (m,))
        conn.commit()
        pv = rollback_preview(bid, conn)
        assert pv["can_rollback"] is False and pv["blockers"]
        try:
            rollback_batch(bid, BatchRollbackIn(note=""), conn)
            raise AssertionError("应当 409")
        except AuditError as err:
            resp = audit_error_handler(None, err)
            assert resp.status_code == 409
            assert err.blockers and "整单拒绝" in err.message
    finally:
        conn.close()


if __name__ == "__main__":
    test_snapshot_contents_and_hash()
    test_get_missing_batch_404()
    test_diff_batches_pair_conflict_params()
    test_diff_detects_lock_and_lead_status_change()
    test_diff_detects_entity_add_remove_change()
    test_preview_is_read_only()
    test_block_deleted_manuscript_and_reviewer()
    test_block_current_lock_removed_or_downgraded()
    test_block_target_lock_vs_current_conflict()
    test_rollback_restores_pairs_conflicts_params_and_attributes()
    test_rollback_keeps_entities_created_after_batch()
    test_rollback_lock_preserved_and_history_not_rewritten()
    test_rollback_preserves_out_of_scope_locks_in_both_directions()
    test_rollback_infeasible_free_pair_recomputed_without_reject()
    test_rejected_rollback_changes_nothing()
    test_batches_cleared_on_reset_and_demo()
    test_migration_creates_batches_table_on_old_db()
    test_api_layer()
    print("所有分配批次审计测试通过 ✅")
