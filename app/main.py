"""FastAPI 入口：REST API + 静态前端托管。

启动时自动建库（含演示数据），因此
`docker compose up --build` 一条命令即可完成依赖安装与数据库初始化。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .db import get_conn, get_setting, normalize_institution, normalize_topics, seed_demo_data, set_setting, transaction
from .declarations import DeclarationError
from . import declarations as decl_svc
from .sandbox import SandboxError
from . import sandbox as sandbox_svc
from .backfill import BackfillError
from . import backfill as backfill_svc
from . import relief as relief_svc
from .schemas import (
    BackfillAdoptIn,
    BackfillPreviewIn,
    ConflictIn,
    DeclarationSubmitIn,
    LockIn,
    ManuscriptIn,
    ManuscriptPatch,
    ReviewerIn,
    ReviewerPatch,
    SandboxAdoptIn,
    SandboxPreviewIn,
    SettingsIn,
)
from .solver import institution_lock_exceeds, solve

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 依赖已在镜像构建/本地安装阶段就绪；这里完成数据库初始化
    db.init_db(seed_demo=os.environ.get("SEED_DEMO", "1") != "0")
    yield


app = FastAPI(title="匿名评审分配系统", lifespan=lifespan)


@app.exception_handler(DeclarationError)
def declaration_error_handler(request: Request, exc: DeclarationError):
    # 错误消息只含申报记录 ID / 稿件标题，绝不回传令牌或其哈希
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(SandboxError)
def sandbox_error_handler(request: Request, exc: SandboxError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(BackfillError)
def backfill_error_handler(request: Request, exc: BackfillError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


# ------------------------------------------------------------- 状态序列化
def build_state(conn: sqlite3.Connection) -> dict:
    def load_manuscripts():
        return [
            {"id": r["id"], "title": r["title"],
             "topics": json.loads(r["topics"]),
             "required_reviews": r["required_reviews"],
             "required_leads": r["required_leads"]}
            for r in conn.execute("SELECT * FROM manuscripts ORDER BY id").fetchall()
        ]

    def load_reviewers():
        return [
            {"id": r["id"], "name": r["name"],
             "topics": json.loads(r["topics"]), "capacity": r["capacity"],
             "institution": r["institution"] or "",
             "can_lead": bool(r["can_lead"])}
            for r in conn.execute("SELECT * FROM reviewers ORDER BY id").fetchall()
        ]

    assignments = [
        {"id": r["id"], "manuscript_id": r["m_id"],
         "reviewer_id": r["r_id"], "locked": bool(r["locked"])}
        for r in conn.execute(
            "SELECT * FROM assignments ORDER BY locked DESC, m_id, r_id")
    ]
    conflicts = [
        {"manuscript_id": r["m_id"], "reviewer_id": r["r_id"]}
        for r in conn.execute("SELECT m_id, r_id FROM conflicts ORDER BY m_id, r_id")
    ]

    load: dict[int, int] = {}
    locked_load: dict[int, int] = {}
    for a in assignments:
        load[a["reviewer_id"]] = load.get(a["reviewer_id"], 0) + 1
        if a["locked"]:
            locked_load[a["reviewer_id"]] = locked_load.get(a["reviewer_id"], 0) + 1

    ms_list = load_manuscripts()
    rv_list = load_reviewers()
    for rv in rv_list:
        rv["assigned"] = load.get(rv["id"], 0)
        rv["locked_assigned"] = locked_load.get(rv["id"], 0)

    per_m: dict[int, int] = {}
    for a in assignments:
        per_m[a["manuscript_id"]] = per_m.get(a["manuscript_id"], 0) + 1
    for m in ms_list:
        m["assigned"] = per_m.get(m["id"], 0)

    solution = get_setting(conn, "last_solution")

    return {
        "settings": {
            "default_required_reviews": get_setting(conn, "default_required_reviews"),
            "default_capacity": get_setting(conn, "default_capacity"),
            "mismatch_weight": get_setting(conn, "mismatch_weight"),
            "load_weight": get_setting(conn, "load_weight"),
            "institution_cap": get_setting(conn, "institution_cap"),
        },
        "manuscripts": ms_list,
        "reviewers": rv_list,
        "conflicts": conflicts,
        "assignments": assignments,
        # 利益冲突自助申报链接状态（不含令牌明文/哈希）
        "declarations": decl_svc.list_declarations(conn),
        "solution": solution,
    }


def _get_or_404(conn: sqlite3.Connection, table: str, obj_id: int, label: str) -> None:
    row = conn.execute(f"SELECT id FROM {table} WHERE id = ?", (obj_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{label} #{obj_id} 不存在")


# ----------------------------------------------------------------- 聚合接口
@app.get("/api/state")
def api_state(conn=Depends(get_conn)):
    return build_state(conn)


# ----------------------------------------------------------------- 稿件
@app.post("/api/manuscripts")
def create_manuscript(body: ManuscriptIn, conn=Depends(get_conn)):
    if body.required_leads > body.required_reviews:
        raise HTTPException(
            status_code=400,
            detail=f"需主审人数（{body.required_leads}）不得超过所需评审数（{body.required_reviews}）。",
        )
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO manuscripts(title, topics, required_reviews, required_leads) "
            "VALUES (?, ?, ?, ?)",
            (body.title.strip(),
             json.dumps(normalize_topics(body.topics), ensure_ascii=False),
             body.required_reviews,
             body.required_leads),
        )
    return build_state(conn) | {"created_id": cur.lastrowid}


@app.patch("/api/manuscripts/{m_id}")
def update_manuscript(m_id: int, body: ManuscriptPatch, conn=Depends(get_conn)):
    _get_or_404(conn, "manuscripts", m_id, "稿件")
    fields, params = [], []
    if body.title is not None:
        fields.append("title = ?")
        params.append(body.title.strip())
    if body.topics is not None:
        fields.append("topics = ?")
        params.append(json.dumps(normalize_topics(body.topics), ensure_ascii=False))
    if body.required_reviews is not None:
        fields.append("required_reviews = ?")
        params.append(body.required_reviews)
    if body.required_leads is not None:
        fields.append("required_leads = ?")
        params.append(body.required_leads)
    if not fields:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    # 主审人数不得超过所需评审数：与库中现有值合并后校验
    if body.required_reviews is not None or body.required_leads is not None:
        row = conn.execute(
            "SELECT required_reviews, required_leads FROM manuscripts WHERE id = ?",
            (m_id,),
        ).fetchone()
        new_req = body.required_reviews if body.required_reviews is not None else row["required_reviews"]
        new_leads = body.required_leads if body.required_leads is not None else row["required_leads"]
        if new_leads > new_req:
            raise HTTPException(
                status_code=400,
                detail=f"需主审人数（{new_leads}）不得超过所需评审数（{new_req}）。",
            )
    params.append(m_id)
    with transaction(conn):
        conn.execute(f"UPDATE manuscripts SET {', '.join(fields)} WHERE id = ?", params)
    return build_state(conn)


@app.delete("/api/manuscripts/{m_id}")
def delete_manuscript(m_id: int, conn=Depends(get_conn)):
    _get_or_404(conn, "manuscripts", m_id, "稿件")
    with transaction(conn):
        conn.execute("DELETE FROM manuscripts WHERE id = ?", (m_id,))
    return build_state(conn)


# ----------------------------------------------------------------- 评审人
@app.post("/api/reviewers")
def create_reviewer(body: ReviewerIn, conn=Depends(get_conn)):
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO reviewers(name, topics, capacity, institution, can_lead) "
            "VALUES (?, ?, ?, ?, ?)",
            (body.name.strip(),
             json.dumps(normalize_topics(body.topics), ensure_ascii=False),
             body.capacity,
             normalize_institution(body.institution),
             1 if body.can_lead else 0),
        )
    return build_state(conn) | {"created_id": cur.lastrowid}


@app.patch("/api/reviewers/{r_id}")
def update_reviewer(r_id: int, body: ReviewerPatch, conn=Depends(get_conn)):
    _get_or_404(conn, "reviewers", r_id, "评审人")
    fields, params = [], []
    if body.name is not None:
        fields.append("name = ?")
        params.append(body.name.strip())
    if body.topics is not None:
        fields.append("topics = ?")
        params.append(json.dumps(normalize_topics(body.topics), ensure_ascii=False))
    if body.capacity is not None:
        fields.append("capacity = ?")
        params.append(body.capacity)
    if body.institution is not None:
        fields.append("institution = ?")
        params.append(normalize_institution(body.institution))
    if body.can_lead is not None:
        fields.append("can_lead = ?")
        params.append(1 if body.can_lead else 0)
    if not fields:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    params.append(r_id)
    with transaction(conn):
        conn.execute(f"UPDATE reviewers SET {', '.join(fields)} WHERE id = ?", params)
    return build_state(conn)


@app.delete("/api/reviewers/{r_id}")
def delete_reviewer(r_id: int, conn=Depends(get_conn)):
    _get_or_404(conn, "reviewers", r_id, "评审人")
    with transaction(conn):
        conn.execute("DELETE FROM reviewers WHERE id = ?", (r_id,))
    return build_state(conn)


# ----------------------------------------------------------------- 冲突
@app.post("/api/conflicts")
def create_conflict(body: ConflictIn, conn=Depends(get_conn)):
    _get_or_404(conn, "manuscripts", body.manuscript_id, "稿件")
    _get_or_404(conn, "reviewers", body.reviewer_id, "评审人")
    locked = conn.execute(
        "SELECT 1 FROM assignments WHERE m_id = ? AND r_id = ? AND locked = 1",
        (body.manuscript_id, body.reviewer_id),
    ).fetchone()
    if locked:
        raise HTTPException(
            status_code=409,
            detail="该稿件-评审人存在锁定的既有分配，不能添加冲突；请先解锁。",
        )
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO conflicts(m_id, r_id) VALUES (?, ?)",
            (body.manuscript_id, body.reviewer_id),
        )
    return build_state(conn)


@app.delete("/api/conflicts/{m_id}/{r_id}")
def delete_conflict(m_id: int, r_id: int, conn=Depends(get_conn)):
    with transaction(conn):
        conn.execute("DELETE FROM conflicts WHERE m_id = ? AND r_id = ?", (m_id, r_id))
    return build_state(conn)


# -------------------------------------------- 利益冲突自助申报（管理端）
@app.post("/api/reviewers/{r_id}/declarations")
def issue_declaration(r_id: int, request: Request, conn=Depends(get_conn)):
    """为评审人签发一次性申报链接。

    旧的待提交链接立即失效（同一评审人至多一个待提交链接）。
    明文令牌仅此一次随响应返回；库内与后续接口只保存/出现哈希之外的状态信息。
    """
    decl_id, token, info = decl_svc.issue_declaration(conn, r_id)
    return {
        "declaration": info,
        "token": token,  # 明文令牌仅本次签发响应可见
        "path": f"/declarations/{token}",
        "url": str(request.base_url).rstrip("/") + f"/declarations/{token}",
        "state": build_state(conn),
    }


@app.post("/api/declarations/{decl_id}/revoke")
def revoke_declaration(decl_id: int, conn=Depends(get_conn)):
    """撤销待提交的申报链接；已提交/已失效返回 409。"""
    decl_svc.revoke_declaration(conn, decl_id)
    return build_state(conn)


# ---------------------------------------- 利益冲突自助申报（评审人公开页）
@app.get("/api/public/declarations/{token}")
def public_declaration(token: str, conn=Depends(get_conn)):
    """评审人凭令牌查看：待提交时仅返回签发清单中的稿件标题。"""
    return decl_svc.get_public_declaration(conn, token)


@app.post("/api/public/declarations/{token}/submit")
def public_declaration_submit(token: str, body: DeclarationSubmitIn, conn=Depends(get_conn)):
    """一次性提交：单事务合并进冲突矩阵并立即失效链接。

    重复提交、清单版本变化（稿件增删/标题修改）返回 409 且不写入；
    勾选含锁定分配对应稿件时同样整单 409，不做部分写入。
    """
    return decl_svc.submit_declaration(conn, token, body.conflict_manuscript_ids)


# ----------------------------------------------------------------- 设置
@app.put("/api/settings")
def update_settings(body: SettingsIn, conn=Depends(get_conn)):
    data = body.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="没有需要更新的设置")
    with transaction(conn):
        for key, value in data.items():
            set_setting(conn, key, value)
    return build_state(conn)


# ----------------------------------------------------------------- 求解 / 锁定
@app.post("/api/solve")
def api_solve(conn=Depends(get_conn)):
    solution = solve(conn)
    return build_state(conn) | {"solution": solution}


# ------------------------------------------------------------- 方案沙盒
@app.post("/api/sandbox/preview")
def sandbox_preview(body: SandboxPreviewIn, conn=Depends(get_conn)):
    """用临时参数生成预览：纯内存计算，不改动 settings/assignments/锁定/last_solution。

    返回与当前方案相比的新增/移除配对、总成本、未满足人数与主审缺口变化，
    以及绑定稿件、评审人、冲突、锁定与参数版本的指纹（供采用时校验）。
    """
    return sandbox_svc.preview(
        conn, body.mismatch_weight, body.load_weight, body.institution_cap)


@app.post("/api/sandbox/adopt")
def sandbox_adopt(body: SandboxAdoptIn, conn=Depends(get_conn)):
    """采用沙盒方案：指纹仍一致时在单个事务内写入临时参数与预览分配。

    指纹不一致（预览后有并发修改）返回 409 且不写入，前端应提示重新预览。
    """
    solution = sandbox_svc.adopt(
        conn, body.mismatch_weight, body.load_weight, body.institution_cap,
        body.fingerprint)
    return build_state(conn) | {"solution": solution}


# ------------------------------------------------------------- 退出补位
@app.post("/api/backfill/preview")
def backfill_preview(body: BackfillPreviewIn, conn=Depends(get_conn)):
    """退出补位只读修复预览：排除临时退出的评审人后在现有硬约束下重排。

    优化目标按“最大化满足人数、最少改动既有非锁定配对、最低原求解成本”
    依次比较，允许必要的连锁换位；展示新增/移除配对、连锁换位与缺口变化，
    并返回绑定稿件、评审人、冲突、锁定、设置与当前非锁定分配的指纹。
    纯内存计算，不改动任何数据。
    """
    return backfill_svc.preview(conn, body.reviewer_id)


@app.post("/api/backfill/adopt")
def backfill_adopt(body: BackfillAdoptIn, conn=Depends(get_conn)):
    """采用退出补位方案：指纹仍一致时在单个事务内写入修复分配。

    退出者的全部配对（含锁定）移除，他人锁定保留；求解参数不变。
    指纹不一致（预览后基线被并发修改）返回 409 且不写入，前端应提示重新预览。
    """
    solution = backfill_svc.adopt(conn, body.reviewer_id, body.fingerprint)
    return build_state(conn) | {"solution": solution}


# ------------------------------------------------------------- 缺口解困建议
@app.post("/api/relief/suggestions")
def relief_suggestions(conn=Depends(get_conn)):
    """缺口解困建议：枚举单步临时调整并只读重算。

    仅当当前分配存在人数或主审缺口时工作；枚举“某评审人容量临时 +1、
    当前同机构上限临时放宽 1、某评审人临时补充主审资格”三类单步调整，
    复用求解器重算，按“总缺口减少 → 主审缺口减少 → 改动配对数 → 成本增量”
    排序，每条建议给出受影响稿件及新旧配对。纯内存试算，不改动 settings、
    reviewers、assignments、锁定或 last_solution；无有效建议时返回空清单。
    """
    return relief_svc.suggest(conn)


@app.post("/api/assignments/lock")
def toggle_lock(body: LockIn, conn=Depends(get_conn)):
    _get_or_404(conn, "manuscripts", body.manuscript_id, "稿件")
    _get_or_404(conn, "reviewers", body.reviewer_id, "评审人")

    is_conflict = conn.execute(
        "SELECT 1 FROM conflicts WHERE m_id = ? AND r_id = ?",
        (body.manuscript_id, body.reviewer_id),
    ).fetchone()

    if body.locked:
        if is_conflict:
            raise HTTPException(status_code=409, detail="存在作者冲突的分配不能锁定。")
        req = conn.execute(
            "SELECT required_reviews FROM manuscripts WHERE id = ?",
            (body.manuscript_id,),
        ).fetchone()["required_reviews"]
        locked_n = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments WHERE m_id = ? AND locked = 1",
            (body.manuscript_id,),
        ).fetchone()["c"]
        existing = conn.execute(
            "SELECT locked FROM assignments WHERE m_id = ? AND r_id = ?",
            (body.manuscript_id, body.reviewer_id),
        ).fetchone()
        # 已存在的行只是被转为锁定（不增加人数）；否则新增一把锁会占用一个名额
        if existing is None and locked_n >= req:
            raise HTTPException(
                status_code=409,
                detail=f"该稿件最多需要 {req} 名评审，已锁定 {locked_n} 人，"
                       f"再锁定一名新评审人将超出需求；请先解锁一把锁。",
            )
        # 机构多样性硬约束：新增锁定会使该稿同机构人数超过上限时以 409 拒绝。
        institution = conn.execute(
            "SELECT COALESCE(institution, '') AS institution FROM reviewers WHERE id = ?",
            (body.reviewer_id,),
        ).fetchone()["institution"]
        org_cap = int(get_setting(conn, "institution_cap"))
        if institution and existing is None and org_cap > 0:
            same_inst_locked = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM assignments a JOIN reviewers rv ON a.r_id = rv.id
                WHERE a.m_id = ? AND a.locked = 1
                  AND COALESCE(rv.institution, '') = ?
                """,
                (body.manuscript_id, institution),
            ).fetchone()["c"]
            if institution_lock_exceeds(org_cap, same_inst_locked, True):
                raise HTTPException(
                    status_code=409,
                    detail=f"该稿件来自「{institution}」的锁定评审已有 {same_inst_locked} 人，"
                           f"再锁定将超过每篇同机构最多 {org_cap} 人的限制；"
                           f"请先解锁一名该机构的锁定，或在设置中放宽上限。",
                )
        with transaction(conn):
            conn.execute(
                "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1) "
                "ON CONFLICT(m_id, r_id) DO UPDATE SET locked = 1",
                (body.manuscript_id, body.reviewer_id),
            )
    else:
        with transaction(conn):
            conn.execute(
                "DELETE FROM assignments WHERE m_id = ? AND r_id = ? AND locked = 1",
                (body.manuscript_id, body.reviewer_id),
            )

    # 锁定状态变化后立即重新求解：锁定边占用容量并参与负载计算
    solution = solve(conn)
    return build_state(conn) | {"solution": solution}


# ----------------------------------------------------------------- 演示/重置
@app.post("/api/demo")
def load_demo(conn=Depends(get_conn)):
    with transaction(conn):
        conn.execute("DELETE FROM assignments")
        conn.execute("DELETE FROM conflicts")
        conn.execute("DELETE FROM conflict_declarations")
        conn.execute("DELETE FROM manuscripts")
        conn.execute("DELETE FROM reviewers")
        seed_demo_data(conn)
        set_setting(conn, "last_solution", None)
    solution = solve(conn)
    return build_state(conn) | {"solution": solution}


@app.post("/api/reset")
def reset_all(conn=Depends(get_conn)):
    with transaction(conn):
        conn.execute("DELETE FROM assignments")
        conn.execute("DELETE FROM conflicts")
        conn.execute("DELETE FROM conflict_declarations")
        conn.execute("DELETE FROM manuscripts")
        conn.execute("DELETE FROM reviewers")
        set_setting(conn, "last_solution", None)
    return build_state(conn)


# ----------------------------------------------------------------- 静态前端
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# 评审人自助申报页（公开，凭路径中的一次性令牌访问）
@app.get("/declarations/{token}")
def declaration_page(token: str):
    return FileResponse(os.path.join(STATIC_DIR, "declaration.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
