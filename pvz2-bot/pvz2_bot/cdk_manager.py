"""
CDK 卡密管理数据库 — SQLite

表 cdk_list:
  cdk_code    TEXT PRIMARY KEY  -- 卡密码
  amount      INTEGER NOT NULL  -- 次数
  is_used     INTEGER DEFAULT 0 -- 0=未用, 1=已用
  used_time   TIMESTAMP         -- 使用时间
  used_for_level TEXT           -- 使用的关卡/platform
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP

管理 API:
  POST /api/cdk/create          -- 生成卡密
  POST /api/cdk/batch           -- 批量生成
  GET  /api/cdk/list            -- 列表查询
  GET  /api/cdk/stats           -- 统计
"""
import json
import random
import string
import sqlite3
import time
from typing import Optional

from flask import Blueprint, request, jsonify


cdk_bp = Blueprint("cdk", __name__)
DB_PATH = "cdk_data.db"


def get_conn():
    """获取CDK数据库连接 (公开发)"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

# 别名兼容旧调用
_conn = get_conn


def init_cdk_db():
    conn = _conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cdk_list (
            cdk_code      TEXT PRIMARY KEY,
            amount        INTEGER NOT NULL DEFAULT 100,
            is_used       INTEGER NOT NULL DEFAULT 0,
            used_time     TIMESTAMP,
            used_for_level TEXT DEFAULT '',
            created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cdk_used ON cdk_list(is_used)")
    conn.commit()
    conn.close()


def _random_cdk() -> str:
    """生成随机8位卡密"""
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=8))


# ========== API ==========

@cdk_bp.route("/cdk/create", methods=["POST"])
def create_cdk():
    """生成单条卡密"""
    data = request.json or {}
    amount = int(data.get("amount", 100))
    prefix = data.get("prefix", "")
    cdk_code = data.get("cdk_code", "").strip()

    if not cdk_code:
        cdk_code = prefix + _random_cdk()

    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO cdk_list (cdk_code, amount) VALUES (?, ?)",
            (cdk_code, amount),
        )
        conn.commit()
        return jsonify({"status": "success", "cdk_code": cdk_code, "amount": amount})
    except sqlite3.IntegrityError:
        return jsonify({"status": "error", "message": "卡密已存在"}), 409
    finally:
        conn.close()


@cdk_bp.route("/cdk/batch", methods=["POST"])
def batch_cdk():
    """批量生成卡密"""
    data = request.json or {}
    count = int(data.get("count", 10))
    amount = int(data.get("amount", 100))
    prefix = data.get("prefix", "")

    if count < 1 or count > 500:
        return jsonify({"status": "error", "message": "数量范围 1-500"}), 400

    conn = _conn()
    created = []
    errors = []
    for _ in range(count):
        cdk = prefix + _random_cdk()
        try:
            conn.execute(
                "INSERT INTO cdk_list (cdk_code, amount) VALUES (?, ?)",
                (cdk, amount),
            )
            created.append(cdk)
        except sqlite3.IntegrityError:
            errors.append(cdk)
    conn.commit()
    conn.close()
    return jsonify({
        "status": "success",
        "created": len(created),
        "created_codes": created,
        "errors": errors,
    })


@cdk_bp.route("/cdk/list", methods=["GET"])
def list_cdk():
    """查询卡密列表"""
    is_used = request.args.get("is_used")
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = max(int(request.args.get("offset", 0)), 0)
    search = request.args.get("search", "").strip()

    conn = _conn()
    try:
        where_clauses = ["1=1"]
        params = []
        if is_used is not None:
            where_clauses.append("is_used = ?")
            params.append(int(is_used))
        if search:
            where_clauses.append("cdk_code LIKE ?")
            params.append(f"%{search}%")

        where_sql = " AND ".join(where_clauses)
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM cdk_list WHERE {where_sql}",
            tuple(params) if params else (),
        ).fetchone()["c"]

        rows = conn.execute(
            f"SELECT * FROM cdk_list WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params + [limit, offset]),
        ).fetchall()
        return jsonify({"total": total, "cdks": [dict(r) for r in rows]})
    finally:
        conn.close()


@cdk_bp.route("/cdk/stats", methods=["GET"])
def cdk_stats():
    """卡密统计"""
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM cdk_list").fetchone()["c"]
        unused = conn.execute("SELECT COUNT(*) AS c FROM cdk_list WHERE is_used=0").fetchone()["c"]
        used = conn.execute("SELECT COUNT(*) AS c FROM cdk_list WHERE is_used=1").fetchone()["c"]
        unused_amt = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS c FROM cdk_list WHERE is_used=0"
        ).fetchone()["c"]
        used_amt = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS c FROM cdk_list WHERE is_used=1"
        ).fetchone()["c"]
        return jsonify({
            "total": total, "unused": unused, "used": used,
            "unused_amount": unused_amt, "used_amount": used_amt,
        })
    finally:
        conn.close()