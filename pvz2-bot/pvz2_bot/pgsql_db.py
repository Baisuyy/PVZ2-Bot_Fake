"""
Postgres + Redis 数据库层 — 供整合登录使用

Postgres 存储 4399 账号 (user_id, username, password, login_token, oi)
Redis   充当账号队列 + 缓存
"""
import json
import os
from typing import Dict, List, Any

import asyncpg
import redis.asyncio as redis

from .config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DATABASE, REDIS_URL


_pool: asyncpg.Pool | None = None
_redis: redis.Redis | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            database=PG_DATABASE,
            min_size=10,
            max_size=50,
            command_timeout=30,
        )
    return _pool


async def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def init_pgsql_db():
    """初始化 Postgres 表结构"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                login_token TEXT NOT NULL,
                secret TEXT NOT NULL DEFAULT '1geh6fvq4r20M02s',
                oi TEXT NOT NULL,
                status INTEGER NOT NULL DEFAULT 0,
                ui TEXT,
                sk TEXT,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                activated_at TIMESTAMP WITH TIME ZONE
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pg_accounts_status ON accounts(status)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pg_accounts_user_id ON accounts(user_id)"
        )


async def save_account(account: Dict[str, Any]) -> int:
    """保存或更新一个账号"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        record = await conn.fetchrow("""
            INSERT INTO accounts (user_id, username, password, login_token, secret, oi)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                password = EXCLUDED.password,
                login_token = EXCLUDED.login_token,
                oi = EXCLUDED.oi
            RETURNING id
        """, account["user_id"], account["username"], account["password"],
           account["login_token"], account["secret"], account["oi"])
        return record["id"]


async def batch_save_accounts(accounts: List[Dict[str, Any]]):
    """批量保存账号"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for account in accounts:
                await conn.execute("""
                    INSERT INTO accounts (user_id, username, password, login_token, secret, oi)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        password = EXCLUDED.password,
                        login_token = EXCLUDED.login_token,
                        oi = EXCLUDED.oi
                """, account["user_id"], account["username"], account["password"],
                   account["login_token"], account["secret"], account["oi"])


async def get_pending_accounts(limit: int = 10) -> List[Dict[str, Any]]:
    """获取待激活账号 (status=0)，FOR UPDATE SKIP LOCKED 防止并发冲突"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, user_id, username, password, login_token, secret, oi
            FROM accounts
            WHERE status = 0
            ORDER BY created_at ASC
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        """, limit)
        return [dict(r) for r in rows]


async def mark_activated(account_id: int, ui: str, sk: str) -> bool:
    """标记账号已激活"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE accounts
            SET status = 1, ui = $2, sk = $3, activated_at = CURRENT_TIMESTAMP
            WHERE id = $1 AND status = 0
        """, account_id, ui, sk)
        return result != "UPDATE 0"


async def mark_failed(account_id: int) -> bool:
    """标记账号激活失败"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE accounts SET status = 2 WHERE id = $1 AND status = 0
        """, account_id)
        return result != "UPDATE 0"


async def get_stats() -> Dict[str, int]:
    """获取账号统计"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM accounts")
        pending = await conn.fetchval("SELECT COUNT(*) FROM accounts WHERE status = 0")
        activated = await conn.fetchval("SELECT COUNT(*) FROM accounts WHERE status = 1")
        failed = await conn.fetchval("SELECT COUNT(*) FROM accounts WHERE status = 2")
    return {"total": total, "pending": pending, "activated": activated, "failed": failed}


async def push_to_redis_queue(account: Dict[str, Any]):
    """推入 Redis 队列"""
    r = await get_redis()
    await r.rpush("account_queue", json.dumps(account))


async def pop_from_redis_queue(count: int = 100) -> List[Dict[str, Any]]:
    """批量从 Redis 队列获取"""
    r = await get_redis()
    results = await r.lrange("account_queue", 0, count - 1)
    if results:
        await r.ltrim("account_queue", count, -1)
    return [json.loads(item) for item in results]