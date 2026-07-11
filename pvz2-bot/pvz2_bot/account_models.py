"""
Pydantic 数据模型 — Account Manager API 的请求/响应类型
"""
from typing import List, Optional
from pydantic import BaseModel, Field, model_validator


# ============================================================
# 上传账号
# ============================================================
class AccountUpload(BaseModel):
    platform: str = Field(..., pattern="^(android|ios)$")
    ui: str = Field(..., description="通用标识")
    sk: str = Field(..., description="密钥")
    secret: str = Field(default="1geh6fvq4r20M02s", description="加密密钥")
    # 安卓专用
    user_id: Optional[str] = Field(None, description="4399 用户ID")
    username: Optional[str] = Field(None, description="4399 用户名")
    # iOS 专用
    udid: Optional[str] = Field(None, description="设备UDID")
    pi: Optional[str] = Field(None, description="玩家ID")


class BatchUploadRequest(BaseModel):
    accounts: List[AccountUpload]


class UploadResult(BaseModel):
    success: int
    skipped: int
    errors: List[str]


# ============================================================
# 账号信息
# ============================================================
class AccountInfo(BaseModel):
    id: int
    account_id: Optional[int] = None  # 兼容旧客户端
    platform: str
    status: str
    ui: str
    sk: str
    secret: str
    user_id: Optional[str] = None
    username: Optional[str] = None
    udid: Optional[str] = None
    pi: Optional[str] = None
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def _fill_account_id(self):
        if self.account_id is None:
            self.account_id = self.id
        return self


# ============================================================
# 状态更新
# ============================================================
class StatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(inactive|activated|used)$")


class BatchStatusItem(BaseModel):
    id: int
    status: str = Field(..., pattern="^(inactive|activated|used)$")


class BatchStatusRequest(BaseModel):
    updates: List[BatchStatusItem]


class BatchStatusResult(BaseModel):
    success: int
    errors: List[str]


# ============================================================
# 统计
# ============================================================
class PlatformStats(BaseModel):
    inactive: int = 0
    activated: int = 0
    used: int = 0


class StatsResponse(BaseModel):
    android: PlatformStats = PlatformStats()
    ios: PlatformStats = PlatformStats()
    total: int = 0