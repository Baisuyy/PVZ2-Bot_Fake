"""
Account Manager — FastAPI 账号管理服务 (端口 8000)

功能:
  - POST /api/accounts/upload        上传单条账号
  - POST /api/accounts/upload/batch   批量上传账号
  - GET  /api/accounts/distribute     原子分发一个账号
  - POST /api/accounts/distribute/batch 批量分发
  - PUT  /api/accounts/{id}/status    更新状态
  - POST /api/accounts/batch/status   批量更新状态
  - GET  /api/accounts/stats          统计
  - GET  /api/accounts/list           列表查询

并发安全: UPDATE + 子查询 + asyncio.Lock，零重复分发
"""
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

import pvz2_bot.database as dbs
from .account_models import (
    AccountUpload, BatchUploadRequest, UploadResult,
    AccountInfo, StatusUpdate, BatchStatusRequest, BatchStatusResult,
    StatsResponse, PlatformStats,
)

app = FastAPI(title="PVZ2 Account Manager", version="3.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger("account_manager")


def _row_to_info(row) -> AccountInfo:
    return AccountInfo(
        id=row["id"],
        platform=row["platform"],
        status=row["status"],
        ui=row["ui"],
        sk=row["sk"],
        secret=row["secret"] or "1geh6fvq4r20M02s",
        user_id=row["user_id"],
        username=row["username"],
        udid=row["udid"],
        pi=row["pi"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


# ========== 上传 ==========

@app.post("/api/accounts/upload", response_model=UploadResult)
async def upload_account(account: AccountUpload):
    try:
        async with dbs.db_pool.write() as conn:
            cursor = await conn.execute(
                """INSERT OR IGNORE INTO accounts
                   (platform, ui, sk, secret, user_id, username, udid, pi)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (account.platform, account.ui, account.sk, account.secret,
                 account.user_id, account.username, account.udid, account.pi),
            )
            if cursor.rowcount > 0:
                return UploadResult(success=1, skipped=0, errors=[])
            return UploadResult(success=0, skipped=1, errors=["ui 已存在"])
    except Exception as e:
        return UploadResult(success=0, skipped=0, errors=[str(e)])


@app.post("/api/accounts/upload/batch", response_model=UploadResult)
async def upload_batch(batch: BatchUploadRequest):
    logger.info(f"[BATCH] 收到 {len(batch.accounts)} 条账号")
    success = 0; skipped = 0; errors = []
    try:
        async with dbs.db_pool.write() as conn:
            for acc in batch.accounts:
                try:
                    cursor = await conn.execute(
                        """INSERT OR IGNORE INTO accounts
                           (platform, ui, sk, secret, user_id, username, udid, pi)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (acc.platform, acc.ui, acc.sk, acc.secret,
                         acc.user_id, acc.username, acc.udid, acc.pi),
                    )
                    if cursor.rowcount > 0:
                        success += 1
                    else:
                        skipped += 1
                except Exception as e:
                    errors.append(f"ui={acc.ui}: {e}")
        return UploadResult(success=success, skipped=skipped, errors=errors)
    except Exception as e:
        return UploadResult(success=success, skipped=skipped, errors=errors + [str(e)])


# ========== 分发（核心） ==========

@app.get("/api/accounts/distribute", response_model=AccountInfo)
async def distribute_account(
    platform: str = Query(..., pattern="^(android|ios)$"),
    purpose: str = Query("activate", pattern="^(activate|invite)$"),
    mark_used: Optional[bool] = Query(None),
):
    """原子分发: UPDATE + 子查询防并发重复"""
    try:
        async with dbs.db_pool.write() as conn:
            if purpose == "activate":
                target_status = "inactive"
                should_mark = False
            else:
                target_status = "activated"
                should_mark = True
            if mark_used is not None:
                should_mark = mark_used

            if should_mark:
                cursor = await conn.execute(
                    """UPDATE accounts SET status='used', updated_at=CURRENT_TIMESTAMP
                       WHERE id=(SELECT id FROM accounts WHERE status=? AND platform=? LIMIT 1)
                       RETURNING *""",
                    (target_status, platform),
                )
                row = await cursor.fetchone()
                if row is None and purpose == "invite":
                    logger.info(f"[降级] {platform} 无 activated, 从 inactive 取")
                    cursor = await conn.execute(
                        """UPDATE accounts SET status='used', updated_at=CURRENT_TIMESTAMP
                           WHERE id=(SELECT id FROM accounts WHERE status='inactive' AND platform=? LIMIT 1)
                           RETURNING *""",
                        (platform,),
                    )
                    row = await cursor.fetchone()
            else:
                cursor = await conn.execute(
                    "SELECT * FROM accounts WHERE status=? AND platform=? LIMIT 1",
                    (target_status, platform),
                )
                row = await cursor.fetchone()

            if row is None:
                raise HTTPException(
                    404, detail=f"无可用 {platform} 账号 (purpose={purpose})"
                )
            return _row_to_info(row)

    except Exception as e:
        logger.exception("分发失败")
        raise HTTPException(500, str(e))


@app.post("/api/accounts/distribute/batch", response_model=list[AccountInfo])
async def distribute_batch(
    platform: str = Query(..., pattern="^(android|ios)$"),
    purpose: str = Query("activate", pattern="^(activate|invite)$"),
    count: int = Query(1, ge=1, le=50),
    mark_used: Optional[bool] = Query(None),
):
    """批量分发: CTE 原子操作"""
    try:
        async with dbs.db_pool.write() as conn:
            if purpose == "activate":
                target_status = "inactive"
                should_mark = False
            else:
                target_status = "activated"
                should_mark = True
            if mark_used is not None:
                should_mark = mark_used

            if should_mark:
                cursor = await conn.execute(
                    """WITH selected AS (
                           SELECT id FROM accounts WHERE status=? AND platform=? LIMIT ?
                       )
                       UPDATE accounts SET status='used', updated_at=CURRENT_TIMESTAMP
                       WHERE id IN (SELECT id FROM selected)
                       RETURNING *""",
                    (target_status, platform, count),
                )
                rows = await cursor.fetchall()
                if len(rows) < count and purpose == "invite":
                    remain = count - len(rows)
                    cursor2 = await conn.execute(
                        """WITH selected AS (
                               SELECT id FROM accounts WHERE status='inactive' AND platform=? LIMIT ?
                           )
                           UPDATE accounts SET status='used', updated_at=CURRENT_TIMESTAMP
                           WHERE id IN (SELECT id FROM selected)
                           RETURNING *""",
                        (platform, remain),
                    )
                    rows.extend(await cursor2.fetchall())
            else:
                cursor = await conn.execute(
                    "SELECT * FROM accounts WHERE status=? AND platform=? LIMIT ?",
                    (target_status, platform, count),
                )
                rows = await cursor.fetchall()

            if not rows:
                raise HTTPException(404, detail=f"无可用 {platform} 账号")
            return [_row_to_info(r) for r in rows]

    except Exception as e:
        logger.exception("批量分发失败")
        raise HTTPException(500, str(e))


# ========== 状态更新 ==========

@app.put("/api/accounts/{account_id}/status", response_model=AccountInfo)
async def update_status(account_id: int, update: StatusUpdate):
    try:
        async with dbs.db_pool.write() as conn:
            cursor = await conn.execute(
                "UPDATE accounts SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (update.status, account_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(404, "账号不存在")
            cursor = await conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,))
            return _row_to_info(await cursor.fetchone())
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/accounts/batch/status", response_model=BatchStatusResult)
async def batch_update_status(batch: BatchStatusRequest):
    success = 0; errors = []
    try:
        async with dbs.db_pool.write() as conn:
            for item in batch.updates:
                try:
                    cursor = await conn.execute(
                        "UPDATE accounts SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (item.status, item.id),
                    )
                    if cursor.rowcount > 0:
                        success += 1
                    else:
                        errors.append(f"id={item.id}: 不存在")
                except Exception as e:
                    errors.append(f"id={item.id}: {e}")
        return BatchStatusResult(success=success, errors=errors)
    except Exception as e:
        return BatchStatusResult(success=success, errors=errors + [str(e)])


# ========== 查询 ==========

@app.get("/api/accounts/stats", response_model=StatsResponse)
async def get_stats():
    stats = StatsResponse()
    async with dbs.db_pool.read() as conn:
        for platform in ("android", "ios"):
            cursor = await conn.execute(
                "SELECT status, COUNT(*) as cnt FROM accounts WHERE platform=? GROUP BY status",
                (platform,),
            )
            ps = PlatformStats()
            async for row in cursor:
                setattr(ps, row["status"], row["cnt"])
            setattr(stats, platform, ps)
        stats.total = (
            stats.android.inactive + stats.android.activated + stats.android.used +
            stats.ios.inactive + stats.ios.activated + stats.ios.used
        )
    return stats


@app.get("/api/accounts/list", response_model=list[AccountInfo])
async def list_accounts(
    platform: Optional[str] = Query(None, pattern="^(android|ios)$"),
    status: Optional[str] = Query(None, pattern="^(inactive|activated|used)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    async with dbs.db_pool.read() as conn:
        query = "SELECT * FROM accounts WHERE 1=1"
        params = []
        if platform:
            query += " AND platform=?"
            params.append(platform)
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cursor = await conn.execute(query, tuple(params))
        return [_row_to_info(r) async for r in cursor]


# ========== 启动 ==========

@app.on_event("startup")
async def startup():
    await dbs.init_db()


@app.on_event("shutdown")
async def shutdown():
    await dbs.close_db()


def run(port: int = 8000):
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    run()