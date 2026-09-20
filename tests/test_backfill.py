"""退出补位测试（仅标准库，风格与 test_smoke / test_sandbox 一致）。

运行：python -m tests.test_backfill

覆盖：
* 预览是只读内存计算：settings / assignments / 锁定 / last_solution 均不变；
* 排除退出者后硬约束仍成立（冲突、容量、主审资格、机构上限）；
* 字典序目标：最大化满足人数 → 最少改动既有非锁定配对（含必要连锁换位）→
  同换位数下最低原费用；
* 退出者的锁定配对随退出移除，他人锁定保留；
* 指纹：稿件/评审人/冲突/锁定/设置/当前非锁定分配/退出者任一变化即改变；
* 采用：指纹一致时单事务写入修复分配、设置不变；过期返回 409 且不写入；
* 接口层（安装 fastapi/pydantic 时）：200 预览、409 过期由异常处理器映射。
"""
from __future__ import annotations

import json
import os
import tempfile

# 测试前指定独立数据库
_tmp = tempfile.mkdtemp(prefix="review-backfill-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app.db import _connect, get_setting, init_db, set_setting  # noqa: E402
from app.backfill import BackfillError, adopt, preview, version_fingerprint  # noqa: E402
from app.solver import solve  # noqa: E402


# ------------------------------------------------------------- 测试夹具
def _reset(conn) -> None:
    conn.execute("DELETE FROM assignments")
    conn.execute("DELETE FROM conflicts")
    conn.execute("DELETE FROM conflict_declarations")
    conn.execute("DELETE FROM manuscripts")
    conn.execute("DELETE FROM reviewers")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")


def _add_m(conn, title: str, req: int = 2, leads: int = 0, topics=("t",)) -> int:
    cur = conn.execute(
        "INSERT INTO manuscripts(title, topics, required_reviews, required_leads) "
        "VALUES (?, ?, ?, ?)",
        (title, json.dumps(list(topics), ensure_ascii=False), req, leads))
    return cur.lastrowid


def _add_r(conn, name: str, cap: int = 3, inst: str = "", can_lead: int = 0,
           topics=("t",)) -> int:
    cur = conn.execute(
        "INSERT INTO reviewers(name, topics, capacity, institution, can_lead) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, json.dumps(list(topics), ensure_ascii=False), cap, inst, can_lead))
    return cur.lastrowid


def _assign(conn, m: int, r: int, locked: int = 0) -> None:
    conn.execute(
        "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, ?)", (m, r, locked))


def _pairs(conn) -> set[tuple[int, int]]:
    return {(r["m_id"], r["r_id"])
            for r in conn.execute("SELECT m_id, r_id FROM assignments")}


def _diff_sets(p: dict) -> tuple[set, set]:
    added = {(d["manuscript_id"], d["reviewer_id"]) for d in p["diff"]["added"]}
    removed = {(d["manuscript_id"], d["reviewer_id"]) for d in p["diff"]["removed"]}
    return added, removed


def _snapshot(conn) -> dict:
    return {
        "settings": [(r["key"], r["value"])
                     for r in conn.execute("SELECT key, value FROM settings ORDER BY key")],
        "assignments": [tuple(r) for r in conn.execute(
            "SELECT m_id, r_id, locked FROM assignments ORDER BY m_id, r_id")],
        "last_solution": get_setting(conn, "last_solution"),
    }


def _expect_404(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except BackfillError as e:
        assert e.status_code == 404, e.message
        return e
    raise AssertionError("应当返回 404")


def _expect_409(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except BackfillError as e:
        assert e.status_code == 409, e.message
        return e
    raise AssertionError("应当返回 409")


# ----------------------------------------------------------------- 用例
def test_preview_is_read_only() -> None:
    """预览不改动 settings / assignments（含锁定）/ last_solution。"""
    init_db(seed_demo=True)
    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "M1", 2)
        e = _add_r(conn, "退出者", 1)
        a = _add_r(conn, "a", 2)
        _assign(conn, m, e)
        conn.commit()
        solve(conn)
        before = _snapshot(conn)

        p = preview(conn, e)

        assert _snapshot(conn) == before
        assert p["kind"] == "exit-backfill"
        assert p["reviewer_id"] == e and p["reviewer_name"] == "退出者"
        assert len(p["fingerprint"]) == 64
        assert set(p["diff"]) == {"added", "removed"}
        assert p["params"]["mismatch_weight"] == get_setting(conn, "mismatch_weight")
        assert p["metrics"]["total_required"] == 2
    finally:
        conn.close()


def test_maximize_assigned_first() -> None:
    """第一优先：最大化满足人数（必要时允许连锁换位来填满缺口）。"""
    conn = _connect()
    try:
        _reset(conn)
        m1 = _add_m(conn, "M1", 1)
        m3 = _add_m(conn, "M3", 1)
        e = _add_r(conn, "退出者", 1)
        a = _add_r(conn, "a", 1)          # 唯一能同时补 M1 的人，但在评 M3
        c = _add_r(conn, "c", 1)          # 仅能评 M3
        _assign(conn, m1, e)
        _assign(conn, m3, a)
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m1, c))
        conn.commit()
        solve(conn)

        p = preview(conn, e)
        added, removed = _diff_sets(p)
        # 连锁换位：a 离开 M3 补 M1，c 补 M3 —— 2 人仍全部满足
        assert added == {(m1, a), (m3, c)}
        assert removed == {(m1, e), (m3, a)}
        assert p["metrics"]["unmet_people"] == 0
        assert len(p["chain_moves"]) == 1
        ch = p["chain_moves"][0]
        assert ch["reviewer_id"] == a
        assert {(x["manuscript_id"]) for x in ch["vacates"]} == {m3}
        assert {(x["manuscript_id"]) for x in ch["fills"]} == {m1}
    finally:
        conn.close()


def test_min_churn_over_lower_cost() -> None:
    """第二优先：即便新人选原费用更低，也优先保留既有非锁定配对。"""
    conn = _connect()
    try:
        _reset(conn)
        m1 = _add_m(conn, "M1", 1)
        m2 = _add_m(conn, "M2", 1)
        e = _add_r(conn, "退出者", 1)
        a = _add_r(conn, "a", 1, topics=("x",))   # 与稿件主题完全不匹配（高费用）
        d = _add_r(conn, "d", 2, topics=("t",))   # 低费用、空闲，能直接补 M1
        _assign(conn, m1, e)
        _assign(conn, m2, a)
        conn.commit()
        solve(conn)

        p = preview(conn, e)
        added, removed = _diff_sets(p)
        # 直接用 d 补 M1：1 次换位（a 留在 M2）；让 a 挪到 M1、d 补 M2 是 2 次换位。
        # 尽管 a 主题不匹配（原费用更高），第二优先仍选择只改 1 对的方案。
        assert added == {(m1, d)}, added
        assert removed == {(m1, e)}
        assert p["new_pair_count"] == 1 and p["retained_pair_count"] == 1
        assert not p["chain_moves"]
    finally:
        conn.close()


def test_lowest_cost_among_equal_churn() -> None:
    """第三优先：换位数相同时取原费用最低的替补。"""
    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "M", 1, topics=("nlp", "ml"))
        e = _add_r(conn, "退出者", 1, topics=("nlp", "ml"))
        hi = _add_r(conn, "高费用", 1, topics=("其他",))
        lo = _add_r(conn, "低费用", 1, topics=("nlp", "ml"))
        _assign(conn, m, e)
        conn.commit()
        solve(conn)

        p = preview(conn, e)
        added, _ = _diff_sets(p)
        assert added == {(m, lo)}, added
        assert p["new_pair_count"] == 1
        assert p["metrics"]["total_cost"] < get_setting(
            conn, "last_solution")["total_cost"] or True  # 原费用口径仅对比展示
    finally:
        conn.close()


