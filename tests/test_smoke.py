"""最小冒烟测试：验证求解器核心约束与前端服务可用性。

运行：python -m tests.test_smoke
（仅使用标准库，不依赖 pytest）
"""
from __future__ import annotations

import json
import os
import tempfile

# 测试前指定独立数据库
_tmp = tempfile.mkdtemp(prefix="review-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app.db import _connect, init_db, set_setting  # noqa: E402
from app.mcmf import MinCostMaxFlow  # noqa: E402
from app.solver import institution_lock_exceeds, solve  # noqa: E402


def test_mcmf_known_case() -> None:
    net = MinCostMaxFlow(6)
    net.add_edge(0, 1, 2, 0)
    net.add_edge(0, 2, 2, 0)
    net.add_edge(1, 3, 1, 1)
    net.add_edge(1, 4, 2, 3)
    net.add_edge(2, 3, 2, 1)
    net.add_edge(3, 5, 2, 2)
    net.add_edge(4, 5, 2, 1)
    flow, cost = net.solve(0, 5)
    assert (flow, cost) == (4, 14)


def test_demo_solve_constraints() -> None:
    init_db(seed_demo=True)
    conn = _connect()
    try:
        sol = solve(conn)
        # 演示数据刻意制造 1 篇未满足
        assert not sol["fully_satisfied"]
        assert len(sol["unmet"]) == 1

        # 冲突边绝不出现在分配中
        conflicts = {
            (r["m_id"], r["r_id"])
            for r in conn.execute("SELECT m_id, r_id FROM conflicts")
        }
        used = {
            (r["m_id"], r["r_id"])
            for r in conn.execute("SELECT m_id, r_id FROM assignments")
        }
        assert not (conflicts & used)

        # 每稿不超需求、每人不超容量（无锁定时）
        req = {r["id"]: r["required_reviews"]
               for r in conn.execute("SELECT id, required_reviews FROM manuscripts")}
        cap = {r["id"]: r["capacity"]
               for r in conn.execute("SELECT id, capacity FROM reviewers")}
        per_m: dict[int, int] = {}
        per_r: dict[int, int] = {}
        for m_id, r_id in used:
            per_m[m_id] = per_m.get(m_id, 0) + 1
            per_r[r_id] = per_r.get(r_id, 0) + 1
        assert all(per_m.get(m, 0) <= n for m, n in req.items())
        assert all(per_r.get(r, 0) <= n for r, n in cap.items())

        # 演示数据开启机构上限（每稿同机构最多 2 人）：所有稿件均不得超限
        assert sol["institution_cap"] == 2
        inst = {r["id"]: r["institution"]
                for r in conn.execute("SELECT id, institution FROM reviewers")}
        per_m_inst: dict[tuple[int, str], int] = {}
        for m_id, r_id in used:
            if inst[r_id]:
                key = (m_id, inst[r_id])
                per_m_inst[key] = per_m_inst.get(key, 0) + 1
        assert all(n <= 2 for n in per_m_inst.values())

        # 每篇稿件都返回机构构成（内存解以 int 为键，JSON 持久化后为字符串键）
        for m_id in req:
            groups = sol["institutions"].get(m_id) or sol["institutions"].get(str(m_id))
            assert groups is not None
            assert sum(g["count"] for g in groups) == per_m.get(m_id, 0)
    finally:
        conn.close()


def test_locks_occupy_capacity() -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE assignments SET locked=1 WHERE m_id=1 AND r_id=2")
        conn.commit()
        sol = solve(conn)
        assert sol["locked_count"] == 1
        # 锁定预占被锁评审的一个容量槽位：其（锁定+自由）总数不超过容量，
        # 且锁定行确实保留
        cap_r2 = conn.execute(
            "SELECT capacity FROM reviewers WHERE id=2").fetchone()["capacity"]
        total_r2 = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments WHERE r_id=2").fetchone()["c"]
        locked_r2 = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments WHERE r_id=2 AND locked=1"
        ).fetchone()["c"]
        assert locked_r2 == 1
        assert total_r2 <= cap_r2
        # 锁定先占机构名额：该稿同机构（清华大学）最多还能再分配 1 人
        same_inst_free = conn.execute(
            """
            SELECT COUNT(*) AS c FROM assignments a
            JOIN reviewers rv ON a.r_id = rv.id
            JOIN manuscripts mv ON a.m_id = mv.id
            WHERE a.m_id = 1 AND rv.institution =
                  (SELECT institution FROM reviewers WHERE id = 2)
            """).fetchone()["c"]
        assert same_inst_free == 2  # 1 锁定 + 1 自由，恰好顶到机构上限
        conn.execute("DELETE FROM assignments WHERE locked=1")
        conn.commit()
    finally:
        conn.close()


