"""缺口解困建议测试（仅标准库，风格与 test_sandbox / test_backfill 一致）。

运行：python -m tests.test_relief

覆盖：
* 模块只在当前分配存在人数/主审缺口时工作；无缺口返回空建议与明确说明；
* 三类单步调整（容量 +1 / 机构上限放宽 1 / 临时补充主审资格）均能被枚举，
  且只有严格减少缺口者入选；无任何单步可改善时返回空清单与明确说明；
* 排序：总缺口减少 ↓ → 主审缺口减少 ↓ → 改动配对数 ↑ → 成本增量 ↑；
* 每条建议给出受影响稿件与新旧（含保留）配对、主审身份与逐稿缺口对照；
* 试算完全只读：settings / reviewers / assignments / 锁定 / last_solution 不变；
* 机构上限关闭（γ=0）时不枚举“放宽 1”；不具主审资格者才枚举资格补充；
* 临时覆盖只放宽约束：建议方案的缺口不可能比基线差；
* 接口层（安装 fastapi/pydantic 时）：200 与响应结构。
"""
from __future__ import annotations

import json
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="review-relief-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app.db import _connect, get_setting, init_db, set_setting  # noqa: E402
from app.relief import (  # noqa: E402
    TYPE_CAPACITY,
    TYPE_INSTITUTION,
    TYPE_LEAD_QUALIFY,
    suggest,
)
from app.solver import compute_solution, solve  # noqa: E402


# ------------------------------------------------------------- 测试夹具
def _reset(conn) -> None:
    conn.execute("DELETE FROM assignments")
    conn.execute("DELETE FROM conflicts")
    conn.execute("DELETE FROM conflict_declarations")
    conn.execute("DELETE FROM manuscripts")
    conn.execute("DELETE FROM reviewers")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
    conn.commit()


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


def _conflict(conn, m: int, r: int) -> None:
    conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m, r))


def _snapshot(conn) -> dict:
    return {
        "settings": [(r["key"], r["value"])
                     for r in conn.execute("SELECT key, value FROM settings ORDER BY key")],
        "reviewers": [tuple(r) for r in conn.execute(
            "SELECT id, name, capacity, institution, can_lead FROM reviewers ORDER BY id")],
        "assignments": [tuple(r) for r in conn.execute(
            "SELECT m_id, r_id, locked FROM assignments ORDER BY m_id, r_id")],
        "last_solution": get_setting(conn, "last_solution"),
    }


def _by_type(res: dict, t: str) -> list[dict]:
    return [s for s in res["suggestions"] if s["adjustment"]["type"] == t]


# ----------------------------------------------------------------- 用例
def test_no_gap_returns_empty() -> None:
    """无人数/主审缺口：不工作，has_gap=false，建议为空并明确说明。"""
    init_db(seed_demo=False)
    conn = _connect()
    try:
        _reset(conn)
        _add_m(conn, "M", 1)
        _add_r(conn, "r", 2)
        conn.commit()
        solve(conn)

        res = suggest(conn)
        assert res["kind"] == "gap-relief-suggestions"
        assert res["has_gap"] is False
        assert res["suggestions"] == []
        assert res["candidate_count"] == 0
        assert "无需" in res["message"]
        assert res["baseline_metrics"]["unmet_people"] == 0
        assert res["baseline_metrics"]["lead_deficit"] == 0
    finally:
        conn.close()


def test_capacity_plus_one_suggestion() -> None:
    """全局容量不足：把某空闲合资格评审人容量 +1 可补齐人数缺口。"""
    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "M", 1)
        busy = _add_r(conn, "已满", 0)
        idle = _add_r(conn, "可扩容", 0)
        conn.commit()
        solve(conn)

        before = _snapshot(conn)
        res = suggest(conn)
        assert res["has_gap"] is True
        caps = _by_type(res, TYPE_CAPACITY)
        assert len(caps) == 2  # 每位评审人各枚举一次
        for s in caps:
            assert s["adjustment"]["capacity_from"] == 0
            assert s["adjustment"]["capacity_to"] == 1
        # 两名评审人扩容后都能补 M：各减 1 人缺口
        assert {s["adjustment"]["reviewer_id"] for s in caps} == {busy, idle}
        assert all(s["deficit_reduction"] == 1 for s in caps)
        assert all(s["lead_deficit_reduction"] == 0 for s in caps)
        # 受影响稿件与新旧配对
        s = caps[0]
        assert len(s["affected_manuscripts"]) == 1
        g = s["affected_manuscripts"][0]
        assert g["manuscript_id"] == m
        assert g["deficit_before"] == 1 and g["deficit_after"] == 0
        assert g["old_pairs"] == [] and len(g["new_pairs"]) == 1
        assert g["new_pairs"][0]["manuscript_id"] == m
        assert s["diff"]["removed"] == []
        assert s["fully_resolves"] is True
        # 只读：快照不变
        assert _snapshot(conn) == before
    finally:
        conn.close()