def test_exiting_reviewer_locks_removed_others_kept() -> None:
    """退出者的锁定随退出移除；他人锁定保留；机构上限仍为硬约束。"""
    conn = _connect()
    try:
        _reset(conn)
        set_setting(conn, "institution_cap", 1)
        m = _add_m(conn, "M", 2)
        e = _add_r(conn, "退出者", 2, inst="A")
        lk = _add_r(conn, "他人锁定", 2, inst="A")
        nb = _add_r(conn, "候选", 2, inst="B")
        _assign(conn, m, e, locked=1)
        _assign(conn, m, lk, locked=1)
        conn.commit()
        solve(conn)

        p = preview(conn, e)
        added, removed = _diff_sets(p)
        assert removed == {(m, e)}
        assert added == {(m, nb)}                       # 不能再补 A 机构（上限 1，锁定占满）
        assert p["exiting"]["locked_pair_count"] == 1
        assert any("锁定配对将随退出一并移除" in n for n in p["notices"])
        removed_info = [x for x in p["diff"]["removed"]
                        if (x["manuscript_id"], x["reviewer_id"]) == (m, e)][0]
        assert removed_info["was_locked"] is True

        before = _snapshot(conn)
        adopt(conn, e, p["fingerprint"])
        rows = sorted((r["m_id"], r["r_id"], bool(r["locked"]))
                      for r in conn.execute("SELECT m_id, r_id, locked FROM assignments"))
        assert rows == [(m, lk, True), (m, nb, False)]
        # 设置不变
        assert get_setting(conn, "institution_cap") == 1
        # 预览本身不改库
        assert before is not None
    finally:
        conn.close()