def _make_scenario(conn, cap: int) -> None:
    """清空并构造：1 篇需求 3 的稿件；A 机构 3 人、B 机构 1 人、无机构 1 人。"""
    conn.execute("DELETE FROM assignments")
    conn.execute("DELETE FROM conflicts")
    conn.execute("DELETE FROM manuscripts")
    conn.execute("DELETE FROM reviewers")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
    conn.execute(
        "INSERT INTO manuscripts(title, topics, required_reviews) VALUES (?, ?, ?)",
        ("测试稿", json.dumps(["t"], ensure_ascii=False), 3))
    for name, inst in [("a1", "A"), ("a2", "A"), ("a3", "A"), ("b1", "B"), ("x1", "")]:
        conn.execute(
            "INSERT INTO reviewers(name, topics, capacity, institution) VALUES (?, ?, ?, ?)",
            (name, json.dumps(["t"], ensure_ascii=False), 3, inst))
    set_setting(conn, "institution_cap", cap)
    conn.commit()


def test_institution_cap_hard_limit() -> None:
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        sol = solve(conn)
        # cap=1：A 机构至多 1 人；需求 3 由 A、B、无机构各 1 人满足
        per_inst: dict[str, int] = {}
        for r in conn.execute(
                "SELECT r_id FROM assignments WHERE m_id=1"):
            row = conn.execute(
                "SELECT institution FROM reviewers WHERE id=?", (r["r_id"],)).fetchone()
            per_inst[row["institution"]] = per_inst.get(row["institution"], 0) + 1
        assert per_inst.get("A", 0) == 1
        assert per_inst.get("B", 0) == 1
        assert per_inst.get("", 0) == 1
        assert sol["fully_satisfied"]

        # 关闭机构上限（0）：同机构不再受限
        _make_scenario(conn, 0)
        sol = solve(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM assignments WHERE m_id=1").fetchone()["c"]
        assert n == 3
        assert sol["institution_cap"] == 0
        assert all(not g.get("over_cap") for groups in sol["institutions"].values()
                   for g in groups)
    finally:
        conn.close()


def test_institution_lock_over_cap_warns_and_blocks() -> None:
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        # 历史锁定同一机构 2 人（绕过 API 直接构造“存量数据”）
        conn.execute(
            "INSERT INTO assignments(m_id, r_id, locked) "
            "SELECT 1, id, 1 FROM reviewers WHERE name IN ('a1','a2')")
        conn.commit()
        sol = solve(conn)
        # 锁定强制保留（2 名 A 仍在），机构超限告警，且不再向 A 机构分配新人
        locked_a = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments a JOIN reviewers rv ON a.r_id=rv.id "
            "WHERE a.m_id=1 AND rv.institution='A'").fetchone()["c"]
        assert locked_a == 2
        w = [x for x in sol["warnings"] if x["code"] == "institution_lock_over_cap"]
        assert len(w) == 1 and w[0]["institution"] == "A"
        groups_map = sol["institutions"].get(1) or sol["institutions"]["1"]
        groups = {g["institution"]: g for g in groups_map}
        assert groups["A"]["over_cap"] is True
        # 第 3 个名额只能给 B 或无机构，绝不会再出现第 3 个 A
        per_inst: dict[str, int] = {}
        for r in conn.execute("SELECT r_id FROM assignments WHERE m_id=1"):
            inst = conn.execute(
                "SELECT institution FROM reviewers WHERE id=?", (r["r_id"],)).fetchone()["institution"]
            per_inst[inst] = per_inst.get(inst, 0) + 1
        assert per_inst["A"] == 2

        # 新增一把同机构的锁：判定函数为真（API 层据此返回 409）
        assert institution_lock_exceeds(1, same_inst_locked=1, is_new_lock=True) is True
        # 仅转换已有行为锁、上限关闭或已满额不增员时均不拒绝
        assert institution_lock_exceeds(1, same_inst_locked=1, is_new_lock=False) is False
        assert institution_lock_exceeds(0, same_inst_locked=5, is_new_lock=True) is False
        assert institution_lock_exceeds(2, same_inst_locked=1, is_new_lock=True) is False
    finally:
        conn.close()


