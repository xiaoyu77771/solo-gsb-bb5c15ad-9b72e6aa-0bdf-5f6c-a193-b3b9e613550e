"""利益冲突自助申报：一次性申报链接的签发、查看、提交与撤销。

安全要点
========
* 令牌（token）使用 ``secrets.token_urlsafe(32)`` 生成，仅在签发时返回明文一次；
  数据库只保存其 SHA-256 哈希（``token_hash``）。
* 任何接口响应（含管理端列表、错误详情、日志消息）都不得回传 token 哈希或明文，
  对外只用申报记录 ID 与稿件标题。
* 提交在**单个事务**（``BEGIN IMMEDIATE``）内完成“校验 + 合并冲突 + 令牌失效”，
  重复提交、清单版本不符、参数非法一律回滚，绝不部分写入。

稿件清单版本
============
签发时对当前全部稿件取 (id, title) 快照（按 id 升序）并计算版本哈希；
提交时重新计算当前版本哈希：若期间稿件有增删或标题变化，则版本不一致，
提交返回 409 且不写入，管理员需重新生成链接。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime

from .db import transaction

STATUS_PENDING = "pending"
STATUS_SUBMITTED = "submitted"
STATUS_REVOKED = "revoked"


class DeclarationError(Exception):
    """业务异常：status_code 为应返回的 HTTP 状态码；message 中不得包含令牌/哈希。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ------------------------------------------------------------- 令牌与清单版本
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def current_manifest(conn: sqlite3.Connection) -> list[dict]:
    """当前稿件清单快照：仅含 id 与标题（评审端只展示标题），按 id 升序。"""
    return [
        {"id": r["id"], "title": r["title"]}
        for r in conn.execute("SELECT id, title FROM manuscripts ORDER BY id")
    ]


def hash_manifest(manifest: list[dict]) -> str:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- 管理端
def issue_declaration(conn: sqlite3.Connection, reviewer_id: int) -> tuple[int, str, dict]:
    """为评审人生成一次性申报链接。

    若该评审人已有待提交链接，旧链接立即失效（status=revoked，
    revoked_reason='重新签发顶替'），由新链接顶替。
    返回 (申报记录 ID, 明文令牌, 新记录公开信息)；明文令牌仅此一次可获取。
    """
    reviewer = conn.execute(
        "SELECT id, name FROM reviewers WHERE id = ?", (reviewer_id,)).fetchone()
    if reviewer is None:
        raise DeclarationError(404, f"评审人 #{reviewer_id} 不存在")

    token = secrets.token_urlsafe(32)
    manifest = current_manifest(conn)
    now = _now()
    with transaction(conn):
        # 同一评审人的旧待提交链接：先撤销（满足“每位评审至多一个待提交链接”）
        conn.execute(
            "UPDATE conflict_declarations SET status = ?, revoked_reason = ?, revoked_at = ? "
            "WHERE reviewer_id = ? AND status = ?",
            (STATUS_REVOKED, "重新签发顶替", now, reviewer_id, STATUS_PENDING),
        )
        cur = conn.execute(
            "INSERT INTO conflict_declarations"
            "(reviewer_id, token_hash, manifest, manifest_hash, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (reviewer_id, hash_token(token),
             json.dumps(manifest, ensure_ascii=False), hash_manifest(manifest),
             STATUS_PENDING, now),
        )
        decl_id = cur.lastrowid
    return decl_id, token, serialize_declaration(get_declaration(conn, decl_id))


def revoke_declaration(conn: sqlite3.Connection, decl_id: int) -> dict:
    """管理员撤销待提交链接；已提交/已失效的链接返回 409。"""
    row = conn.execute(
        "SELECT id, status FROM conflict_declarations WHERE id = ?", (decl_id,)).fetchone()
    if row is None:
        raise DeclarationError(404, f"申报链接 #{decl_id} 不存在")
    if row["status"] != STATUS_PENDING:
        raise DeclarationError(
            409, f"申报链接 #{decl_id} 当前状态为「{_status_label(row['status'])}」，不可撤销。")
    with transaction(conn):
        conn.execute(
            "UPDATE conflict_declarations SET status = ?, revoked_reason = ?, revoked_at = ? "
            "WHERE id = ? AND status = ?",
            (STATUS_REVOKED, "管理员撤销", _now(), decl_id, STATUS_PENDING),
        )
    return serialize_declaration(get_declaration(conn, decl_id))