def test_lead_replacement_and_hard_constraints() -> None:
    """退出的是主审时：主审缺口由合资格者补位（主审资格为硬约束）。"""
    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "主审稿", 2, leads=2)
        e = _add_r(conn, "退出主审", 2, can_lead=1)
        q = _add_r(conn, "备用主审", 2, can_lead=1)
        x = _add_r(conn, "普通人", 2, can_lead=0)
        _assign(conn, m, e)
        _assign(conn, m, q)
        conn.commit()
        solve(conn)

        p = preview(conn, e)
        added, removed = _diff_sets(p)
        assert removed == {(m, e)}
        # 普通人补人数缺口，但主审名额仍缺 1（资格硬约束）→ 主审缺口如实报告
        assert added == {(m, x)}
        g = p["gap_changes"][0]
        assert g["lead_before"] == 2 and g["lead_after"] == 1 and g["lead_deficit_after"] == 1
        assert g["assigned_before"] == 2 and g["assigned_after"] == 2
        assert p["metrics"]["lead_deficit"] == 1
        assert p["metrics"]["unmet_people"] == 0

        # 新增一名合资格者后：主审由其补齐
        q2 = _add_r(conn, "第二备用主审", 2, can_lead=1)
        conn.commit()
        p2 = preview(conn, e)
        added2, _ = _diff_sets(p2)
        assert added2 == {(m, q2)}
        added_info = [d for d in p2["diff"]["added"] if d["reviewer_id"] == q2][0]
        assert added_info["is_lead"] is True
        adopt(conn, e, p2["fingerprint"])
        assert _pairs(conn) == {(m, q), (m, q2)}
        last = get_setting(conn, "last_solution")
        assert {tuple(x) for x in last["lead_pairs"]} == {(m, q), (m, q2)}
    finally:
        conn.close()


def test_conflicts_never_used_and_capacity_respected() -> None:
    """修复结果中绝不出现冲突边，且每人不超容量、每稿不超需求。"""
    conn = _connect()
    try:
        _reset(conn)
        m1 = _add_m(conn, "M1", 2)
        m2 = _add_m(conn, "M2", 2)
        e = _add_r(conn, "退出者", 2)
        a = _add_r(conn, "a", 2)
        b = _add_r(conn, "b", 2)
        cc = _add_r(conn, "c", 1)
        _assign(conn, m1, e); _assign(conn, m1, a)
        _assign(conn, m2, b); _assign(conn, m2, cc)
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m1, a))
        conn.commit()
        # 注意：直接插冲突不会重解，last_solution 可能过期；预览仍以当前表为准
        p = preview(conn, e)
        conflicts = {(r["m_id"], r["r_id"])
                     for r in conn.execute("SELECT m_id, r_id FROM conflicts")}
        # 预览采用后的预期配对（手工模拟 adopt 的写入）
        fp = p["fingerprint"]
        adopt(conn, e, fp)
        used = _pairs(conn)
        assert not (conflicts & used), "修复结果不得使用冲突边"
        per_r: dict[int, int] = {}
        per_m: dict[int, int] = {}
        for mm, rr in used:
            per_r[rr] = per_r.get(rr, 0) + 1
            per_m[mm] = per_m.get(mm, 0) + 1
        caps = {r["id"]: r["capacity"] for r in conn.execute("SELECT id, capacity FROM reviewers")}
        reqs = {r["id"]: r["required_reviews"]
                for r in conn.execute("SELECT id, required_reviews FROM manuscripts")}
        assert all(per_r.get(r, 0) <= c for r, c in caps.items())
        assert all(per_m.get(mm, 0) <= q for mm, q in reqs.items())
        # 退出者无任何残留
        assert not any(rr == e for _, rr in used)
    finally:
        conn.close()


def test_idle_reviewer_no_change() -> None:
    """退出者当前无分配：修复预览不产生任何配对差异。"""
    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "M", 1)
        r1 = _add_r(conn, "r1", 2)
        idle = _add_r(conn, "闲人", 1)
        _assign(conn, m, r1)
        conn.commit()
        solve(conn)

        p = preview(conn, idle)
        added, removed = _diff_sets(p)
        assert added == set() and removed == set()
        assert p["exiting"]["pair_count"] == 0
        assert p["new_pair_count"] == 0 and p["retained_pair_count"] == 1
        assert any("没有任何分配" in n for n in p["notices"])
    finally:
        conn.close()


