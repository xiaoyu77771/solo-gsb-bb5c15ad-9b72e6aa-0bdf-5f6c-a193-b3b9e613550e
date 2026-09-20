"""请求体模型（Pydantic）。"""
from __future__ import annotations

from typing import List, Union

from pydantic import BaseModel, Field, field_validator

TopicInput = Union[str, List[str]]


class ManuscriptIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    topics: TopicInput = ""
    required_reviews: int = Field(default=3, ge=1, le=20)
    # 需主审人数：0~2，且不得超过所需评审数（跨字段校验在端点内合并现有值后进行）
    required_leads: int = Field(default=0, ge=0, le=2)


class ManuscriptPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    topics: TopicInput | None = None
    required_reviews: int | None = Field(default=None, ge=1, le=20)
    required_leads: int | None = Field(default=None, ge=0, le=2)


class ReviewerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    topics: TopicInput = ""
    capacity: int = Field(default=3, ge=0, le=50)
    institution: str = Field(default="", max_length=120)
    can_lead: bool = False  # 是否可任主审


class ReviewerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    topics: TopicInput | None = None
    capacity: int | None = Field(default=None, ge=0, le=50)
    institution: str | None = Field(default=None, max_length=120)
    can_lead: bool | None = None


class ConflictIn(BaseModel):
    manuscript_id: int
    reviewer_id: int


class SettingsIn(BaseModel):
    default_required_reviews: int | None = Field(default=None, ge=1, le=20)
    default_capacity: int | None = Field(default=None, ge=0, le=50)
    mismatch_weight: int | None = Field(default=None, ge=0, le=100000)
    load_weight: int | None = Field(default=None, ge=0, le=100000)
    # 每篇稿件同机构最多人数；0 表示关闭机构多样性约束
    institution_cap: int | None = Field(default=None, ge=0, le=20)


class LockIn(BaseModel):
    manuscript_id: int
    reviewer_id: int
    locked: bool


class SandboxPreviewIn(BaseModel):
    """方案沙盒预览：三项临时求解参数（仅用于内存重算，不写入设置）。"""
    mismatch_weight: int = Field(ge=0, le=100000)
    load_weight: int = Field(ge=0, le=100000)
    # 每篇稿件同机构最多人数；0 表示关闭机构多样性约束
    institution_cap: int = Field(ge=0, le=20)


class SandboxAdoptIn(SandboxPreviewIn):
    """采用沙盒方案：附带预览时绑定的版本指纹（不一致返回 409）。"""
    fingerprint: str = Field(min_length=1, max_length=128)


class BackfillPreviewIn(BaseModel):
    """退出补位预览：选择一名临时退出的评审人（只读重排，不写入任何数据）。"""
    reviewer_id: int = Field(ge=1)


class BackfillAdoptIn(BaseModel):
    """采用退出补位方案：退出者 ID + 预览时绑定的版本指纹（不一致返回 409）。"""
    reviewer_id: int = Field(ge=1)
    fingerprint: str = Field(min_length=1, max_length=128)


class DeclarationSubmitIn(BaseModel):
    """评审人一次性申报提交体：勾选存在冲突的稿件 ID 列表（空列表=无冲突）。"""
    conflict_manuscript_ids: List[int] = Field(default_factory=list, max_length=1000)


class BatchCreateIn(BaseModel):
    """创建不可变分配批次快照：可选管理员备注。"""
    note: str = Field(default="", max_length=500)


class BatchRollbackIn(BaseModel):
    """按批次原子回滚：目标批次 ID + 可选备注（写入回滚后自动创建的新批次）。"""
    note: str = Field(default="", max_length=500)


class TopicsIn(BaseModel):
    """仅用于前端校验的占位模型（当前未使用，保留扩展）。"""

    @field_validator("*")
    @classmethod
    def noop(cls, v):
        return v