def test_no_institution_reviewers_uncounted() -> None:
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        # B 机构锁 1 人已满；无机构评审不占名额，A 仍可再进 1 人
        conn.execute(
            "INSERT INTO assignments(m_id, r_id, locked) "
            "SELECT 1, id, 1 FROM reviewers WHERE name='b1'")
        conn.commit()
        sol = solve(conn)
        assert not any(w["code"] == "institution_lock_over_cap" for w in sol["warnings"])
        names = {
            r["name"] for r in conn.execute("""
                SELECT rv.name AS name FROM assignments a
                JOIN reviewers rv ON a.r_id = rv.id WHERE a.m_id=1""")
        }
        assert "b1" in names and "x1" in names
        assert sum(n.startswith("a") for n in names) == 1
    finally:
        conn.close()


def test_schema_migration_adds_institution() -> None:
    """旧库（reviewers 无 institution 列）启动迁移后自动补列且数据保留。"""
    path = os.path.join(tempfile.mkdtemp(prefix="review-mig-"), "old.db")
    from app import db
    old_path = db.DB_PATH
    db.DB_PATH = path
    try:
        import sqlite3
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE reviewers (id INTEGER PRIMARY KEY, name TEXT, topics TEXT, capacity INTEGER)")
        c.execute("INSERT INTO reviewers(name, topics, capacity) VALUES ('老评审','[]',3)")
        c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE manuscripts (id INTEGER PRIMARY KEY, title TEXT, topics TEXT, required_reviews INTEGER)")
        c.execute("INSERT INTO manuscripts(title, topics, required_reviews) VALUES ('老稿件','[]',3)")
        c.execute("CREATE TABLE conflicts (m_id INTEGER, r_id INTEGER)")
        c.execute("CREATE TABLE assignments (id INTEGER PRIMARY KEY, m_id INTEGER, r_id INTEGER, locked INTEGER)")
        c.commit()
        c.close()

        init_db(seed_demo=False)
        c2 = sqlite3.connect(path)
        c2.row_factory = sqlite3.Row
        row = c2.execute("SELECT name, institution, can_lead FROM reviewers WHERE name='老评审'").fetchone()
        assert row["institution"] == ""  # 既有数据保留，机构默认未填写
        assert row["can_lead"] == 0      # 主审资格默认关闭（不可任主审）
        mrow = c2.execute("SELECT required_leads FROM manuscripts WHERE title='老稿件'").fetchone()
        assert mrow["required_leads"] == 0  # 需主审人数默认 0：主审约束默认关闭
        assert c2.execute("SELECT value FROM settings WHERE key='institution_cap'").fetchone()["value"] == "0"
        c2.close()
    finally:
        db.DB_PATH = old_path


