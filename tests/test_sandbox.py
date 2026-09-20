"""方案沙盒测试（仅标准库，风格与 test_smoke / test_declarations 一致）。

运行：python -m tests.test_sandbox

覆盖：
* 预览不改动 settings / assignments / 锁定 / last_solution；
* 预览差异（新增/移除配对）与指标（总成本、未满足人数、主审缺口）正确；
* 版本指纹：稿件/评审人/冲突/锁定/参数任一变化即改变；同数据重求解不改变；
* 采用：指纹一致时单事务写入临时参数与预览分配，结果与直接求解一致；
* 采用：指纹过期返回 409 且不写入任何内容；重新预览后可采用。
"""
from __future__ import annotations

import json
import os
import tempfile

# 测试前指定独立数据库
_tmp = tempfile.mkdtemp(prefix="review-sandbox-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app.db import _connect, get_setting, init_db, set_setting  # noqa: E402
from app.sandbox import SandboxError, adopt, preview, version_fingerprint  # noqa: E402
from app.solver import compute_solution, solve  # noqa: E402


# ------------------------------------------------------------- 测试夹具
def _make_scenario(conn, cap: int = 1) -> None:
    """清空并构造：1 篇需求 3 的稿件；A 机构 3 人、B 机构 1 人、无机构 1 人。

    机构上限 cap=1 时分配为 A/B/无机构 各 1 人；cap=0 时三人可同机构，
    便于预览产生确定的新增/移除配对差异。
    """
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
    set_setting(conn, "mismatch_weight", 10)
    set_setting(conn, "load_weight", 2)
    conn.commit()


def _snapshot(conn) -> dict:
    """需要被预览“绝不动”的数据快照。"""
    return {
        "settings": dict(conn.execute("SELECT key, value FROM settings").fetchall()
                         and [(r["key"], r["value"]) for r in
                              conn.execute("SELECT key, value FROM settings")]),
        "assignments": [tuple(r) for r in conn.execute(
            "SELECT m_id, r_id, locked FROM assignments ORDER BY m_id, r_id")],
        "last_solution": get_setting(conn, "last_solution"),
    }


def _pairs(conn) -> set[tuple[int, int]]:
    return {(r["m_id"], r["r_id"]) for r in conn.execute(
        "SELECT m_id, r_id FROM assignments")}


def _expect_409(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except SandboxError as e:
        assert e.status_code == 409, e.message
        return e
    raise AssertionError("应当返回 409")


# ----------------------------------------------------------------- 用例
def test_preview_does_not_mutate() -> None:
    """预览是纯内存计算：settings、assignments（含锁定）、last_solution 均不变。"""
    init_db(seed_demo=True)
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        solve(conn)
        # 加一把锁，确认锁定行也不受影响
        conn.execute("UPDATE assignments SET locked = 1 WHERE m_id = 1 AND r_id = "
                     "(SELECT r_id FROM assignments WHERE m_id = 1 LIMIT 1)")
        conn.commit()
        solve(conn)
        before = _snapshot(conn)

        p = preview(conn, mismatch_weight=3, load_weight=9, institution_cap=0)

        after = _snapshot(conn)
        assert after == before, "预览不得改动 settings / assignments / last_solution"
        # 预览返回结构完整
        assert p["params"] == {"mismatch_weight": 3, "load_weight": 9, "institution_cap": 0}
        assert p["current_params"] == {"mismatch_weight": 10, "load_weight": 2,
                                       "institution_cap": 1}
        assert len(p["fingerprint"]) == 64
        assert set(p["diff"]) == {"added", "removed"}
        assert p["metrics"]["total_required"] == 3
        assert p["current_metrics"] is not None
        assert p["delta"] is not None
    finally:
        conn.close()


def test_preview_diff_and_metrics() -> None:
    """新增/移除配对 = 预览分配集与当前分配集的对称差；指标与当前解一致。"""
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        sol = solve(conn)
        current = _pairs(conn)

        p = preview(conn, mismatch_weight=10, load_weight=2, institution_cap=0)
        added = {(d["manuscript_id"], d["reviewer_id"]) for d in p["diff"]["added"]}
        removed = {(d["manuscript_id"], d["reviewer_id"]) for d in p["diff"]["removed"]}

        # 预览分配集 = 锁定 ∪ 内存重算的新自由分配
        _, expected_new = compute_solution(conn, 10, 2, 0)
        locked = {(r["m_id"], r["r_id"]) for r in conn.execute(
            "SELECT m_id, r_id FROM assignments WHERE locked = 1")}
        expected_preview = locked | set(expected_new)
        assert added == expected_preview - current
        assert removed == current - expected_preview
        assert not (added & current)          # 新增必不在当前
        assert removed <= current             # 移除必来自当前
        assert (current - removed) | added == expected_preview

        # 指标：与直接重算一致；当前指标来自 last_solution
        assert p["metrics"]["total_cost"] == compute_solution(conn, 10, 2, 0)[0]["total_cost"]
        assert p["current_metrics"]["total_cost"] == sol["total_cost"]
        assert p["current_metrics"]["unmet_people"] == sum(
            u["deficit"] for u in sol["unmet"])
        assert p["delta"]["total_cost"] == (
            p["metrics"]["total_cost"] - p["current_metrics"]["total_cost"])
        # 本场景 cap=0 与 cap=1 都能满足 3 人，但配对集合确实不同
        assert added or removed
    finally:
        conn.close()


def test_preview_lead_deficit_metrics() -> None:
    """主审缺口指标：预览与当前方案分别统计 lead_deficit 并给出变化量。"""
    conn = _connect()
    try:
        conn.execute("DELETE FROM assignments")
        conn.execute("DELETE FROM conflicts")
        conn.execute("DELETE FROM manuscripts")
        conn.execute("DELETE FROM reviewers")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
        conn.execute(
            "INSERT INTO manuscripts(title, topics, required_reviews, required_leads) "
            "VALUES ('主审稿', '[\"t\"]', 2, 2)")
        # 仅 1 名可任主审 → 主审缺口恒为 1
        conn.execute("INSERT INTO reviewers(name, topics, capacity, can_lead) "
                     "VALUES ('q1', '[\"t\"]', 2, 1)")
        conn.execute("INSERT INTO reviewers(name, topics, capacity, can_lead) "
                     "VALUES ('x1', '[\"t\"]', 2, 0)")
        conn.execute("INSERT INTO reviewers(name, topics, capacity, can_lead) "
                     "VALUES ('x2', '[\"t\"]', 2, 0)")
        conn.commit()
        sol = solve(conn)
        assert sol["unmet"][0]["lead_deficit"] == 1

        p = preview(conn, mismatch_weight=5, load_weight=1, institution_cap=0)
        assert p["metrics"]["lead_deficit"] == 1
        assert p["metrics"]["unmet_people"] == 0      # 人数可满足，仅主审不足
        assert p["current_metrics"]["lead_deficit"] == 1
        assert p["delta"]["lead_deficit"] == 0
        assert p["unmet"][0]["lead_deficit"] == 1
    finally:
        conn.close()


def test_fingerprint_stability_and_sensitivity() -> None:
    """指纹：同数据稳定；稿件/评审人/冲突/锁定/参数变化即变；同数据重求解不变。"""
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        fp0 = version_fingerprint(conn)
        assert version_fingerprint(conn) == fp0

        # 同数据重新求解（重写非锁定分配为相同配对）→ 指纹不变
        solve(conn)
        assert version_fingerprint(conn) == fp0

        # 稿件变化
        conn.execute("UPDATE manuscripts SET title = '测试稿·改' WHERE id = 1")
        conn.commit()
        fp1 = version_fingerprint(conn)
        assert fp1 != fp0
        conn.execute("UPDATE manuscripts SET title = '测试稿' WHERE id = 1")
        conn.commit()
        assert version_fingerprint(conn) == fp0  # 改回后指纹复原

        # 评审人变化
        conn.execute("UPDATE reviewers SET capacity = 2 WHERE name = 'a1'")
        conn.commit()
        assert version_fingerprint(conn) != fp0
        conn.execute("UPDATE reviewers SET capacity = 3 WHERE name = 'a1'")
        conn.commit()

        # 冲突变化
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (1, 1)")
        conn.commit()
        assert version_fingerprint(conn) != fp0
        conn.execute("DELETE FROM conflicts WHERE m_id = 1 AND r_id = 1")
        conn.commit()
        assert version_fingerprint(conn) == fp0

        # 锁定变化
        conn.execute("UPDATE assignments SET locked = 1 WHERE m_id = 1 AND r_id = "
                     "(SELECT r_id FROM assignments WHERE m_id = 1 LIMIT 1)")
        conn.commit()
        assert version_fingerprint(conn) != fp0
        conn.execute("UPDATE assignments SET locked = 0 WHERE m_id = 1")
        conn.commit()
        assert version_fingerprint(conn) == fp0

        # 求解参数变化
        set_setting(conn, "load_weight", 7)
        conn.commit()
        assert version_fingerprint(conn) != fp0
        set_setting(conn, "load_weight", 2)
        conn.commit()
        assert version_fingerprint(conn) == fp0
    finally:
        conn.close()


def test_adopt_writes_params_and_assignments() -> None:
    """采用：单事务写入临时参数 + 预览分配；结果与同参数直接求解一致。"""
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        solve(conn)
        # 锁定一对，确认采用后锁定保留
        conn.execute("UPDATE assignments SET locked = 1 WHERE m_id = 1 AND r_id = "
                     "(SELECT r_id FROM assignments WHERE m_id = 1 LIMIT 1)")
        conn.commit()
        solve(conn)
        locked_before = {(r["m_id"], r["r_id"]) for r in conn.execute(
            "SELECT m_id, r_id FROM assignments WHERE locked = 1")}
        assert len(locked_before) == 1

        p = preview(conn, mismatch_weight=4, load_weight=6, institution_cap=0)
        expected_pairs = (_pairs(conn)
                          - {(d["manuscript_id"], d["reviewer_id"]) for d in p["diff"]["removed"]}
                          ) | {(d["manuscript_id"], d["reviewer_id"]) for d in p["diff"]["added"]}

        sol = adopt(conn, 4, 6, 0, p["fingerprint"])

        # 临时参数已写入设置
        assert int(get_setting(conn, "mismatch_weight")) == 4
        assert int(get_setting(conn, "load_weight")) == 6
        assert int(get_setting(conn, "institution_cap")) == 0
        # 预览分配已写入（锁定保留），且与同参数重算一致
        assert _pairs(conn) == expected_pairs
        assert locked_before <= _pairs(conn)
        assert _pairs(conn) == locked_before | set(
            compute_solution(conn, 4, 6, 0)[1])
        # last_solution 已更新为预览解
        last = get_setting(conn, "last_solution")
        assert last["mismatch_weight"] == 4 and last["load_weight"] == 6
        assert last["institution_cap"] == 0
        assert last["total_cost"] == sol["total_cost"]
        # 采用后指纹随参数变化而更新
        assert version_fingerprint(conn) != p["fingerprint"]
    finally:
        conn.close()


def test_adopt_stale_returns_409_and_writes_nothing() -> None:
    """预览后发生并发修改：采用返回 409 且绝不写入；重新预览后可采用。"""
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        solve(conn)
        p = preview(conn, mismatch_weight=4, load_weight=6, institution_cap=0)
        before = _snapshot(conn)

        # 并发修改：新增一条冲突
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (1, 1)")
        conn.commit()

        e = _expect_409(adopt, conn, 4, 6, 0, p["fingerprint"])
        assert "重新" in e.message and "预览" in e.message
        assert _snapshot(conn) == before, "409 时不得写入任何内容"

        # 重新预览 → 新指纹 → 可采用
        p2 = preview(conn, mismatch_weight=4, load_weight=6, institution_cap=0)
        assert p2["fingerprint"] != p["fingerprint"]
        adopt(conn, 4, 6, 0, p2["fingerprint"])
        assert int(get_setting(conn, "mismatch_weight")) == 4
    finally:
        conn.close()


def test_adopt_fingerprint_covers_locks_and_settings() -> None:
    """锁定变化、参数变化同样使采用 409。"""
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        solve(conn)

        # 预览后新增一把锁 → 409
        p = preview(conn, 10, 2, 1)
        conn.execute("UPDATE assignments SET locked = 1 WHERE m_id = 1 AND r_id = "
                     "(SELECT r_id FROM assignments WHERE m_id = 1 LIMIT 1)")
        conn.commit()
        _expect_409(adopt, conn, 10, 2, 1, p["fingerprint"])

        # 预览后修改求解参数 → 409
        p2 = preview(conn, 10, 2, 1)
        set_setting(conn, "institution_cap", 2)
        conn.commit()
        _expect_409(adopt, conn, 10, 2, 1, p2["fingerprint"])

        # 指纹一致（无并发修改）→ 正常采用
        p3 = preview(conn, 10, 2, 1)
        adopt(conn, 10, 2, 1, p3["fingerprint"])
        assert int(get_setting(conn, "institution_cap")) == 1
    finally:
        conn.close()


def test_adopt_requires_valid_fingerprint() -> None:
    """伪造/空指纹一律 409，不写入。"""
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        solve(conn)
        before = _snapshot(conn)
        _expect_409(adopt, conn, 1, 1, 0, "0" * 64)
        assert _snapshot(conn) == before
    finally:
        conn.close()


def test_sandbox_api_layer() -> None:
    """接口层：预览返回 200 结构；过期采用由异常处理器映射为 409。"""
    try:
        from fastapi.responses import JSONResponse  # noqa: F401
        from app.main import sandbox_adopt, sandbox_error_handler, sandbox_preview
        from app.schemas import SandboxAdoptIn, SandboxPreviewIn
    except ModuleNotFoundError:
        print("  （跳过接口层测试：未安装 fastapi/pydantic）")
        return

    conn = _connect()
    try:
        _make_scenario(conn, 1)
        solve(conn)
        p = sandbox_preview(SandboxPreviewIn(
            mismatch_weight=3, load_weight=5, institution_cap=0), conn)
        assert p["params"]["mismatch_weight"] == 3
        assert "fingerprint" in p and "diff" in p and "metrics" in p

        # 并发修改后经接口采用 → SandboxError(409)，处理器输出 409 JSON
        conn.execute("INSERT INTO reviewers(name, topics, capacity) VALUES ('new', '[]', 1)")
        conn.commit()
        try:
            sandbox_adopt(SandboxAdoptIn(
                mismatch_weight=3, load_weight=5, institution_cap=0,
                fingerprint=p["fingerprint"]), conn)
            raise AssertionError("应当 409")
        except SandboxError as err:
            resp = sandbox_error_handler(None, err)
            assert resp.status_code == 409
            assert "预览" in err.message

        # 指纹一致时经接口采用 → 返回完整状态与新解
        p2 = sandbox_preview(SandboxPreviewIn(
            mismatch_weight=3, load_weight=5, institution_cap=0), conn)
        data = sandbox_adopt(SandboxAdoptIn(
            mismatch_weight=3, load_weight=5, institution_cap=0,
            fingerprint=p2["fingerprint"]), conn)
        assert data["settings"]["mismatch_weight"] == 3
        assert data["solution"]["load_weight"] == 5
        assert data["solution"]["institution_cap"] == 0
    finally:
        conn.close()


if __name__ == "__main__":
    test_preview_does_not_mutate()
    test_preview_diff_and_metrics()
    test_preview_lead_deficit_metrics()
    test_fingerprint_stability_and_sensitivity()
    test_adopt_writes_params_and_assignments()
    test_adopt_stale_returns_409_and_writes_nothing()
    test_adopt_fingerprint_covers_locks_and_settings()
    test_adopt_requires_valid_fingerprint()
    test_sandbox_api_layer()
    print("所有方案沙盒测试通过 ✅")
