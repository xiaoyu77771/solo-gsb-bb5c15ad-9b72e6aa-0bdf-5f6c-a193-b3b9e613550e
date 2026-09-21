"""分配批次审计测试（仅标准库，风格与 test_sandbox / test_backfill 一致）。

运行：python -m tests.test_audit

覆盖：
* 快照创建：要求已求解；快照内容（配对/主审/锁定/参数/备注）正确；
* 不可变：数据库触发器拒绝 UPDATE/DELETE；
* 批次差异：新增/移除配对、锁定变化、主审变化、参数变化、已删除对象标注；
* 原子回滚：成功恢复配对/锁定/参数并写入新批次（不改写历史）；
* 整单拒绝：快照对象已删除、批次之后新增当前锁定、锁定与当前冲突重合、
  锁定超容量/需求/机构上限——均 409、不写入、reasons 逐条说明；
* 非锁定配对因当前冲突未恢复时不阻断回滚并明确列出；
* 迁移：旧库启动幂等获得 assignment_batches 表与不可变触发器；
* reset/demo 不清除批次历史；
* 接口层（安装 fastapi 时）：创建/列表/详情/差异/回滚 409 reasons。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

# 测试前指定独立数据库
_tmp = tempfile.mkdtemp(prefix="review-audit-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app import audit  # noqa: E402
from app.db import _connect, get_setting, init_db, set_setting  # noqa: E402
from app.solver import solve  # noqa: E402


# ------------------------------------------------------------- 测试夹具
def _make_scenario(conn, org_cap: int = 0) -> None:
    """清空并构造：2 篇稿件（M1 需 2 含 1 主审，M2 需 1），4 名评审。

    R1 可任主审；主题全部相容；机构上限参数可由调用方设定。
    """
    conn.execute("DELETE FROM assignments")
    conn.execute("DELETE FROM conflicts")
    conn.execute("DELETE FROM conflict_declarations")
    conn.execute("DELETE FROM manuscripts")
    conn.execute("DELETE FROM reviewers")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
    conn.execute(
        "INSERT INTO manuscripts(title, topics, required_reviews, required_leads) "
        "VALUES (?, ?, ?, ?)", ("M1", json.dumps(["t"]), 2, 1))
    conn.execute(
        "INSERT INTO manuscripts(title, topics, required_reviews, required_leads) "
        "VALUES (?, ?, ?, ?)", ("M2", json.dumps(["t"]), 1, 0))
    for name, idx, can_lead in [("R1", 3, 1), ("R2", 3, 0), ("R3", 3, 0), ("R4", 1, 0)]:
        conn.execute(
            "INSERT INTO reviewers(name, topics, capacity, can_lead) VALUES (?, ?, ?, ?)",
            (name, json.dumps(["t"]), idx, can_lead))
    set_setting(conn, "mismatch_weight", 10)
    set_setting(conn, "load_weight", 2)
    set_setting(conn, "institution_cap", org_cap)
    conn.commit()


def _pairs(conn) -> set[tuple[int, int]]:
    return {(r["m_id"], r["r_id"]) for r in conn.execute("SELECT m_id, r_id FROM assignments")}


def _locked(conn) -> set[tuple[int, int]]:
    return {(r["m_id"], r["r_id"])
            for r in conn.execute("SELECT m_id, r_id FROM assignments WHERE locked = 1")}


def _expect_error(status: int, fn, *args, **kwargs) -> audit.AuditError:
    try:
        fn(*args, **kwargs)
    except audit.AuditError as e:
        assert e.status_code == status, (e.status_code, e.message)
        if status == 409:
            assert e.reasons  # 整单拒绝必须逐条说明原因
        return e
    raise AssertionError(f"应当返回 {status}")


# ------------------------------------------------------------- 快照
def test_snapshot_requires_solution() -> None:
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        err = _expect_error(400, audit.create_snapshot, conn, "无解快照")
        assert "尚未成功求解" in err.message
        assert audit.list_batches(conn) == []
    finally:
        conn.close()


def test_snapshot_content() -> None:
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        sol = solve(conn)
        lead_pairs = {tuple(p) for p in sol["lead_pairs"]}
        b = audit.create_snapshot(conn, "第一轮")
        assert b["kind"] == "snapshot" and b["rolled_back_from"] is None
        assert b["note"] == "第一轮"
        assert b["pair_count"] == 3 and b["locked_count"] == 0
        assert b["lead_count"] == len(lead_pairs) == 1
        assert b["params"] == {"mismatch_weight": 10, "load_weight": 2, "institution_cap": 0}
        assert len(b["content_hash"]) == 64

        # 详情含全部配对及主审/锁定标记；名称已解析
        detail = audit.get_batch(conn, b["id"])
        assert len(detail["pairs"]) == 3
        p_lead = [p for p in detail["pairs"] if p["is_lead"]]
        assert len(p_lead) == 1 and p_lead[0]["reviewer_id"] == 1
        assert all(not p["locked"] for p in detail["pairs"])
        assert all(p["manuscript_title"] in ("M1", "M2") for p in detail["pairs"])
        assert {p["reviewer_name"] for p in detail["pairs"]} == {"R1", "R2", "R3"}

        # 锁定后再快照：锁定数与主审抵扣进入快照
        conn.execute("UPDATE assignments SET locked = 1 WHERE m_id = 1 AND r_id = 1")
        conn.commit()
        solve(conn)
        b2 = audit.create_snapshot(conn, "锁定 R1")
        assert b2["locked_count"] == 1
        assert (1, 1) in _locked(conn)
    finally:
        conn.close()


def test_snapshot_immutable_via_triggers() -> None:
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        solve(conn)
        b = audit.create_snapshot(conn, "x")
        for stmt, args in [
            ("UPDATE assignment_batches SET note = ? WHERE id = ?", ("改", b["id"])),
            ("DELETE FROM assignment_batches WHERE id = ?", (b["id"],)),
        ]:
            try:
                conn.execute(stmt, args)
                conn.commit()
            except sqlite3.Error as e:
                assert "不可变审计表" in str(e)
                conn.rollback()
            else:
                raise AssertionError("审计表必须拒绝改写/删除")
        # 记录仍在且内容不变
        again = audit.get_batch(conn, b["id"])
        assert again["note"] == "x"
    finally:
        conn.close()


# ------------------------------------------------------------- 差异
def test_batch_diff() -> None:
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        solve(conn)
        b1 = audit.create_snapshot(conn, "A")

        # 调整参数并锁定一对后重解，制造参数/锁定/主审变化
        set_setting(conn, "mismatch_weight", 1)
        m2_r = conn.execute("SELECT r_id FROM assignments WHERE m_id = 2").fetchone()["r_id"]
        conn.execute("UPDATE assignments SET locked = 1 WHERE m_id = 2 AND r_id = ?", (m2_r,))
        conn.commit()
        solve(conn)
        b2 = audit.create_snapshot(conn, "B")

        d = audit.batch_diff(conn, b1["id"], b2["id"])
        assert d["from_batch"]["id"] == b1["id"] and d["to_batch"]["id"] == b2["id"]
        # M2 的分配转为锁定：锁定变化至少 1
        assert any((x["manuscript_id"], x["reviewer_id"]) == (2, m2_r) and x["locked_after"]
                   for x in d["lock_changed"])
        assert any(c["param"] == "mismatch_weight" and c["before"] == 10 and c["after"] == 1
                   for c in d["param_changes"])

        # 新增/移除：删掉 R4（容量 1、此前未被安排则无差）；改为删除一名在配评审后重解
        conn.execute("DELETE FROM reviewers WHERE id = 3")
        conn.commit()
        solve(conn)
        b3 = audit.create_snapshot(conn, "C")
        d2 = audit.batch_diff(conn, b2["id"], b3["id"])
        # 旧批次中 R3 的配对在 b3 中消失
        assert any(x["reviewer_id"] == 3 for x in d2["removed"])
        # 差异中对已删除评审人标注 deleted
        rm3 = [x for x in d2["removed"] if x["reviewer_id"] == 3][0]
        assert rm3["reviewer_deleted"] is True
    finally:
        conn.close()


def test_diff_missing_batch_404() -> None:
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        solve(conn)
        b = audit.create_snapshot(conn, "x")
        _expect_error(404, audit.batch_diff, conn, b["id"], 9999)
    finally:
        conn.close()


# ------------------------------------------------------------- 回滚：成功
def test_rollback_restores_and_appends() -> None:
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        solve(conn)
        b1 = audit.create_snapshot(conn, "基线")
        base_pairs = set(_pairs(conn))
        base_params = (10, 2, 0)

        # 之后：改参数、锁定、加新评审人，方案随之变化
        set_setting(conn, "mismatch_weight", 1)
        set_setting(conn, "institution_cap", 0)
        conn.execute("INSERT INTO reviewers(name, topics, capacity) VALUES ('R5', ?, 3)",
                     (json.dumps(["t"]),))
        conn.commit()
        solve(conn)
        assert _pairs(conn) != base_pairs or get_setting(conn, "mismatch_weight") == 1
        b2 = audit.create_snapshot(conn, "变更后")

        result = audit.rollback(conn, b1["id"])
        nb = result["batch"]
        # 新批次：回滚类型、指向源批次、历史不改写
        assert nb["kind"] == "rollback" and nb["rolled_back_from"] == b1["id"]
        assert nb["id"] > b2["id"]
        old = audit.get_batch(conn, b1["id"])
        assert old["note"] == "基线" and old["params"] == {
            "mismatch_weight": 10, "load_weight": 2, "institution_cap": 0}

        # 当前数据已恢复
        assert _pairs(conn) == base_pairs
        assert (int(get_setting(conn, "mismatch_weight")),
                int(get_setting(conn, "load_weight")),
                int(get_setting(conn, "institution_cap"))) == base_params
        assert result["skipped_conflict_pairs"] == []
        # last_solution 已按恢复参数重解
        sol = get_setting(conn, "last_solution")
        assert sol["mismatch_weight"] == 10
        # 自动备注注明来源
        assert f"#{b1['id']}" in nb["note"]

        # 时间线最新三条：回滚批次在前，历史快照均保留不改写
        ids = [(x["id"], x["kind"]) for x in audit.list_batches(conn)][:3]
        assert ids == [(nb["id"], "rollback"), (b2["id"], "snapshot"), (b1["id"], "snapshot")]
    finally:
        conn.close()


def test_rollback_restores_locks_and_leads() -> None:
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        solve(conn)
        conn.execute("UPDATE assignments SET locked = 1 WHERE m_id = 1 AND r_id = 1")
        conn.commit()
        sol = solve(conn)
        lead_in_batch = {tuple(p) for p in sol["lead_pairs"]}
        b = audit.create_snapshot(conn, "带锁基线")

        # 解锁并求解
        conn.execute("UPDATE assignments SET locked = 0 WHERE m_id = 1 AND r_id = 1")
        conn.commit()
        solve(conn)
        assert (1, 1) not in _locked(conn)

        result = audit.rollback(conn, b["id"])
        assert (1, 1) in _locked(conn)
        # 主审标记在回滚批次中恢复
        pairs = result["batch"]["pairs"]
        assert [p for p in pairs if (p["manuscript_id"], p["reviewer_id"]) == (1, 1)][0]["locked"]
        new_sol = get_setting(conn, "last_solution")
        assert lead_in_batch == {tuple(p) for p in new_sol["lead_pairs"]}
    finally:
        conn.close()


def test_rollback_conflicted_free_pair_skipped_not_blocked() -> None:
    """批次中非锁定配对在当前冲突矩阵中时：不阻断回滚，重解自然不建该边，明确列出。"""
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        solve(conn)
        b = audit.create_snapshot(conn, "基线")
        # 取一条非锁定配对补登冲突
        m_id, r_id = list(_pairs(conn))[0]
        conn.execute("INSERT OR IGNORE INTO conflicts(m_id, r_id) VALUES (?, ?)", (m_id, r_id))
        conn.commit()
        result = audit.rollback(conn, b["id"])
        assert [m_id, r_id] in result["skipped_conflict_pairs"]
        assert (m_id, r_id) not in _pairs(conn)
        # 回滚批次仍成功写入
        assert result["batch"]["kind"] == "rollback"
    finally:
        conn.close()


# ------------------------------------------------------------- 回滚：整单拒绝
def test_rollback_rejects_deleted_entity() -> None:
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        solve(conn)
        b = audit.create_snapshot(conn, "基线")

        before_pairs = set(_pairs(conn))
        conn.execute("DELETE FROM reviewers WHERE id = 2")
        conn.commit()
        err = _expect_error(409, audit.rollback, conn, b["id"])
        assert any("评审人 #2 已被删除" in r for r in err.reasons)
        # 整单不写入
        # 拒绝后不得产生回滚批次
        assert not any(x["kind"] == "rollback" and x["rolled_back_from"] == b["id"]
                       for x in audit.list_batches(conn))
        assert get_setting(conn, "last_solution") is not None
        # 现存数据完全未被改动（删除 R2 的级联效果是删除前的状态，回滚未再动）
        assert _pairs(conn) == before_pairs - {(m, 2) for m in (1, 2)}
    finally:
        conn.close()


def test_rollback_rejects_extra_current_lock() -> None:
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        solve(conn)
        b = audit.create_snapshot(conn, "基线")
        params_before = get_setting(conn, "mismatch_weight")

        # 情形一：快照中存在但当时未锁定的配对，批次之后被锁上
        m2_r = conn.execute("SELECT r_id FROM assignments WHERE m_id = 2").fetchone()["r_id"]
        conn.execute("UPDATE assignments SET locked = 1 WHERE m_id = 2 AND r_id = ?", (m2_r,))
        conn.commit()
        solve(conn)
        err = _expect_error(409, audit.rollback, conn, b["id"])
        assert any("当时未锁定" in r and "解除其当前锁定" in r for r in err.reasons)
        # 未写入：锁仍在、无新批次
        assert (2, m2_r) in _locked(conn)
        assert not any(x["kind"] == "rollback" and x["rolled_back_from"] == b["id"]
                       for x in audit.list_batches(conn))
        assert get_setting(conn, "mismatch_weight") == params_before

        # 情形二：批次之后新增的锁定配对（快照中根本不存在）
        conn.execute("UPDATE assignments SET locked = 0")
        conn.commit()
        solve(conn)
        m_new = conn.execute(
            "INSERT INTO manuscripts(title, topics, required_reviews) VALUES (?, ?, 1)",
            ("M3", json.dumps(["t"]))).lastrowid
        conn.commit()
        conn.execute(
            "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1)", (m_new, m2_r))
        conn.commit()
        solve(conn)
        err = _expect_error(409, audit.rollback, conn, b["id"])
        assert any("不在批次中（批次之后新增的锁定）" in r for r in err.reasons)
    finally:
        conn.close()


def test_rollback_rejects_lock_now_conflicted() -> None:
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        solve(conn)
        # 构造带锁定的批次
        conn.execute("UPDATE assignments SET locked = 1 WHERE m_id = 1 AND r_id = 1")
        conn.commit()
        solve(conn)
        b = audit.create_snapshot(conn, "带锁")
        # 解锁后补登冲突，再回滚（锁仍在快照中）
        conn.execute("UPDATE assignments SET locked = 0 WHERE m_id = 1 AND r_id = 1")
        conn.execute("INSERT OR IGNORE INTO conflicts(m_id, r_id) VALUES (1, 1)")
        conn.commit()
        solve(conn)
        err = _expect_error(409, audit.rollback, conn, b["id"])
        assert any("作者冲突重合" in r for r in err.reasons)
        # 拒绝后不得产生回滚批次
        assert not any(x["kind"] == "rollback" and x["rolled_back_from"] == b["id"]
                       for x in audit.list_batches(conn))
    finally:
        conn.close()


def test_rollback_rejects_lock_over_capacity() -> None:
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        solve(conn)
        # R4 容量 1，把 M2 的既有分配换成 R4 并锁定（锁定占用名额后求解不会覆盖）
        m2_r = conn.execute("SELECT r_id FROM assignments WHERE m_id = 2").fetchone()["r_id"]
        conn.execute("UPDATE assignments SET r_id = 4, locked = 1 WHERE m_id = 2 AND r_id = ?",
                     (m2_r,))
        conn.commit()
        solve(conn)
        assert (2, 4) in _locked(conn)
        b = audit.create_snapshot(conn, "R4 锁定")

        # 之后把 R4 容量调为 0 并解锁
        conn.execute("UPDATE reviewers SET capacity = 0 WHERE id = 4")
        conn.execute("UPDATE assignments SET locked = 0 WHERE m_id = 2 AND r_id = 4")
        conn.commit()
        solve(conn)
        err = _expect_error(409, audit.rollback, conn, b["id"])
        assert any("R4" in r and "个人容量 0" in r for r in err.reasons)
    finally:
        conn.close()


def test_rollback_rejects_lock_over_required() -> None:
    conn = _connect()
    try:
        _make_scenario(conn)
        # 构造“M2 需求 2、同稿锁定 2 人”的批次（当前合法）
        conn.execute("UPDATE manuscripts SET required_reviews = 2 WHERE id = 2")
        conn.commit()
        solve(conn)
        m2_rows = conn.execute("SELECT r_id FROM assignments WHERE m_id = 2").fetchall()
        assert len(m2_rows) == 2
        conn.execute("UPDATE assignments SET locked = 1 WHERE m_id = 2")
        conn.commit()
        solve(conn)
        assert sum(1 for _ in conn.execute(
            "SELECT 1 FROM assignments WHERE m_id = 2 AND locked = 1")) == 2
        b = audit.create_snapshot(conn, "M2 两锁")

        # 之后把 M2 需求降回 1 并解锁
        conn.execute("UPDATE manuscripts SET required_reviews = 1 WHERE id = 2")
        conn.execute("UPDATE assignments SET locked = 0 WHERE m_id = 2")
        conn.commit()
        solve(conn)
        err = _expect_error(409, audit.rollback, conn, b["id"])
        assert any("所需评审数 1" in r for r in err.reasons)
    finally:
        conn.close()


def test_rollback_rejects_institution_over_cap() -> None:
    """批次之后评审人机构被修改，导致恢复锁定后同机构锁定数超批次参数上限 → 拒绝。

    注意：机构上限本身是快照求解参数的一部分，回滚会一并恢复，单纯“之后调严
    上限”不会冲突；这里制造的是上限（随快照恢复为 2）与当前机构归属之间的冲突。
    """
    conn = _connect()
    try:
        _make_scenario(conn, org_cap=2)
        # M2 将有 3 把锁；先扩容并求解，按求解结果给三人分别设置不同机构
        conn.execute("UPDATE manuscripts SET required_reviews = 3 WHERE id = 2")
        conn.commit()
        solve(conn)
        m2_rs = [r["r_id"] for r in conn.execute(
            "SELECT r_id FROM assignments WHERE m_id = 2 ORDER BY r_id")]
        assert len(m2_rs) == 3
        for idx, r_id in enumerate(m2_rs):
            conn.execute("UPDATE reviewers SET institution = ? WHERE id = ?",
                         (chr(ord("A") + idx), r_id))
        # M1 的主审（R1）若恰在 M2 锁名单中，也必须有机构才会参与同机构计数
        conn.execute("UPDATE reviewers SET institution = COALESCE(NULLIF(institution, ''), 'A')")
        conn.commit()
        solve(conn)
        conn.execute("UPDATE assignments SET locked = 1 WHERE m_id = 2")
        conn.commit()
        solve(conn)
        b = audit.create_snapshot(conn, "异机构三锁")

        # 批次之后：把其中两人的机构改成与第一人相同（同为 A），并解锁
        conn.execute("UPDATE assignments SET locked = 0 WHERE m_id = 2")
        conn.execute("UPDATE reviewers SET institution = 'A' WHERE id IN (?, ?)",
                     (m2_rs[1], m2_rs[2]))
        conn.commit()
        solve(conn)
        err = _expect_error(409, audit.rollback, conn, b["id"])
        assert any("「A」" in r and "最多 2 人" in r for r in err.reasons)
    finally:
        conn.close()


def test_rollback_atomic_no_partial_write() -> None:
    """拒绝后：参数、配对、锁定均保持回滚前状态。"""
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _make_scenario(conn)
        solve(conn)
        b = audit.create_snapshot(conn, "基线")
        conn.execute("UPDATE assignments SET locked = 1 WHERE m_id = 2 AND r_id = 3")
        set_setting(conn, "mismatch_weight", 7)
        conn.commit()
        solve(conn)
        before = {
            "pairs": set(_pairs(conn)), "locked": set(_locked(conn)),
            "beta": get_setting(conn, "mismatch_weight"),
        }
        _expect_error(409, audit.rollback, conn, b["id"])
        assert set(_pairs(conn)) == before["pairs"]
        assert set(_locked(conn)) == before["locked"]
        assert get_setting(conn, "mismatch_weight") == before["beta"]
    finally:
        conn.close()


def test_reset_and_demo_keep_batch_history() -> None:
    """批次审计历史在 reset / demo 后仍保留（只增审计表）。"""
    init_db(seed_demo=True)
    conn = _connect()
    try:
        solve(conn)
        b = audit.create_snapshot(conn, "保留我")
        conn.execute("DELETE FROM assignments")
        conn.execute("DELETE FROM conflicts")
        conn.execute("DELETE FROM conflict_declarations")
        conn.execute("DELETE FROM manuscripts")
        conn.execute("DELETE FROM reviewers")
        set_setting(conn, "last_solution", None)
        conn.commit()
        # 批次仍在
        assert audit.get_batch(conn, b["id"])["note"] == "保留我"
    finally:
        conn.close()


# ------------------------------------------------------------- 迁移
def test_migration_creates_batches_table() -> None:
    path = os.path.join(tempfile.mkdtemp(prefix="review-mig-audit-"), "old.db")
    from app import db
    old_path = db.DB_PATH
    db.DB_PATH = path
    try:
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE reviewers (id INTEGER PRIMARY KEY, name TEXT, topics TEXT,"
                  " capacity INTEGER, institution TEXT, can_lead INTEGER)")
        c.execute("INSERT INTO reviewers(name, topics, capacity, institution, can_lead)"
                  " VALUES ('老评审','[]',3,'',0)")
        c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE manuscripts (id INTEGER PRIMARY KEY, title TEXT, topics TEXT,"
                  " required_reviews INTEGER, required_leads INTEGER)")
        c.execute("INSERT INTO manuscripts(title, topics, required_reviews, required_leads)"
                  " VALUES ('老稿','[]',3,0)")
        c.execute("CREATE TABLE conflicts (m_id INTEGER, r_id INTEGER)")
        c.execute("CREATE TABLE assignments (id INTEGER PRIMARY KEY, m_id INTEGER,"
                  " r_id INTEGER, locked INTEGER)")
        c.execute("CREATE TABLE conflict_declarations ("
                  "id INTEGER PRIMARY KEY, reviewer_id INTEGER, token_hash TEXT,"
                  " manifest TEXT, manifest_hash TEXT, status TEXT,"
                  " submitted_conflicts TEXT, revoked_reason TEXT,"
                  " created_at TEXT, submitted_at TEXT, revoked_at TEXT)")
        c.commit()
        c.close()

        init_db(seed_demo=False)
        c2 = sqlite3.connect(path)
        tabs = {r[0] for r in c2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "assignment_batches" in tabs
        trgs = {r[0] for r in c2.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert "trg_assignment_batches_no_update" in trgs
        assert "trg_assignment_batches_no_delete" in trgs
        c2.close()

        conn = _connect()
        try:
            solve(conn)
            b = audit.create_snapshot(conn, "迁移库快照")
            assert b["pair_count"] >= 0
        finally:
            conn.close()
    finally:
        db.DB_PATH = old_path


# ------------------------------------------------------------- 接口层
def test_api_layer() -> None:
    """安装 fastapi/pydantic 时验证路由；环境缺失则跳过（与其他测试模块一致）。"""
    try:
        from fastapi.testclient import TestClient
    except Exception:
        print("  （跳过接口层测试：未安装 fastapi/pydantic）")
        return

    from app.main import app
    init_db(seed_demo=True)
    client = TestClient(app)
    r = client.get("/api/state")
    assert r.status_code == 200 and "batches" in r.json()

    r = client.post("/api/batches", json={"note": "接口批次"})
    assert r.status_code == 200
    bid = r.json()["batch"]["id"]
    assert r.json()["batch"]["note"] == "接口批次"

    r = client.get("/api/batches")
    assert any(x["id"] == bid for x in r.json()["batches"])

    r = client.get(f"/api/batches/{bid}")
    assert r.status_code == 200 and len(r.json()["pairs"]) >= 0

    r = client.get(f"/api/batches/{bid}/diff/{bid}")
    assert r.status_code == 200 and r.json()["summary"]["added"] == 0

    r = client.post(f"/api/batches/{bid}/rollback")
    assert r.status_code == 200
    assert r.json()["batch"]["rolled_back_from"] == bid

    r = client.get("/api/batches/99999")
    assert r.status_code == 404

    # 制造删除对象后回滚 → 409 + reasons
    target = client.post("/api/batches", json={"note": "将被删稿"}).json()["batch"]
    ms = client.get("/api/state").json()["manuscripts"]
    client.delete(f"/api/manuscripts/{ms[0]['id']}")
    r = client.post(f"/api/batches/{target['id']}/rollback")
    assert r.status_code == 409
    assert r.json()["reasons"]


if __name__ == "__main__":
    test_snapshot_requires_solution()
    test_snapshot_content()
    test_snapshot_immutable_via_triggers()
    test_batch_diff()
    test_diff_missing_batch_404()
    test_rollback_restores_and_appends()
    test_rollback_restores_locks_and_leads()
    test_rollback_conflicted_free_pair_skipped_not_blocked()
    test_rollback_rejects_deleted_entity()
    test_rollback_rejects_extra_current_lock()
    test_rollback_rejects_lock_now_conflicted()
    test_rollback_rejects_lock_over_capacity()
    test_rollback_rejects_lock_over_required()
    test_rollback_rejects_institution_over_cap()
    test_rollback_atomic_no_partial_write()
    test_reset_and_demo_keep_batch_history()
    test_migration_creates_batches_table()
    test_api_layer()
    print("所有分配批次审计测试通过 ✅")