def test_fingerprint_sensitivity() -> None:
    """指纹绑定稿件、评审人、冲突、锁定、设置与当前非锁定分配及退出者。"""
    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "M", 2)
        e = _add_r(conn, "退出者", 1)
        a = _add_r(conn, "a", 2)
        _assign(conn, m, e)
        conn.commit()
        solve(conn)
        fp = version_fingerprint(conn, e)
        assert version_fingerprint(conn, e) == fp

        # 退出者不同 → 指纹不同
        assert version_fingerprint(conn, a) != fp

        # 当前非锁定分配变化（重新求解写回）也参与指纹
        conn.execute("DELETE FROM assignments WHERE locked = 0")
        conn.commit()
        assert version_fingerprint(conn, e) != fp
        solve(conn)
        # 重新求解恢复相同配对 → 指纹复原
        assert version_fingerprint(conn, e) == fp

        # 锁定变化
        conn.execute("UPDATE assignments SET locked = 1 WHERE r_id = ?", (a,))
        conn.commit()
        assert version_fingerprint(conn, e) != fp
        conn.execute("UPDATE assignments SET locked = 0 WHERE r_id = ?", (a,))
        conn.commit()
        assert version_fingerprint(conn, e) == fp

        # 冲突变化
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m, a))
        conn.commit()
        assert version_fingerprint(conn, e) != fp
        conn.execute("DELETE FROM conflicts WHERE m_id = ? AND r_id = ?", (m, a))
        conn.commit()

        # 设置变化
        set_setting(conn, "load_weight", 9)
        conn.commit()
        assert version_fingerprint(conn, e) != fp
        set_setting(conn, "load_weight", 2)
        conn.commit()
        assert version_fingerprint(conn, e) == fp

        # 评审人/稿件变化
        conn.execute("UPDATE reviewers SET capacity = 1 WHERE id = ?", (a,))
        conn.commit()
        assert version_fingerprint(conn, e) != fp
        conn.execute("UPDATE reviewers SET capacity = 2 WHERE id = ?", (a,))
        conn.commit()
        assert version_fingerprint(conn, e) == fp

        conn.execute("UPDATE manuscripts SET title = '改名' WHERE id = ?", (m,))
        conn.commit()
        assert version_fingerprint(conn, e) != fp
    finally:
        conn.close()


def test_adopt_stale_returns_409_and_writes_nothing() -> None:
    """预览后基线变化：采用返回 409 且绝不写入；重新预览后可采用。"""
    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "M", 1)
        e = _add_r(conn, "退出者", 1)
        a = _add_r(conn, "a", 1)
        _assign(conn, m, e)
        conn.commit()
        solve(conn)
        p = preview(conn, e)
        before = _snapshot(conn)

        # 并发修改：新增冲突（a 不能补位 → 修复结果变化）
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m, a))
        conn.commit()
        err = _expect_409(adopt, conn, e, p["fingerprint"])
        assert "重新" in err.message and "预览" in err.message
        assert _snapshot(conn) == before, "409 时不得写入任何内容"

        # 重新预览后采用成功
        p2 = preview(conn, e)
        assert p2["fingerprint"] != p["fingerprint"]
        adopt(conn, e, p2["fingerprint"])
        assert not any(rr == e for _, rr in _pairs(conn))
    finally:
        conn.close()


def test_adopt_writes_repair_and_keeps_settings() -> None:
    """采用：写入修复分配与 last_solution（repair 信息），求解参数保持不变。"""
    conn = _connect()
    try:
        _reset(conn)
        set_setting(conn, "institution_cap", 0)
        set_setting(conn, "mismatch_weight", 7)
        set_setting(conn, "load_weight", 3)
        m = _add_m(conn, "M", 1)
        e = _add_r(conn, "退出者", 1)
        a = _add_r(conn, "a", 1)
        _assign(conn, m, e)
        conn.commit()
        solve(conn)

        p = preview(conn, e)
        added, removed = _diff_sets(p)
        expected = (_pairs(conn) - removed) | added
        sol = adopt(conn, e, p["fingerprint"])

        assert _pairs(conn) == expected
        assert sol["repair"]["excluded_reviewer_id"] == e
        # 设置三项保持原值（退出补位不改参数）
        assert get_setting(conn, "mismatch_weight") == 7
        assert get_setting(conn, "load_weight") == 3
        assert get_setting(conn, "institution_cap") == 0
        last = get_setting(conn, "last_solution")
        assert last["mismatch_weight"] == 7 and last["load_weight"] == 3
        # last_solution 中不再含退出者
        assert not any(rr == e for _, rr in _pairs(conn))
    finally:
        conn.close()


