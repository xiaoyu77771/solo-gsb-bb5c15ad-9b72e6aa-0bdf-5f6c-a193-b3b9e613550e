"""SQLite 数据访问层。

单文件 SQLite，连接通过 FastAPI 依赖注入获取。
主题以 JSON 数组字符串存储（如 '["机器学习", "NLP"]'）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "review.db"))

DEFAULTS = {
    "default_required_reviews": 3,   # 新建稿件默认所需评审数
    "default_capacity": 3,           # 新建评审人默认个人容量
    "mismatch_weight": 10,           # 主题不匹配单位费用
    "load_weight": 2,                # 负载边际递增单位费用
    "institution_cap": 0,            # 每篇稿件同机构最多人数（0 关闭机构多样性约束）
    "last_solution": None,           # 最近一次求解结果（JSON）
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发提交（如同一直连令牌被双击）时让第二个写事务等待而不是立刻报 locked
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_conn():
    """FastAPI 依赖：每次请求一个连接。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS manuscripts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    topics           TEXT NOT NULL DEFAULT '[]',
    required_reviews INTEGER NOT NULL DEFAULT 3,
    required_leads   INTEGER NOT NULL DEFAULT 0   -- 需主审人数（0~2 且不超过所需评审数；0 关闭主审约束）
);

CREATE TABLE IF NOT EXISTS reviewers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    topics      TEXT NOT NULL DEFAULT '[]',
    capacity    INTEGER NOT NULL DEFAULT 3,
    institution TEXT NOT NULL DEFAULT '',  -- 可选所属机构；空串表示未填写（不计入机构上限）
    can_lead    INTEGER NOT NULL DEFAULT 0  -- 是否可任主审（主审资格硬约束）
);

-- 作者冲突：稿件 m_id 与评审人 r_id 之间绝不允许出现分配边
CREATE TABLE IF NOT EXISTS conflicts (
    m_id INTEGER NOT NULL REFERENCES manuscripts(id) ON DELETE CASCADE,
    r_id INTEGER NOT NULL REFERENCES reviewers(id) ON DELETE CASCADE,
    PRIMARY KEY (m_id, r_id)
);

-- 既有分配（求解结果或锁定记录）
CREATE TABLE IF NOT EXISTS assignments (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    m_id   INTEGER NOT NULL REFERENCES manuscripts(id) ON DELETE CASCADE,
    r_id   INTEGER NOT NULL REFERENCES reviewers(id) ON DELETE CASCADE,
    locked INTEGER NOT NULL DEFAULT 0,
    UNIQUE (m_id, r_id)
);

-- 利益冲突自助申报：管理员为评审人生成的一次性申报链接
-- token 仅以 SHA-256 哈希存储（token_hash），明文令牌只在签发响应中出现一次；
-- manifest 绑定签发时的稿件清单（id+标题）与版本哈希 manifest_hash，
-- 提交时当前清单版本与之不符（稿件增删或标题变化）一律 409 且不写入。
CREATE TABLE IF NOT EXISTS conflict_declarations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewer_id      INTEGER NOT NULL REFERENCES reviewers(id) ON DELETE CASCADE,
    token_hash       TEXT NOT NULL UNIQUE,        -- 令牌的 SHA-256 哈希（hex），接口/日志不回传
    manifest         TEXT NOT NULL DEFAULT '[]',  -- 签发时稿件清单快照 [{"id","title"}]（JSON）
    manifest_hash    TEXT NOT NULL,               -- 签发时稿件清单版本哈希（规范化 JSON 的 sha256）
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','submitted','revoked')),
    submitted_conflicts TEXT,                     -- 提交时勾选的稿件 ID 列表（JSON，仅提交后写入）
    revoked_reason   TEXT,                        -- 撤销原因（管理员撤销 / 重新签发顶替）
    created_at       TEXT NOT NULL,
    submitted_at     TEXT,
    revoked_at       TEXT
);

-- 每位评审人至多存在一个待提交链接：重新签发会把旧链接顶替为 revoked
CREATE UNIQUE INDEX IF NOT EXISTS idx_declarations_one_pending_per_reviewer
    ON conflict_declarations(reviewer_id) WHERE status = 'pending';

