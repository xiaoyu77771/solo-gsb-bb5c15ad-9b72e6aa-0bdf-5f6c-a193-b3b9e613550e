"""利益冲突自助申报功能测试（仅标准库，风格与 test_smoke 一致）。

运行：python -m tests.test_declarations

覆盖：
* 签发链接绑定签发时稿件清单版本，评审端只返回稿件标题；
* 提交在单事务内合并进冲突矩阵并立即失效；重复提交/清单变化 409 且不部分写入；
* 签发后稿件增删/标题变化 -> 提交 409；重新生成链接后恢复可提交；
* 勾选含锁定分配对应稿件 -> 整单 409 不写入，解锁后可重试；
* 管理端四态（未签发/待提交/已提交/已失效）、撤销待提交链接、重新签发顶替旧链接；
* 令牌仅保存哈希：数据库无明文令牌，任何响应不含哈希；
* 旧库迁移自动建表。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

# 测试前指定独立数据库
_tmp = tempfile.mkdtemp(prefix="review-decl-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app.db import _connect, init_db, transaction  # noqa: E402
from app import declarations as decl  # noqa: E402
from app.declarations import DeclarationError, hash_token  # noqa: E402


# ------------------------------------------------------------- 测试夹具
def _reset_fixture(conn) -> tuple[list[int], list[int]]:
    """清空并构造 3 篇稿件、2 名评审人；返回 (稿件IDs, 评审IDs)。"""
    with transaction(conn):
        conn.execute("DELETE FROM assignments")
        conn.execute("DELETE FROM conflicts")
        conn.execute("DELETE FROM conflict_declarations")
        conn.execute("DELETE FROM manuscripts")
        conn.execute("DELETE FROM reviewers")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers','conflict_declarations')")
    m_ids, r_ids = [], []
    for i, title in enumerate(["稿件甲", "稿件乙", "稿件丙"], 1):
        cur = conn.execute(
            "INSERT INTO manuscripts(title, topics, required_reviews) VALUES (?, '[]', 1)",
            (title,))
        m_ids.append(cur.lastrowid)
    for name in ["评审甲", "评审乙"]:
        cur = conn.execute(
            "INSERT INTO reviewers(name, topics, capacity) VALUES (?, '[]', 3)", (name,))
        r_ids.append(cur.lastrowid)
    conn.commit()
    return m_ids, r_ids


def _expect_409(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except DeclarationError as e:
        assert e.status_code == 409, e.message
        return e
    raise AssertionError("应当返回 409")


def _expect_404(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except DeclarationError as e:
        assert e.status_code == 404
        return e
    raise AssertionError("应当返回 404")


def _expect_400(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except DeclarationError as e:
        assert e.status_code == 400
        return e
    raise AssertionError("应当返回 400")


def _conflict_pairs(conn) -> set[tuple[int, int]]:
    return {(r["m_id"], r["r_id"]) for r in conn.execute("SELECT m_id, r_id FROM conflicts")}


def _status_of(conn, decl_id: int) -> str:
    return conn.execute(
        "SELECT status FROM conflict_declarations WHERE id=?", (decl_id,)).fetchone()["status"]


# ------------------------------------------------------------- 测试用例
def test_issue_binds_manifest_and_only_titles_public() -> None:
    conn = _connect()
    try:
        m_ids, r_ids = _reset_fixture(conn)
        decl_id, token, info = decl.issue_declaration(conn, r_ids[0])
        assert info["status"] == "pending"
        assert info["manifest_count"] == 3
        assert info["stale"] is False

        pub = decl.get_public_declaration(conn, token)
        assert pub["status"] == "pending" and pub["stale"] is False
        assert pub["reviewer_name"] == "评审甲"
        # 评审端只展示稿件标题（与回传用的 id），不含主题等其他信息
        assert [m["title"] for m in pub["manuscripts"]] == ["稿件甲", "稿件乙", "稿件丙"]
        assert all(set(m.keys()) == {"id", "title"} for m in pub["manuscripts"])

        # 未知令牌 -> 404
        _expect_404(decl.get_public_declaration, conn, "not-a-real-token")
        # 给不存在的评审人签发 -> 404
        _expect_404(decl.issue_declaration, conn, 999999)
    finally:
        conn.close()


def test_token_only_hash_stored_and_never_returned() -> None:
    conn = _connect()
    try:
        m_ids, r_ids = _reset_fixture(conn)
        _, token, info = decl.issue_declaration(conn, r_ids[0])
        # 数据库中只有 sha256 哈希，查不到明文
        row = conn.execute(
            "SELECT token_hash FROM conflict_declarations WHERE reviewer_id=?",
            (r_ids[0],)).fetchone()
        assert row["token_hash"] == hash_token(token)
        assert len(row["token_hash"]) == 64
        leak = conn.execute(
            "SELECT COUNT(*) c FROM conflict_declarations WHERE token_hash = ?",
            (token,)).fetchone()["c"]
        assert leak == 0
        # 序列化结果不含哈希/令牌
        assert "token_hash" not in json.dumps(info, ensure_ascii=False)
        assert "token" not in info
        listed = decl.list_declarations(conn)
        assert all("token_hash" not in d and "token" not in d for d in listed)
        # 404 错误消息不含哈希
        e = _expect_404(decl.get_public_declaration, conn, "wrong-token")
        assert hash_token(token) not in e.message
    finally:
        conn.close()


def test_submit_merges_and_consumes_token() -> None:
    conn = _connect()
    try:
        m_ids, r_ids = _reset_fixture(conn)
        # 预置一条管理员登记的冲突，验证“合并=并集、未勾选不删除”
        with transaction(conn):
            conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)",
                         (m_ids[0], r_ids[0]))
        _, token, _ = decl.issue_declaration(conn, r_ids[0])
        result = decl.submit_declaration(conn, token, [m_ids[1], m_ids[2]])
        assert result["submitted"] is True and result["merged_count"] == 2
        pairs = _conflict_pairs(conn)
        # 预置的 (M1,R1) 仍在；本次勾选并入
        assert pairs == {(m_ids[0], r_ids[0]), (m_ids[1], r_ids[0]), (m_ids[2], r_ids[0])}

        # 链接立即失效
        pub = decl.get_public_declaration(conn, token)
        assert pub["status"] == "submitted" and pub["submitted_at"]
        row = conn.execute(
            "SELECT status, submitted_conflicts FROM conflict_declarations WHERE token_hash=?",
            (hash_token(token),)).fetchone()
        assert row["status"] == "submitted"
        assert json.loads(row["submitted_conflicts"]) == [m_ids[1], m_ids[2]]
    finally:
        conn.close()


def test_duplicate_submit_409_no_partial_write() -> None:
    conn = _connect()
    try:
        m_ids, r_ids = _reset_fixture(conn)
        _, token, _ = decl.issue_declaration(conn, r_ids[0])
        decl.submit_declaration(conn, token, [m_ids[0]])
        before = _conflict_pairs(conn)
        # 重复提交：409，冲突矩阵无变化（不部分写入）
        e = _expect_409(decl.submit_declaration, conn, token, [m_ids[1], m_ids[2]])
        assert "重复提交" in e.message or "已提交" in e.message
        assert _conflict_pairs(conn) == before
        # 已提交链接不可撤销
        decl_id = conn.execute(
            "SELECT id FROM conflict_declarations WHERE token_hash=?",
            (hash_token(token),)).fetchone()["id"]
        _expect_409(decl.revoke_declaration, conn, decl_id)
    finally:
        conn.close()


def test_remerge_is_idempotent_union() -> None:
    """再次签发并提交相同冲突对：INSERT OR IGNORE 去重，merged=0 / duplicate=2。"""
    conn = _connect()
    try:
        m_ids, r_ids = _reset_fixture(conn)
        _, t1, _ = decl.issue_declaration(conn, r_ids[0])
        decl.submit_declaration(conn, t1, [m_ids[0], m_ids[1]])
        _, t2, _ = decl.issue_declaration(conn, r_ids[0])
        result = decl.submit_declaration(conn, t2, [m_ids[0], m_ids[1]])
        assert result["merged_count"] == 0 and result["duplicate_count"] == 2
        # 冲突矩阵仍只有两条，没有重复行
        rows = conn.execute(
            "SELECT COUNT(*) c FROM conflicts WHERE r_id=? AND m_id IN (?, ?)",
            (r_ids[0], m_ids[0], m_ids[1])).fetchone()["c"]
        assert rows == 2
    finally:
        conn.close()


def test_empty_submit_is_valid() -> None:
    """无冲突可勾选空列表：提交成功（合并 0 条），链接同样立即失效。"""
    conn = _connect()
    try:
        m_ids, r_ids = _reset_fixture(conn)
        _, token, _ = decl.issue_declaration(conn, r_ids[1])
        result = decl.submit_declaration(conn, token, [])
        assert result["merged_count"] == 0 and result["submitted"] is True
        assert _status_of(
            conn,
            conn.execute("SELECT id FROM conflict_declarations WHERE token_hash=?",
                         (hash_token(token),)).fetchone()["id"]) == "submitted"
        assert _conflict_pairs(conn) == set()
    finally:
        conn.close()


def test_stale_manifest_title_add_delete_409_no_write() -> None:
    conn = _connect()
    try:
        m_ids, r_ids = _reset_fixture(conn)

        # --- 标题变化
        _, token, info = decl.issue_declaration(conn, r_ids[0])
        with transaction(conn):
            conn.execute("UPDATE manuscripts SET title='改了标题' WHERE id=?", (m_ids[0],))
        pub = decl.get_public_declaration(conn, token)
        assert pub["stale"] is True
        before = _conflict_pairs(conn)
        e = _expect_409(decl.submit_declaration, conn, token, [m_ids[1]])
        assert "稿件清单已发生变化" in e.message
        assert _conflict_pairs(conn) == before
        assert _status_of(conn, info["id"]) == "pending"  # 未失效
        with transaction(conn):
            conn.execute("UPDATE manuscripts SET title='稿件甲' WHERE id=?", (m_ids[0],))

        # --- 新增稿件
        _, plain2, info2 = decl.issue_declaration(conn, r_ids[0])
        cur = conn.execute(
            "INSERT INTO manuscripts(title, topics, required_reviews) VALUES ('新增稿','[]',1)")
        new_id = cur.lastrowid
        conn.commit()
        assert decl.get_public_declaration(conn, plain2)["stale"] is True
        _expect_409(decl.submit_declaration, conn, plain2, [m_ids[0]])

        # --- 删除稿件
        _, plain3, _ = decl.issue_declaration(conn, r_ids[0])
        with transaction(conn):
            conn.execute("DELETE FROM manuscripts WHERE id=?", (new_id,))
        _expect_409(decl.submit_declaration, conn, plain3, [m_ids[0]])

        # 重新生成链接后清单恢复一致，可正常提交
        _, plain4, _ = decl.issue_declaration(conn, r_ids[0])
        assert decl.get_public_declaration(conn, plain4)["stale"] is False
        result = decl.submit_declaration(conn, plain4, [m_ids[2]])
        assert result["merged_count"] == 1
    finally:
        conn.close()


def test_locked_assignment_blocks_whole_submission() -> None:
    """勾选含锁定既有分配的稿件：整单 409、不部分写入，解锁后可重试。"""
    conn = _connect()
    try:
        m_ids, r_ids = _reset_fixture(conn)
        with transaction(conn):
            conn.execute("INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1)",
                         (m_ids[0], r_ids[0]))
        _, token, _ = decl.issue_declaration(conn, r_ids[0])
        before = _conflict_pairs(conn)
        e = _expect_409(decl.submit_declaration, conn, token, [m_ids[0], m_ids[1]])
        assert "锁定" in e.message and "稿件甲" in e.message
        assert _conflict_pairs(conn) == before  # M2 也没有被写入（无部分写入）
        # 链接仍待提交
        assert decl.get_public_declaration(conn, token)["status"] == "pending"
        # 解锁后重试成功
        with transaction(conn):
            conn.execute("DELETE FROM assignments WHERE m_id=? AND r_id=?",
                         (m_ids[0], r_ids[0]))
        result = decl.submit_declaration(conn, token, [m_ids[0], m_ids[1]])
        assert result["merged_count"] == 2
    finally:
        conn.close()


def test_unknown_manuscript_id_400() -> None:
    conn = _connect()
    try:
        m_ids, r_ids = _reset_fixture(conn)
        _, token, _ = decl.issue_declaration(conn, r_ids[0])
        e = _expect_400(decl.submit_declaration, conn, token, [m_ids[0], 999999])
        assert "999999" in e.message
        # 未写入、链接仍有效
        assert _conflict_pairs(conn) == set()
        assert decl.get_public_declaration(conn, token)["status"] == "pending"
    finally:
        conn.close()


def test_reissue_revokes_old_pending_and_admin_statuses() -> None:
    conn = _connect()
    try:
        m_ids, r_ids = _reset_fixture(conn)
        _, t1, i1 = decl.issue_declaration(conn, r_ids[0])
        _, t2, i2 = decl.issue_declaration(conn, r_ids[0])  # 重新签发顶替
        # 旧链接已失效，原因是“重新签发顶替”；新链接待提交
        assert decl.get_public_declaration(conn, t1)["status"] == "revoked"
        assert decl.get_public_declaration(conn, t2)["status"] == "pending"
        old = conn.execute("SELECT * FROM conflict_declarations WHERE id=?", (i1["id"],)).fetchone()
        assert old["status"] == "revoked" and old["revoked_reason"] == "重新签发顶替"

        # 管理端列表不含哈希
        listed = {d["id"]: d for d in decl.list_declarations(conn)}
        assert all("token_hash" not in json.dumps(d, ensure_ascii=False) for d in listed.values())
        assert listed[i1["id"]]["status"] == "revoked"
        assert listed[i2["id"]]["status"] == "pending" and listed[i2["id"]]["stale"] is False

        # 撤销待提交链接
        decl.revoke_declaration(conn, i2["id"])
        assert decl.get_public_declaration(conn, t2)["status"] == "revoked"
        # 重复撤销 -> 409；撤销不存在的链接 -> 404
        _expect_409(decl.revoke_declaration, conn, i2["id"])
        _expect_404(decl.revoke_declaration, conn, 999999)

        # 待提交链接在清单变化时带 stale 标记
        _, t3, i3 = decl.issue_declaration(conn, r_ids[1])
        with transaction(conn):
            conn.execute("UPDATE manuscripts SET title='变' WHERE id=?", (m_ids[0],))
        d3_listed = [d for d in decl.list_declarations(conn) if d["id"] == i3["id"]][0]
        assert d3_listed["stale"] is True
        # 标题恢复后重新签发、空提交 -> 已提交
        with transaction(conn):
            conn.execute("UPDATE manuscripts SET title='稿件甲' WHERE id=?", (m_ids[0],))
        _, t4, i4 = decl.issue_declaration(conn, r_ids[1])
        decl.submit_declaration(conn, t4, [])
        assert [d for d in decl.list_declarations(conn) if d["id"] == i4["id"]][0]["status"] == "submitted"
        assert decl.get_public_declaration(conn, t3)["status"] == "revoked"  # 被 t4 顶替
    finally:
        conn.close()


def test_admin_list_reports_four_statuses() -> None:
    """每位评审人的最新一条：未签发 / 待提交 / 已提交 / 已失效 四态可区分。"""
    conn = _connect()
    try:
        m_ids, r_ids = _reset_fixture(conn)
        # 追加两名评审：r_pending 待提交、r_revoked 被撤销
        cur = conn.execute(
            "INSERT INTO reviewers(name, topics, capacity) VALUES ('评审丙','[]',3)")
        r_pending = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO reviewers(name, topics, capacity) VALUES ('评审丁','[]',3)")
        r_revoked = cur.lastrowid
        # 从未签发的评审（“未签发”状态由前端在无任何记录时呈现）
        cur = conn.execute(
            "INSERT INTO reviewers(name, topics, capacity) VALUES ('评审戊','[]',3)")
        r_none = cur.lastrowid
        conn.commit()

        _, t_pending, _ = decl.issue_declaration(conn, r_pending)
        _, t_sub, _ = decl.issue_declaration(conn, r_ids[1])
        decl.submit_declaration(conn, t_sub, [m_ids[0]])
        _, t_rev, i_rev = decl.issue_declaration(conn, r_revoked)
        decl.revoke_declaration(conn, i_rev["id"])

        latest = {}
        for d in decl.list_declarations(conn):
            if d["reviewer_id"] not in latest or d["id"] > latest[d["reviewer_id"]]["id"]:
                latest[d["reviewer_id"]] = d
        assert latest[r_pending]["status"] == "pending"
        assert latest[r_ids[1]]["status"] == "submitted"
        assert latest[r_revoked]["status"] == "revoked"
        # 从未签发的评审在管理端申报列表中没有任何记录（前端据此显示“未签发”）
        assert r_none not in latest
    finally:
        conn.close()


def test_cascade_delete_reviewer_removes_declarations() -> None:
    """删除评审人：申报链接随外键级联删除（不产生孤儿记录）。"""
    conn = _connect()
    try:
        m_ids, r_ids = _reset_fixture(conn)
        _, token, _ = decl.issue_declaration(conn, r_ids[0])
        with transaction(conn):
            conn.execute("DELETE FROM reviewers WHERE id=?", (r_ids[0],))
        n = conn.execute("SELECT COUNT(*) c FROM conflict_declarations").fetchone()["c"]
        # 其余评审没有链接
        assert n == 0
    finally:
        conn.close()


def test_migration_adds_table_on_old_db() -> None:
    """旧库（无 conflict_declarations 表）启动后自动建表且既有数据保留。"""
    path = os.path.join(tempfile.mkdtemp(prefix="review-decl-mig-"), "old.db")
    from app import db
    old_path = db.DB_PATH
    db.DB_PATH = path
    try:
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE reviewers (id INTEGER PRIMARY KEY, name TEXT, topics TEXT, capacity INTEGER, institution TEXT, can_lead INTEGER)")
        c.execute("INSERT INTO reviewers(name, topics, capacity, institution, can_lead) VALUES ('老评审','[]',3,'',0)")
        c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE manuscripts (id INTEGER PRIMARY KEY, title TEXT, topics TEXT, required_reviews INTEGER, required_leads INTEGER)")
        c.execute("INSERT INTO manuscripts(title, topics, required_reviews, required_leads) VALUES ('老稿','[]',3,0)")
        c.execute("CREATE TABLE conflicts (m_id INTEGER, r_id INTEGER, PRIMARY KEY(m_id,r_id))")
        c.execute("CREATE TABLE assignments (id INTEGER PRIMARY KEY, m_id INTEGER, r_id INTEGER, locked INTEGER)")
        c.commit()
        c.close()

        init_db(seed_demo=False)
        c2 = sqlite3.connect(path)
        c2.row_factory = sqlite3.Row
        # 表与部分唯一索引存在，旧数据保留
        tables = {r[0] for r in c2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "conflict_declarations" in tables
        idx = c2.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_declarations_one_pending_per_reviewer'"
        ).fetchone()[0]
        assert "UNIQUE INDEX" in idx
        assert c2.execute("SELECT name FROM reviewers WHERE name='老评审'").fetchone()["name"] == "老评审"
        # 迁移后可在旧库上正常签发
        c2.close()
        conn = _connect()
        try:
            _, token, info = decl.issue_declaration(conn, 1)
            assert info["manifest_count"] == 1
            assert info["status"] == "pending"
        finally:
            conn.close()
    finally:
        db.DB_PATH = old_path


# ------------------------------------------------------------- 入口
if __name__ == "__main__":
    init_db(seed_demo=True)
    test_issue_binds_manifest_and_only_titles_public()
    test_token_only_hash_stored_and_never_returned()
    test_submit_merges_and_consumes_token()
    test_duplicate_submit_409_no_partial_write()
    test_remerge_is_idempotent_union()
    test_empty_submit_is_valid()
    test_stale_manifest_title_add_delete_409_no_write()
    test_locked_assignment_blocks_whole_submission()
    test_unknown_manuscript_id_400()
    test_reissue_revokes_old_pending_and_admin_statuses()
    test_admin_list_reports_four_statuses()
    test_cascade_delete_reviewer_removes_declarations()
    test_migration_adds_table_on_old_db()
    print("所有利益冲突自助申报测试通过 ✅")