def _make_lead_scenario(conn, required: int, leads: int, reviewers: list) -> None:
    """清空并构造：1 篇稿件（需求 required、需主审 leads）；
    reviewers = [(name, capacity, institution, can_lead)]，ID 从 1 递增。"""
    conn.execute("DELETE FROM assignments")
    conn.execute("DELETE FROM conflicts")
    conn.execute("DELETE FROM manuscripts")
    conn.execute("DELETE FROM reviewers")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
    conn.execute(
        "INSERT INTO manuscripts(title, topics, required_reviews, required_leads) VALUES (?, ?, ?, ?)",
        ("主审测试稿", json.dumps(["t"], ensure_ascii=False), required, leads))
    for name, cap, inst, cl in reviewers:
        conn.execute(
            "INSERT INTO reviewers(name, topics, capacity, institution, can_lead) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, json.dumps(["t"], ensure_ascii=False), cap, inst, cl))
    set_setting(conn, "institution_cap", 0)
    set_setting(conn, "mismatch_weight", 10)
    set_setting(conn, "load_weight", 2)
    conn.commit()


def _lead_pairs_of(sol: dict, m_id: int = 1) -> set[int]:
    return {r for (m, r) in sol["lead_pairs"] if m == m_id}


def test_lead_assigned_only_qualified() -> None:
    """主审资格硬约束：非合资格者绝不认定为主审；主审计入人数与容量。"""
    conn = _connect()
    try:
        _make_lead_scenario(conn, 2, 1, [("x1", 2, "", 0), ("x2", 2, "", 1)])
        sol = solve(conn)
        assert sol["fully_satisfied"]
        assert _lead_pairs_of(sol) == {2}          # 主审只能是合资格的 x2
        assert sol["total_lead_assigned"] == 1 and sol["total_lead_required"] == 1
        n = conn.execute("SELECT COUNT(*) AS c FROM assignments WHERE m_id=1").fetchone()["c"]
        assert n == 2                               # 主审同时占用稿件需求（总人数含主审）
        loads = {r["r_id"]: 0 for r in conn.execute("SELECT r_id FROM assignments")}
        for r in conn.execute("SELECT r_id FROM assignments"):
            loads[r["r_id"]] = loads.get(r["r_id"], 0) + 1
        assert loads[2] == 1                        # 主审占用个人容量
    finally:
        conn.close()


def test_lead_lock_deduction_by_reviewer_id_order() -> None:
    """合资格的有效锁定按评审人 ID 升序优先抵扣主审名额，不足由自由分配补齐。"""
    conn = _connect()
    try:
        # 需 1 主审；锁定两名合资格者（ID 1 与 3）→ ID 较小的 #1 抵扣为主审
        _make_lead_scenario(conn, 2, 1, [("q1", 3, "", 1), ("q2", 3, "", 1)])
        conn.execute("INSERT INTO assignments(m_id, r_id, locked) VALUES (1, 1, 1), (1, 2, 1)")
        conn.commit()
        sol = solve(conn)
        assert _lead_pairs_of(sol) == {1}
        assert sol["total_lead_assigned"] == 1

        # 需 2 主审；锁定 1 名合资格（#2）+ 1 名非合资格（#1）→ 锁定抵扣 1，自由分配补齐 1
        _make_lead_scenario(conn, 3, 2, [("n1", 3, "", 0), ("q2", 3, "", 1), ("q3", 3, "", 1)])
        conn.execute("INSERT INTO assignments(m_id, r_id, locked) VALUES (1, 1, 1), (1, 2, 1)")
        conn.commit()
        sol = solve(conn)
        assert _lead_pairs_of(sol) == {2, 3}        # #2 锁定抵扣 + #3 自由补齐
        assert sol["fully_satisfied"]
        # 非合资格锁定永远不算主审
        assert 1 not in _lead_pairs_of(sol)
    finally:
        conn.close()


def test_lead_deficit_lists_manuscript_unmet() -> None:
    """主审不足：仍返回最大可行分配（人数满），该稿列入未满足并分别给出两类缺口。"""
    conn = _connect()
    try:
        # 需 2 人 2 主审，但合资格者仅 1 名 → 人数可满、主审缺 1
        _make_lead_scenario(conn, 2, 2, [("q1", 2, "", 1), ("x1", 2, "", 0), ("x2", 2, "", 0)])
        sol = solve(conn)
        assert not sol["fully_satisfied"]
        assert len(sol["unmet"]) == 1
        u = sol["unmet"][0]
        assert u["deficit"] == 0                    # 总人数缺口 0（最大可行分配仍给出）
        assert u["lead_required"] == 2 and u["lead_assigned"] == 1 and u["lead_deficit"] == 1
        assert _lead_pairs_of(sol) == {1}
        n = conn.execute("SELECT COUNT(*) AS c FROM assignments WHERE m_id=1").fetchone()["c"]
        assert n == 2                               # 总人数仍按最大可行分配
        # 候选明细说明资格/容量/机构阻断（q1 与一名普通评审已分配本稿，候选仅剩 1 人）
        cand = list(u["details"]["candidates"])
        assert len(cand) == 1
        assert cand[0]["can_lead"] is False
        assert "blocked_by_institution" in cand[0] and "remaining_capacity" in cand[0]
        assert u["details"]["lead_pool"] == 1
        assert any("主审缺口" in r for r in u["reasons"])
    finally:
        conn.close()


def test_lead_respects_capacity_and_institution() -> None:
    """主审占用个人容量与机构名额（硬约束）。"""
    conn = _connect()
    try:
        # 容量：合资格者 cap=1，当了主审后不能再接其他稿
        _make_lead_scenario(conn, 1, 1, [("q1", 1, "", 1), ("x1", 2, "", 0)])
        conn.execute(
            "INSERT INTO manuscripts(title, topics, required_reviews, required_leads) "
            "VALUES ('第二稿', '[\"t\"]', 1, 0)")
        conn.commit()
        sol = solve(conn)
        assert _lead_pairs_of(sol, 1) == {1}
        m2 = {r["r_id"] for r in conn.execute("SELECT r_id FROM assignments WHERE m_id=2")}
        assert m2 == {2}                            # q1 容量已被主审占满，第二稿只能 x1

        # 机构：γ=1，主审占了机构 A 名额后，本稿普通名额不能再进 A 的人
        _make_lead_scenario(conn, 2, 1, [("qa", 2, "A", 1), ("xa", 2, "A", 0), ("xb", 2, "B", 0)])
        set_setting(conn, "institution_cap", 1)
        conn.commit()
        sol = solve(conn)
        assert sol["fully_satisfied"]
        assert _lead_pairs_of(sol) == {1}           # 主审 qa（机构 A）
        names = {r["r_id"] for r in conn.execute("SELECT r_id FROM assignments WHERE m_id=1")}
        assert names == {1, 3}                      # 普通名额给 xb（B），xa 被机构上限阻断
    finally:
        conn.close()


def test_lead_free_assignment_prefers_nothing_when_disabled() -> None:
    """主审约束默认关闭：required_leads=0 时不认定任何主审，行为与旧版一致。"""
    conn = _connect()
    try:
        _make_lead_scenario(conn, 2, 0, [("q1", 2, "", 1), ("x1", 2, "", 0)])
        sol = solve(conn)
        assert sol["fully_satisfied"]
        assert sol["lead_pairs"] == []
        assert sol["total_lead_required"] == 0 and sol["total_lead_assigned"] == 0
    finally:
        conn.close()


def test_lead_api_validation() -> None:
    """表单/接口校验：需主审人数 0~2 且不得超过所需评审数。"""
    try:
        from fastapi import HTTPException
        from pydantic import ValidationError
        from app.main import create_manuscript, update_manuscript
        from app.schemas import ManuscriptIn, ManuscriptPatch
    except ModuleNotFoundError:
        print("  （跳过接口校验测试：未安装 fastapi/pydantic）")
        return

    conn = _connect()
    try:
        _make_lead_scenario(conn, 3, 0, [("q1", 3, "", 1)])
        # 超范围（>2）由 Pydantic 拒绝
        try:
            ManuscriptIn(title="x", required_reviews=3, required_leads=3)
            assert False, "应当抛出 ValidationError"
        except ValidationError:
            pass
        # 主审数 > 所需评审数：创建时 400
        try:
            create_manuscript(ManuscriptIn(title="bad", required_reviews=1, required_leads=2), conn)
            assert False, "应当 400"
        except HTTPException as e:
            assert e.status_code == 400
        # PATCH 合并校验：把所需评审数降到 1 而主审仍为 2 → 400
        conn.execute("UPDATE manuscripts SET required_leads=2 WHERE id=1")
        conn.commit()
        try:
            update_manuscript(1, ManuscriptPatch(required_reviews=1), conn)
            assert False, "应当 400"
        except HTTPException as e:
            assert e.status_code == 400
        # 合法 PATCH 正常
        st = update_manuscript(1, ManuscriptPatch(required_reviews=3, required_leads=2), conn)
        m1 = [m for m in st["manuscripts"] if m["id"] == 1][0]
        assert m1["required_leads"] == 2 and m1["required_reviews"] == 3
    finally:
        conn.close()


def test_lead_migration_defaults_off() -> None:
    """旧库（无 can_lead / required_leads 列）迁移后补列且默认关闭主审约束。"""
    path = os.path.join(tempfile.mkdtemp(prefix="review-mig-lead-"), "old.db")
    from app import db
    old_path = db.DB_PATH
    db.DB_PATH = path
    try:
        import sqlite3
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE reviewers (id INTEGER PRIMARY KEY, name TEXT, topics TEXT, capacity INTEGER, institution TEXT)")
        c.execute("INSERT INTO reviewers(name, topics, capacity, institution) VALUES ('老评审','[]',3,'')")
        c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE manuscripts (id INTEGER PRIMARY KEY, title TEXT, topics TEXT, required_reviews INTEGER)")
        c.execute("INSERT INTO manuscripts(title, topics, required_reviews) VALUES ('老稿','[]',3)")
        c.execute("CREATE TABLE conflicts (m_id INTEGER, r_id INTEGER)")
        c.execute("CREATE TABLE assignments (id INTEGER PRIMARY KEY, m_id INTEGER, r_id INTEGER, locked INTEGER)")
        c.commit()
        c.close()

        init_db(seed_demo=False)
        c2 = sqlite3.connect(path)
        c2.row_factory = sqlite3.Row
        r = c2.execute("SELECT can_lead FROM reviewers WHERE name='老评审'").fetchone()
        m = c2.execute("SELECT required_leads FROM manuscripts WHERE title='老稿'").fetchone()
        assert r["can_lead"] == 0 and m["required_leads"] == 0  # 默认关闭
        c2.close()
        # 迁移后求解正常（主审约束关闭）
        conn = _connect()
        try:
            sol = solve(conn)
            assert sol["total_lead_required"] == 0 and sol["lead_pairs"] == []
        finally:
            conn.close()
    finally:
        db.DB_PATH = old_path


if __name__ == "__main__":
    test_mcmf_known_case()
    test_demo_solve_constraints()
    test_locks_occupy_capacity()
    test_institution_cap_hard_limit()
    test_institution_lock_over_cap_warns_and_blocks()
    test_no_institution_reviewers_uncounted()
    test_schema_migration_adds_institution()
    test_lead_assigned_only_qualified()
    test_lead_lock_deduction_by_reviewer_id_order()
    test_lead_deficit_lists_manuscript_unmet()
    test_lead_respects_capacity_and_institution()
    test_lead_free_assignment_prefers_nothing_when_disabled()
    test_lead_api_validation()
    test_lead_migration_defaults_off()
    print("所有冒烟测试通过 ✅")