def test_institution_cap_relax_suggestion() -> None:
    """同机构上限卡死时：只有放宽上限 1 有效；容量 +1 无法绕过硬约束。"""
    conn = _connect()
    try:
        _reset(conn)
        set_setting(conn, "institution_cap", 1)
        _add_m(conn, "M", 2)
        for name in ("a", "b"):
            _add_r(conn, name, 3, inst="A")
        conn.commit()
        solve(conn)

        res = suggest(conn)
        # 候选：2 个容量 +1、1 个机构放宽、2 个主审资格
        assert res["candidate_count"] == 5
        inst = _by_type(res, TYPE_INSTITUTION)
        assert len(inst) == 1
        s = inst[0]
        assert s["adjustment"]["institution_cap_from"] == 1
        assert s["adjustment"]["institution_cap_to"] == 2
        assert s["deficit_reduction"] == 1
        # 容量 +1 一律不能改善（仍被同机构硬上限挡住）
        assert _by_type(res, TYPE_CAPACITY) == []
        assert s["fully_resolves"] is True
        assert s["affected_manuscripts"][0]["deficit_after"] == 0
    finally:
        conn.close()


def test_institution_cap_disabled_not_enumerated() -> None:
    """γ=0（机构约束关闭）时不枚举“放宽 1”调整。"""
    conn = _connect()
    try:
        _reset(conn)
        set_setting(conn, "institution_cap", 0)
        _add_m(conn, "M", 1)
        _add_r(conn, "r", 0)
        conn.commit()
        solve(conn)

        res = suggest(conn)
        assert _by_type(res, TYPE_INSTITUTION) == []
        assert all(s["adjustment"]["type"] != TYPE_INSTITUTION
                   for s in res["suggestions"])
    finally:
        conn.close()


def test_grant_lead_qualification_suggestion() -> None:
    """主审缺口：为已分配/可分配但不具资格的评审人临时补资格可补齐主审。"""
    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "主审稿", 1, leads=1)
        r = _add_r(conn, "普通人", 1, can_lead=0)
        conn.commit()
        solve(conn)
        # 人数满足、主审缺 1
        base = get_setting(conn, "last_solution")
        assert base["total_assigned"] == 1
        assert sum(u["lead_deficit"] for u in base["unmet"]) == 1

        res = suggest(conn)
        leads = _by_type(res, TYPE_LEAD_QUALIFY)
        assert len(leads) == 1
        s = leads[0]
        assert s["adjustment"]["reviewer_id"] == r
        assert s["lead_deficit_reduction"] == 1
        assert s["deficit_reduction"] == 0
        # 配对不变（0 改动），既有配对被新方案认定为主审：受影响稿件仍需展示
        assert s["changed_pair_count"] == 0
        assert s["diff"]["added"] == [] and s["diff"]["removed"] == []
        g = s["affected_manuscripts"][0]
        assert g["manuscript_id"] == m
        assert g["lead_before"] == 0 and g["lead_after"] == 1
        assert g["lead_deficit_before"] == 1 and g["lead_deficit_after"] == 0
        assert len(g["kept_pairs"]) == 1
        assert g["kept_pairs"][0]["was_lead"] is False
        assert g["kept_pairs"][0]["is_lead"] is True
        assert s["fully_resolves"] is True
    finally:
        conn.close()


def test_already_lead_reviewer_not_enumerated() -> None:
    """已具备主审资格者不枚举“补充资格”。"""
    conn = _connect()
    try:
        _reset(conn)
        _add_m(conn, "M", 1, leads=1)
        leader = _add_r(conn, "已是主审", 0, can_lead=1)
        conn.commit()
        solve(conn)
        res = suggest(conn)
        for s in res["suggestions"]:
            assert not (s["adjustment"]["type"] == TYPE_LEAD_QUALIFY
                        and s["adjustment"]["reviewer_id"] == leader)
    finally:
        conn.close()


