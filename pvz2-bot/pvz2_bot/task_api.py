"""
任务节点 Flask API — 端口 39902

功能:
  - 调度层 STUN/任务下发
  - 调度层心跳注册
  - 前端查询接口 (状态/日志/统计数据)
"""
import json
import logging
import threading
import time
from typing import Optional

import requests
from flask import Flask, request, jsonify

from .config import (
    NODE_NAME, NODE_VERSION, NODE_LISTEN_PORT,
    SCHEDULER_URL, HEARTBEAT_INTERVAL, REGISTER_RETRY_SECONDS,
    UNIFIED_PASSWORD, ACCOUNT_API_BASE,
    LOG_SUCCESS_RESPONSES,
)
from .task_engine import TaskEngine
from .task_models import Task, RuntimeStore

logger = logging.getLogger("task_api")

app = Flask(__name__)
engine = TaskEngine()
runtime = RuntimeStore(node_name=NODE_NAME, version=NODE_VERSION)

# 调度层会话
_scheduler_session = requests.Session()
_scheduler_session.headers.update({
    "Authorization": f"Bearer {UNIFIED_PASSWORD}",
    "Content-Type": "application/json",
})


# ============================================================
# 调度层通信
# ============================================================

_last_sch_warn = 0.0

def register_to_scheduler():
    """向调度层注册本节点"""
    global _last_sch_warn
    try:
        payload = runtime.register_payload()
        payload["listen_port"] = NODE_LISTEN_PORT
        payload["task_count"] = engine.worker_count
        resp = _scheduler_session.post(
            f"{SCHEDULER_URL}/nodes/register",
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            runtime.registered = True
            runtime.registration_error = ""
            mode = resp.json().get("mode", "active")
            runtime.scheduler_mode = mode
            engine.set_mode(mode)
            logger.info("已注册到调度层 → mode=%s", mode)
        else:
            runtime.registered = False
            runtime.registration_error = f"HTTP {resp.status_code}"
            logger.warning("注册失败: HTTP %d", resp.status_code)
    except Exception as e:
        runtime.registered = False
        runtime.registration_error = str(e)
        now = time.time()
        if now - _last_sch_warn > 30:
            logger.warning("调度层不可达 (%s)，每30s报一次", e)
            _last_sch_warn = now
        else:
            logger.debug("调度层不可达(压制): %s", e)


def send_heartbeat():
    """心跳上报"""
    import os
    try:
        payload = runtime.heartbeat_payload()
        payload["account_counts"] = _fetch_account_stats()
        try:
            stat = os.stat(RUNTIME_SQLITE_DB)
            payload["db_revision"] = f"{int(stat.st_mtime)}:{stat.st_size}"
        except OSError:
            payload["db_revision"] = "missing"

        resp = _scheduler_session.post(
            f"{SCHEDULER_URL}/nodes/heartbeat",
            json=payload,
            timeout=5,
        )
        if resp.status_code == 200:
            runtime.scheduler_last_ok_at = time.time()
            runtime.scheduler_last_error = ""
            data = resp.json()
            mode = data.get("mode", runtime.scheduler_mode)
            if mode != runtime.scheduler_mode:
                runtime.scheduler_mode = mode
                engine.set_mode(mode)
                logger.info("调度层模式变更: %s → %s", runtime.scheduler_mode, mode)
        else:
            runtime.scheduler_last_error = f"HTTP {resp.status_code}"
    except Exception as e:
        runtime.scheduler_last_error = str(e)


def _fetch_account_stats() -> dict:
    try:
        resp = requests.get(
            f"{ACCOUNT_API_BASE}/api/accounts/stats", timeout=5
        )
        return resp.json()
    except Exception:
        return {"android": {}, "ios": {}}


def _heartbeat_loop():
    """心跳循环 — 独立线程"""
    while True:
        if not runtime.registered:
            register_to_scheduler()
            time.sleep(REGISTER_RETRY_SECONDS)
        else:
            send_heartbeat()
            engine.dispatch()  # 从队列拉取任务
            time.sleep(HEARTBEAT_INTERVAL)


# ============================================================
# Flask API — 任务管理
# ============================================================

@app.route("/api/v2/task/submit", methods=["POST"])
def submit_task():
    """调度层推送任务"""
    data = request.json or {}
    token = request.headers.get("Authorization", "")
    if token != f"Bearer {UNIFIED_PASSWORD}":
        return jsonify({"status": "error", "message": "unauthorized"}), 403

    try:
        task = Task(
            id=data["task_id"],
            platform=data["platform"],
            task_type=data["task_type"],
            total_count=int(data["total_count"]),
            level_id=data["level_id"],
            payload_json=json.dumps(data),
        )
    except KeyError as e:
        return jsonify({"status": "error", "message": f"缺少字段: {e}"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    ok = engine.submit(task)
    if ok:
        return jsonify({"status": "queued", "task_id": task.id})
    return jsonify({"status": "rejected", "reason": "队列已满或模式不允许"}), 429


@app.route("/api/v2/task/status/<task_id>", methods=["GET"])
def task_status(task_id: str):
    """查询任务状态"""
    token = request.headers.get("Authorization", "")
    if token != f"Bearer {UNIFIED_PASSWORD}":
        return jsonify({"status": "error", "message": "unauthorized"}), 403

    import sqlite3
    conn = sqlite3.connect(RUNTIME_SQLITE_DB, timeout=5)
    try:
        row = conn.execute(
            "SELECT * FROM node_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row:
            task = Task.from_row(row)
            return jsonify(task.to_record())
        return jsonify({"status": "not_found"}), 404
    finally:
        conn.close()


@app.route("/api/v2/status", methods=["GET"])
def node_status():
    """节点状态"""
    return jsonify({
        "node_name": NODE_NAME,
        "version": NODE_VERSION,
        "port": NODE_LISTEN_PORT,
        "registered": runtime.registered,
        "runtime_mode": runtime.runtime_mode,
        "scheduler_mode": runtime.scheduler_mode,
        "active_tasks": engine.active_tasks,
        "worker_count": engine.worker_count,
        "queue_size": engine.queue.size,
    })


# ============================================================
# 启动
# ============================================================

def run(port: int | None = None):
    port = port or NODE_LISTEN_PORT
    logger.info("启动任务节点，端口 %d", port)
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop, name="HeartbeatLoop", daemon=True
    )
    heartbeat_thread.start()
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=32)
    except ImportError:
        app.run(host="0.0.0.0", port=port, debug=False)