def get_declaration(conn: sqlite3.Connection, decl_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM conflict_declarations WHERE id = ?", (decl_id,)).fetchone()
    if row is None:
        raise DeclarationError(404, f"申报链接 #{decl_id} 不存在")
    return row


def serialize_declaration(row: sqlite3.Row, stale: bool | None = None) -> dict:
    """管理端可见字段；**绝不包含 token_hash / 明文令牌**。"""
    if stale is None:
        stale = False
    return {
        "id": row["id"],
        "reviewer_id": row["reviewer_id"],
        "status": row["status"],
        "stale": stale,  # 仅待提交链接有意义：签发时稿件清单是否已发生变化
        "manifest_count": len(json.loads(row["manifest"])),
        "submitted_conflicts": (
            json.loads(row["submitted_conflicts"])
            if row["submitted_conflicts"] is not None else None
        ),
        "revoked_reason": row["revoked_reason"],
        "created_at": row["created_at"],
        "submitted_at": row["submitted_at"],
        "revoked_at": row["revoked_at"],
    }


def list_declarations(conn: sqlite3.Connection) -> list[dict]:
    """全部申报链接（管理端）。待提交链接附带 stale 标记（清单是否已变化）。"""
    current_hash = hash_manifest(current_manifest(conn))
    result = []
    for row in conn.execute(
            "SELECT * FROM conflict_declarations ORDER BY reviewer_id, id"):
        stale = (row["status"] == STATUS_PENDING and row["manifest_hash"] != current_hash)
        result.append(serialize_declaration(row, stale=stale))
    return result


# ----------------------------------------------------------------- 评审端（公开）
def _get_by_token_hash(conn: sqlite3.Connection, token_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM conflict_declarations WHERE token_hash = ?",
        (token_hash,)).fetchone()


def get_public_declaration(conn: sqlite3.Connection, token: str) -> dict:
    """评审人打开申报链接：返回评审人姓名、状态与（待提交时）签发清单中的稿件标题。"""
    row = _get_by_token_hash(conn, hash_token(token))
    if row is None:
        raise DeclarationError(404, "申报链接不存在或已失效，请联系管理员重新获取。")

    reviewer = conn.execute(
        "SELECT name FROM reviewers WHERE id = ?", (row["reviewer_id"],)).fetchone()
    manifest = json.loads(row["manifest"])
    if row["status"] == STATUS_PENDING:
        current_hash = hash_manifest(current_manifest(conn))
        return {
            "status": STATUS_PENDING,
            "reviewer_name": reviewer["name"] if reviewer else f"#{row['reviewer_id']}",
            "stale": row["manifest_hash"] != current_hash,
            "manuscript_count": len(manifest),
            # 评审端只展示稿件标题（附带 id 仅用于回传勾选，不展示任何其他稿件信息）
            "manuscripts": manifest,
        }
    return {
        "status": row["status"],
        "reviewer_name": reviewer["name"] if reviewer else f"#{row['reviewer_id']}",
        "manuscript_count": len(manifest),
        "submitted_at": row["submitted_at"],
        "revoked_at": row["revoked_at"],
    }


def submit_declaration(conn: sqlite3.Connection, token: str, conflict_ids: list[int]) -> dict:
    """评审人一次性提交：单事务内校验清单版本、合并冲突矩阵并立即失效链接。

    成功返回 {"submitted": True, "merged_count": n, "duplicate_count": k}。
    任何失败都在事务内回滚（链接保持 pending 可重试），绝不部分写入。
    """
    token_hash = hash_token(token)
    now = _now()

    # BEGIN IMMEDIATE：立即获取写锁，把“校验 + 写入 + 失效”串行化，
    # 双击/并发重复提交时后者要么等待后看到 status=submitted（409），不会重复合并。
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _get_by_token_hash(conn, token_hash)
        if row is None:
            conn.execute("ROLLBACK")
            raise DeclarationError(404, "申报链接不存在或已失效，请联系管理员重新获取。")
        if row["status"] != STATUS_PENDING:
            conn.execute("ROLLBACK")
            raise DeclarationError(
                409,
                "该申报链接已提交或已失效，不能重复提交；如需修改请联系管理员重新生成链接。")

        # 稿件清单版本校验：签发后稿件有增删或标题变化 → 409 不写入
        manifest = json.loads(row["manifest"])
        snapshot = current_manifest(conn)
        if hash_manifest(snapshot) != row["manifest_hash"]:
            conn.execute("ROLLBACK")
            raise DeclarationError(
                409,
                "签发链接后稿件清单已发生变化（有稿件新增、删除或标题修改），"
                "为避免漏报本次提交未写入；请联系管理员重新生成申报链接。")

        manifest_ids = {m["id"] for m in manifest}
        unknown = sorted(set(conflict_ids) - manifest_ids)
        if unknown:
            conn.execute("ROLLBACK")
            raise DeclarationError(
                400,
                "提交包含不属于本链接签发清单的稿件 ID："
                + "、".join(f"#{i}" for i in unknown) + "，请刷新页面后重试。")

        reviewer_id = row["reviewer_id"]

        # 已勾选冲突若对应“锁定的既有分配”，登记冲突会与锁定矛盾：
        # 整单 409 拒绝（管理员需先解锁），保证不出现部分写入。
        if conflict_ids:
            placeholders = ",".join("?" for _ in conflict_ids)
            locked = conn.execute(
                f"SELECT m_id FROM assignments "
                f"WHERE r_id = ? AND locked = 1 AND m_id IN ({placeholders})",
                (reviewer_id, *conflict_ids),
            ).fetchall()
            if locked:
                titles = {m["id"]: m["title"] for m in snapshot}
                locked_titles = "、".join(
                    f"《{titles.get(r['m_id'], r['m_id'])}》" for r in locked)
                conn.execute("ROLLBACK")
                raise DeclarationError(
                    409,
                    f"勾选稿件 {locked_titles} 存在锁定的既有分配，不能登记冲突；"
                    f"请联系管理员先解锁后再重新提交，本次未写入。")

        # 合并进现有冲突矩阵：勾选 = 并入（union），未勾选不会删除既有冲突。
        # INSERT OR IGNORE 利用 (m_id, r_id) 主键去重，保证幂等合并。
        merged = 0
        for m_id in set(conflict_ids):
            cur = conn.execute(
                "INSERT OR IGNORE INTO conflicts(m_id, r_id) VALUES (?, ?)",
                (m_id, reviewer_id))
            merged += cur.rowcount or 0
        duplicate_count = len(set(conflict_ids)) - merged

        # 立即失效：一次性链接
        updated = conn.execute(
            "UPDATE conflict_declarations SET status = ?, submitted_conflicts = ?, submitted_at = ? "
            "WHERE id = ? AND status = ?",
            (STATUS_SUBMITTED,
             json.dumps(sorted(set(conflict_ids))), now, row["id"], STATUS_PENDING),
        )
        if updated.rowcount != 1:
            # 极端并发下被并发提交抢先：回滚全部写入（含冲突合并），返回 409
            conn.execute("ROLLBACK")
            raise DeclarationError(409, "申报链接已被提交，不能重复提交。")
        conn.execute("COMMIT")
    except DeclarationError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {
        "submitted": True,
        "merged_count": merged,
        "duplicate_count": duplicate_count,
        "submitted_at": now,
    }


def _status_label(status: str) -> str:
    return {
        STATUS_PENDING: "待提交",
        STATUS_SUBMITTED: "已提交",
        STATUS_REVOKED: "已失效",
    }.get(status, status)