def test_no_single_step_helps_empty_suggestions() -> None:
    """缺口存在但没有任何单步调整可改善：明确返回空建议清单。"""
    conn = _connect()
    try:
        _reset(conn)
        set_setting(conn, "institution_cap", 2)
        m = _add_m(conn, "M", 2, leads=1)
        r = _add_r(conn, "唯一评审", 0, can_lead=0)
        _conflict(conn, m, r)   # 唯一评审又冲突：扩容/补资格都无解
        conn.commit()
        solve(conn)

        res = suggest(conn)
        assert res["has_gap"] is True
        # γ=2 开启：1 个容量 +1、1 个机构放宽、1 个资格补充
        assert res["candidate_count"] == 3
        assert res["suggestions"] == []
        assert "没有" in res["message"] and "单步" in res["message"]
    finally:
        conn.close()


def test_ordering_deficit_then_lead_then_churn_then_cost() -> None:
    """排序：先比总缺口减少，再主审缺口减少，再改动配对数，最后成本增量。"""
    conn = _connect()
    try:
        _reset(conn)
        # m1：纯人数缺口（总容量恰好差 1，不需主审），仅零容量评审可补——扩容减人数缺口
        m1 = _add_m(conn, "M1", 4, leads=0)
        # m2：人数满足、主审缺 1——在岗的非主审补资格即可，0 配对改动
        m2 = _add_m(conn, "M2", 1, leads=1)
        cap_filler = _add_r(conn, "扩容补位", 0, can_lead=0)
        _add_r(conn, "在岗普通人", 1, can_lead=0)
        m2_reviewer = _add_r(conn, "M1帮手", 2, can_lead=0)
        _add_r(conn, "闲人", 2, can_lead=0)
        conn.commit()
        solve(conn)

        res = suggest(conn)
        sigs = [
            (s["deficit_reduction"], s["lead_deficit_reduction"],
             s["changed_pair_count"], s["cost_delta"], s["adjustment"]["type"])
            for s in res["suggestions"]
        ]
        # 与显式排序键完全一致
        keys = [(-d, -l, c, cost, t) for d, l, c, cost, t in sigs]
        assert keys == sorted(keys), sigs
        # 总缺口减少 1 的建议排在只减主审缺口的前面
        types_in_order = [s["adjustment"]["type"] for s in res["suggestions"]]
        assert types_in_order[0] == TYPE_CAPACITY
        cap = next(s for s in res["suggestions"] if s["adjustment"]["reviewer_id"] == cap_filler)
        assert cap["deficit_reduction"] == 1 and cap["lead_deficit_reduction"] == 0
        # 存在 0 配对改动的主审资格建议（在岗评审被直接认定为主审）
        zero_churn_leads = [s for s in _by_type(res, TYPE_LEAD_QUALIFY)
                            if s["changed_pair_count"] == 0]
        assert zero_churn_leads, "应至少有一个 0 改动的主审资格补充建议"
        lead = zero_churn_leads[0]
        assert lead["adjustment"]["reviewer_id"] == m2_reviewer
        assert lead["lead_deficit_reduction"] == 1
    finally:
        conn.close()


def test_churn_tiebreak_by_changed_pairs_and_cost() -> None:
    """同为扩容补位：改动配对数相同者按成本增量升序。"""
    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "M", 1, topics=("t",))
        cheap = _add_r(conn, "低费用", 0, topics=("t",))
        pricey = _add_r(conn, "高费用", 0, topics=("其他",))
        conn.commit()
        solve(conn)

        res = suggest(conn)
        caps = _by_type(res, TYPE_CAPACITY)
        assert len(caps) == 2
        costs = {s["adjustment"]["reviewer_id"]: s["cost_delta"] for s in caps}
        assert costs[cheap] < costs[pricey]
        # 排序结果：低费用在前
        assert res["suggestions"][0]["adjustment"]["reviewer_id"] == cheap
        # 配对改动数均为 1
        assert all(s["changed_pair_count"] == 1 for s in caps)
    finally:
        conn.close()


def test_suggestions_never_worsen_baseline() -> None:
    """入选建议的总缺口/主审缺口都不得高于基线（临时覆盖只放宽约束）。"""
    init_db(seed_demo=True)
    conn = _connect()
    try:
        res = suggest(conn)
        b = res["baseline_metrics"]
        for s in res["suggestions"]:
            assert s["metrics"]["unmet_people"] <= b["unmet_people"]
            assert s["metrics"]["lead_deficit"] <= b["lead_deficit"]
            assert (s["metrics"]["unmet_people"] < b["unmet_people"]
                    or s["metrics"]["lead_deficit"] < b["lead_deficit"])
    finally:
        conn.close()