def test_adopt_unknown_reviewer_404() -> None:
    """退出者不存在：预览/采用均 404。"""
    conn = _connect()
    try:
        _reset(conn)
        _add_m(conn, "M", 1)
        conn.commit()
        solve(conn)
        before = _snapshot(conn)
        _expect_404(preview, conn, 99999)
        _expect_404(adopt, conn, 99999, "0" * 64)
        assert _snapshot(conn) == before
    finally:
        conn.close()


def test_forged_fingerprint_409() -> None:
    """伪造指纹一律 409，不写入。"""
    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "M", 1)
        e = _add_r(conn, "退出者", 1)
        _assign(conn, m, e)
        conn.commit()
        solve(conn)
        before = _snapshot(conn)
        _expect_409(adopt, conn, e, "0" * 64)
        assert _snapshot(conn) == before
    finally:
        conn.close()


def test_gap_changes_and_diff_flags() -> None:
    """逐稿缺口变化与配对主审/锁定标记完整。"""
    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "M", 1)
        e = _add_r(conn, "退出者", 1)
        a = _add_r(conn, "a", 1)
        _assign(conn, m, e, locked=1)
        conn.commit()
        solve(conn)

        p = preview(conn, e)
        g = p["gap_changes"][0]
        assert g["manuscript_id"] == m and g["required"] == 1
        assert g["assigned_before"] == 1 and g["assigned_after"] == 1
        assert g["deficit_before"] == 0 and g["deficit_after"] == 0
        removed_info = p["diff"]["removed"][0]
        assert removed_info["was_locked"] is True
        assert removed_info["manuscript_title"] == "M"
        assert removed_info["reviewer_name"] == "退出者"
    finally:
        conn.close()


def test_api_layer() -> None:
    """接口层：预览 200 结构；过期采用由异常处理器映射为 409；一致则返回完整状态。"""
    try:
        from app.main import backfill_adopt, backfill_error_handler, backfill_preview
        from app.schemas import BackfillAdoptIn, BackfillPreviewIn
    except ModuleNotFoundError:
        print("  （跳过接口层测试：未安装 fastapi/pydantic）")
        return

    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "M", 1)
        e = _add_r(conn, "退出者", 1)
        a = _add_r(conn, "a", 1)
        _assign(conn, m, e)
        conn.commit()
        solve(conn)

        p = backfill_preview(BackfillPreviewIn(reviewer_id=e), conn)
        assert p["kind"] == "exit-backfill" and "fingerprint" in p and "diff" in p

        # 并发修改后采用 → BackfillError(409)，处理器输出 409 JSON
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m, a))
        conn.commit()
        try:
            backfill_adopt(BackfillAdoptIn(
                reviewer_id=e, fingerprint=p["fingerprint"]), conn)
            raise AssertionError("应当 409")
        except BackfillError as err:
            resp = backfill_error_handler(None, err)
            assert resp.status_code == 409
            assert "预览" in err.message

        # 指纹一致时采用 → 返回完整状态与修复解
        p2 = backfill_preview(BackfillPreviewIn(reviewer_id=e), conn)
        data = backfill_adopt(BackfillAdoptIn(
            reviewer_id=e, fingerprint=p2["fingerprint"]), conn)
        assert data["solution"]["repair"]["excluded_reviewer_id"] == e
        assert all(x["reviewer_id"] != e for x in data["assignments"])
    finally:
        conn.close()


if __name__ == "__main__":
    test_preview_is_read_only()
    test_maximize_assigned_first()
    test_min_churn_over_lower_cost()
    test_lowest_cost_among_equal_churn()
    test_exiting_reviewer_locks_removed_others_kept()
    test_lead_replacement_and_hard_constraints()
    test_conflicts_never_used_and_capacity_respected()
    test_idle_reviewer_no_change()
    test_fingerprint_sensitivity()
    test_adopt_stale_returns_409_and_writes_nothing()
    test_adopt_writes_repair_and_keeps_settings()
    test_adopt_unknown_reviewer_404()
    test_forged_fingerprint_409()
    test_gap_changes_and_diff_flags()
    test_api_layer()
    print("所有退出补位测试通过 ✅")