-- 分配批次审计：成功求解或锁定变更后，管理员可创建的**不可变**批次快照。
-- 快照正文 snapshot（JSON）记录当时的稿件/评审人实体信息、稿件—评审人配对
-- （含主审标记 is_lead 与锁定状态 locked）、冲突约束、求解参数（β/α/γ）、
-- 上次求解指标摘要；params_hash 为快照规范化 JSON 的 SHA-256（完整性校验）。
-- kind='rollback' 的批次是“按某历史批次原子回滚”后自动写入的新批次
-- （rollback_of 指向来源批次）；回滚只新增批次，绝不改写或删除历史批次。
CREATE TABLE IF NOT EXISTS assignment_batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'snapshot'
                   CHECK (kind IN ('snapshot','rollback')),
    note           TEXT NOT NULL DEFAULT '',   -- 管理员备注（rollback 批次记录回滚说明）
    snapshot       TEXT NOT NULL,              -- 不可变快照正文（JSON）
    params_hash    TEXT NOT NULL,               -- 快照完整性哈希（SHA-256 hex）
    rollback_of    INTEGER REFERENCES assignment_batches(id),  -- 仅 rollback 批次：来源批次
    pair_count     INTEGER NOT NULL DEFAULT 0,
    locked_count   INTEGER NOT NULL DEFAULT 0,
    lead_count     INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0
);
"""


def init_db(seed_demo: bool = True) -> None:
    """建库建表、写入默认设置；空库时可选择载入演示数据。"""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        for key, value in DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, None if value is None else str(value)),
            )
        conn.commit()

        if seed_demo and conn.execute("SELECT COUNT(*) AS c FROM manuscripts").fetchone()["c"] == 0:
            seed_demo_data(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """对既有数据库做幂等的轻量迁移（保留现有数据）。

    - reviewers.institution：评审人可选机构（旧库补列，默认空串=未填写）。
    - reviewers.can_lead / manuscripts.required_leads：主审资格硬约束
      （旧库补列，默认 0=不可任主审 / 不需主审，即该约束默认关闭）。
    - conflict_declarations：利益冲突自助申报链接表（含 token 哈希、
      签发稿件清单快照与版本哈希、状态）。新表由 SCHEMA 中的
      CREATE TABLE IF NOT EXISTS 在建库脚本里幂等创建，旧库启动时自动获得，
      这里再做一次存在性兜底（针对仅执行迁移而不执行完整 SCHEMA 的场景）。
    - assignment_batches：分配批次审计表（不可变快照 + 回滚来源批次）。
      同样由 SCHEMA 幂等创建，这里再做存在性兜底。
    新版本引入的设置项由 init_db 中的 INSERT OR IGNORE 补默认值。
    """
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(reviewers)")}
    if "institution" not in rcols:
        conn.execute("ALTER TABLE reviewers ADD COLUMN institution TEXT NOT NULL DEFAULT ''")
    if "can_lead" not in rcols:
        conn.execute("ALTER TABLE reviewers ADD COLUMN can_lead INTEGER NOT NULL DEFAULT 0")
    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(manuscripts)")}
    if "required_leads" not in mcols:
        conn.execute("ALTER TABLE manuscripts ADD COLUMN required_leads INTEGER NOT NULL DEFAULT 0")
    # 申报链接表幂等兜底：与 SCHEMA 中定义保持一致
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "conflict_declarations" not in tables:
        conn.executescript(
            """
            CREATE TABLE conflict_declarations (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                reviewer_id      INTEGER NOT NULL REFERENCES reviewers(id) ON DELETE CASCADE,
                token_hash       TEXT NOT NULL UNIQUE,
                manifest         TEXT NOT NULL DEFAULT '[]',
                manifest_hash    TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending','submitted','revoked')),
                submitted_conflicts TEXT,
                revoked_reason   TEXT,
                created_at       TEXT NOT NULL,
                submitted_at     TEXT,
                revoked_at       TEXT
            );
            CREATE UNIQUE INDEX idx_declarations_one_pending_per_reviewer
                ON conflict_declarations(reviewer_id) WHERE status = 'pending';
            """)
    # 分配批次审计表幂等兜底：与 SCHEMA 中定义保持一致
    if "assignment_batches" not in tables:
        conn.executescript(
            """
            CREATE TABLE assignment_batches (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at     TEXT NOT NULL,
                kind           TEXT NOT NULL DEFAULT 'snapshot'
                               CHECK (kind IN ('snapshot','rollback')),
                note           TEXT NOT NULL DEFAULT '',
                snapshot       TEXT NOT NULL,
                params_hash    TEXT NOT NULL,
                rollback_of    INTEGER REFERENCES assignment_batches(id),
                pair_count     INTEGER NOT NULL DEFAULT 0,
                locked_count   INTEGER NOT NULL DEFAULT 0,
                lead_count     INTEGER NOT NULL DEFAULT 0,
                conflict_count INTEGER NOT NULL DEFAULT 0
            );
            """)


def get_setting(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] is None:
        return DEFAULTS.get(key)
    if key in ("default_required_reviews", "default_capacity",
               "mismatch_weight", "load_weight", "institution_cap"):
        return int(row["value"])
    if key == "last_solution":
        return json.loads(row["value"])
    return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, None if value is None else str(value)),
    )


def normalize_topics(raw: str | list[str]) -> list[str]:
    """将逗号/顿号分隔的字符串或列表规范化为去空白、去重、保序的小写主题列表。"""
    if isinstance(raw, list):
        items = raw
    else:
        items = raw.replace("、", ",").replace("，", ",").split(",")
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        t = item.strip().lower()
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def normalize_institution(raw: str | None) -> str:
    """机构为可选自由文本：仅去除首尾空白；空串表示未填写（不计入机构上限）。"""
    return (raw or "").strip()


def seed_demo_data(conn: sqlite3.Connection) -> None:
    """载入一组用于快速体验的演示数据（含一个容量不足样例）。"""
    ms = [
        # (标题, 主题, 所需评审数, 需主审人数)
        ("基于深度学习的机器翻译", ["机器学习", "nlp"], 3, 1),
        ("图神经网络推荐系统", ["机器学习", "推荐系统"], 3, 1),
        ("分布式数据库一致性协议", ["数据库", "分布式系统"], 3, 1),
        ("低功耗边缘计算架构", ["体系结构", "分布式系统"], 3, 1),
        ("大模型安全对齐", ["nlp", "安全"], 3, 1),
    ]
    rs = [
        # 机构仅为演示机构多样性约束：清华 3 人、北大 1 人、中科院 1 人；
        # 孙老师未填机构，不受“每篇同机构最多人数”限制（但其容量为 0）。
        # 张教授、李研究员、钱教授标记“可任主审”，演示主审资格硬约束。
        ("张教授", ["机器学习", "nlp"], 3, "清华大学", 1),
        ("李研究员", ["nlp", "安全"], 3, "清华大学", 1),
        # 王老师只擅长数据库（不含“分布式系统”），且与 M4 冲突：
        # 于是 M4（体系结构/分布式系统）只有钱教授 1 名相容评审，
        # 钱教授容量仅 3、需求 3，在机构上限下仍只够 1 名，演示“未满足稿件与原因”。
        ("王老师", ["数据库"], 3, "北京大学", 0),
        ("赵博士", ["推荐系统", "机器学习"], 2, "清华大学", 0),
        ("钱教授", ["体系结构", "分布式系统"], 3, "中科院", 1),
        # 孙老师容量 0：全部容量之和 14 < 需求之和 15，形成全局性容量不足
        ("孙老师", ["安全"], 0, "", 0),
    ]
    with transaction(conn):
        # 若在已有库上重新载入演示数据，重置自增序列，保证下面的 ID 引用确定。
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
        m_ids: list[int] = []
        for title, topics, req, leads in ms:
            cur = conn.execute(
                "INSERT INTO manuscripts(title, topics, required_reviews, required_leads) "
                "VALUES (?, ?, ?, ?)",
                (title, json.dumps(topics, ensure_ascii=False), req, leads),
            )
            m_ids.append(cur.lastrowid)
        r_ids: list[int] = []
        for name, topics, cap, institution, can_lead in rs:
            cur = conn.execute(
                "INSERT INTO reviewers(name, topics, capacity, institution, can_lead) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, json.dumps(topics, ensure_ascii=False), cap, institution, can_lead),
            )
            r_ids.append(cur.lastrowid)
        # M4（体系结构/分布式）与除钱教授外的所有评审人冲突，
        # 形成“无冲突且相容的评审仅 1 名”的未满足样例。
        m4 = m_ids[3]
        conflicts = [(m4, rid) for rid in r_ids if rid != r_ids[4]]
        for m_id, r_id in conflicts:
            conn.execute("INSERT OR IGNORE INTO conflicts(m_id, r_id) VALUES (?, ?)", (m_id, r_id))
        # 演示机构多样性约束：每篇稿件同机构最多 2 人。
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('institution_cap', '2') "
            "ON CONFLICT(key) DO UPDATE SET value = '2'"
        )
