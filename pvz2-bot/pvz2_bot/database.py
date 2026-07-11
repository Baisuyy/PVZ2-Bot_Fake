"""
账号存储 SQLite — 供 Account Manager (8000端口) 使用

表结构:
  accounts(id, platform, status, ui, sk, secret, user_id, username, udid, pi, ...)
  status: inactive → activated → used

并发安全:
  - WAL 模式 + busy_timeout=30000
  - asyncio.Lock 保证进程内串行写
  - UPDATE+子查询 原子分发
"""
import asyncio
import os
from contextlib import asynccontextmanager

import aiosqlite


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "accounts.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT    NOT NULL CHECK(platform IN ('android', 'ios')),
    status      TEXT    NOT NULL DEFAULT 'inactive'
                        CHECK(status IN ('inactive', 'activated', 'used')),
    ui          TEXT    UNIQUE NOT NULL,
    sk          TEXT    NOT NULL,
    secret      TEXT    NOT NULL DEFAULT '1geh6fvq4r20M02s',
    user_id     TEXT,
    username    TEXT,
    udid        TEXT,
    pi          TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_accounts_platform_status
    ON accounts(platform, status);

CREATE INDEX IF NOT EXISTS idx_accounts_ui ON accounts(ui);
"""


class DatabasePool:
    """
    SQLite 并发安全连接池。

    并发保证:
    1. asyncio.Lock: 进程内多协程串行执行写操作
    2. isolation_level='' (默认): sqlite3 自动为 DML 包裹隐式事务
    3. PRAGMA busy_timeout=30000: 跨进程写锁冲突自动等待
    4. WAL 模式: 读写可并发，写-写自动排队
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._read_conn: aiosqlite.Connection | None = None
        self._write_conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def init(self):
        self._write_conn = await aiosqlite.connect(self.db_path, timeout=30)
        self._write_conn.row_factory = aiosqlite.Row
        await self._write_conn.execute("PRAGMA journal_mode=WAL")
        await self._write_conn.execute("PRAGMA busy_timeout=30000")
        await self._write_conn.execute("PRAGMA synchronous=NORMAL")

        self._read_conn = await aiosqlite.connect(self.db_path, timeout=30)
        self._read_conn.row_factory = aiosqlite.Row
        await self._read_conn.execute("PRAGMA journal_mode=WAL")
        await self._read_conn.execute("PRAGMA busy_timeout=30000")

        await self._write_conn.executescript(SCHEMA)
        await self._write_conn.commit()

    async def close(self):
        if self._read_conn:
            await self._read_conn.close()
        if self._write_conn:
            await self._write_conn.close()

    @asynccontextmanager
    async def read(self):
        """共享读连接，多个读操作可并发"""
        yield self._read_conn

    @asynccontextmanager
    async def write(self):
        """
        写操作上下文管理器:
        - asyncio.Lock 保证串行
        - yield 结束后自动 commit
        - 异常时自动 rollback
        """
        async with self._write_lock:
            try:
                yield self._write_conn
                await self._write_conn.commit()
            except Exception:
                await self._write_conn.rollback()
                raise


# 全局单例
db_pool: DatabasePool | None = None


async def init_db():
    global db_pool
    db_pool = DatabasePool(DB_PATH)
    await db_pool.init()


async def close_db():
    global db_pool
    if db_pool:
        await db_pool.close()
        db_pool = None