def test_preview_is_strictly_read_only() -> None:
    """试算不改动 settings / reviewers / assignments / 锁定 / last_solution。"""
    conn = _connect()
    try:
        _reset(conn)
        set_setting(conn, "institution_cap", 1)
        m = _add_m(conn, "M", 2, leads=1)
        _add_r(conn, "a", 1, inst="A", can_lead=1)
        _add_r(conn, "b", 2, inst="A", can_lead=0)
        _add_r(conn, "c", 0, inst="B", can_lead=1)
        conn.commit()
        solve(conn)
        before = _snapshot(conn)

        res = suggest(conn)
        assert res["suggestions"]  # 确实有建议
        assert _snapshot(conn) == before

        # 再用临时覆盖直接调用求解内核：库表依然不变
        params = {k: get_setting(conn, k) for k in
                  ("mismatch_weight", "load_weight", "institution_cap")}
        compute_solution(conn, params["mismatch_weight"], params["load_weight"],
                         params["institution_cap"],
                         capacity_override={1: 99}, org_cap_override=9,
                         extra_lead_reviewers={2})
        assert _snapshot(conn) == before
    finally:
        conn.close()


def test_diff_and_gap_fields_consistency() -> None:
    """建议的 diff/受影响稿件/指标三者口径一致。"""
    conn = _connect()
    try:
        _reset(conn)
        m = _add_m(conn, "M", 2)
        _add_r(conn, "满", 0)
        _add_r(conn, "闲", 0)
        conn.commit()
        solve(conn)
        res = suggest(conn)
        b = res["baseline_metrics"]
        for s in res["suggestions"]:
            added = {(p["manuscript_id"], p["reviewer_id"]) for p in s["diff"]["added"]}
            removed = {(p["manuscript_id"], p["reviewer_id"]) for p in s["diff"]["removed"]}
            assert not (added & removed)
            # 受影响稿件集合 = diff 涉及稿件并集
            aff_from_diff = {m_id for m_id, _ in added} | {m_id for m_id, _ in removed}
            aff_listed = {g["manuscript_id"] for g in s["affected_manuscripts"]}
            assert aff_from_diff <= aff_listed
            # 逐稿缺口改善量合计 = 全局改善量
            assert sum(g["deficit_before"] - g["deficit_after"]
                       for g in s["affected_manuscripts"]) == s["deficit_reduction"]
            assert sum(g["lead_deficit_before"] - g["lead_deficit_after"]
                       for g in s["affected_manuscripts"]) == s["lead_deficit_reduction"]
            # delta 与指标一致
            assert s["delta"]["unmet_people"] == s["metrics"]["unmet_people"] - b["unmet_people"]
            assert s["delta"]["lead_deficit"] == s["metrics"]["lead_deficit"] - b["lead_deficit"]
            assert s["delta"]["total_cost"] == s["cost_delta"]
    finally:
        conn.close()


def test_api_layer() -> None:
    """接口层（安装 fastapi/pydantic 时）：200 与结构字段。"""
    try:
        from app.main import relief_suggestions
    except ModuleNotFoundError:
        print("  （跳过接口层测试：未安装 fastapi/pydantic）")
        return

    conn = _connect()
    try:
        _reset(conn)
        _add_m(conn, "M", 1)
        _add_r(conn, "r", 0)
        conn.commit()
        solve(conn)
        res = relief_suggestions(conn)
        assert res["kind"] == "gap-relief-suggestions"
        assert res["has_gap"] is True
        assert {"created_at", "candidate_count", "suggestions", "message",
                "baseline_metrics", "baseline_unmet", "params"} <= set(res)
        s0 = res["suggestions"][0]
        assert {"adjustment", "deficit_reduction", "lead_deficit_reduction",
                "changed_pair_count", "cost_delta", "fully_resolves",
                "affected_manuscripts", "diff", "warnings", "metrics"} <= set(s0)

        # 无缺口场景：接口正常返回空建议
        _reset(conn)
        _add_m(conn, "M", 1)
        _add_r(conn, "ok", 2)
        conn.commit()
        solve(conn)
        res2 = relief_suggestions(conn)
        assert res2["has_gap"] is False and res2["suggestions"] == []
    finally:
        conn.close()


if __name__ == "__main__":
    test_no_gap_returns_empty()
    test_capacity_plus_one_suggestion()
    test_institution_cap_relax_suggestion()
    test_institution_cap_disabled_not_enumerated()
    test_grant_lead_qualification_suggestion()
    test_already_lead_reviewer_not_enumerated()
    test_no_single_step_helps_empty_suggestions()
    test_ordering_deficit_then_lead_then_churn_then_cost()
    test_churn_tiebreak_by_changed_pairs_and_cost()
    test_suggestions_never_worsen_baseline()
    test_preview_is_strictly_read_only()
    test_diff_and_gap_fields_consistency()
    test_api_layer()
    print("所有缺口解困建议测试通过 ✅")
