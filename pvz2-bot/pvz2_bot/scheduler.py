"""
调度层 (Dispatcher / Scheduler) — Flask API 端口 39900

功能:
  - 接收前端 CDK 任务 → 分发到可用任务节点
  - 节点注册/心跳管理
  - 账号封禁状态汇总
  - ChainState/NS 同步
  - 负载均衡: 最小任务数节点优先
"""
import json
import logging
import os
import threading
import time
from typing import Dict, Optional

import requests
from flask import Flask, request, jsonify

from .config import UNIFIED_PASSWORD

API_TOKEN = UNIFIED_PASSWORD

logger = logging.getLogger("scheduler")

app = Flask(__name__)

# ============================================================
# 节点注册表 (内存)
# ============================================================
_nodes: Dict[str, dict] = {}
_nodes_lock = threading.Lock()
NODE_TIMEOUT = 15  # 心跳超时秒数

# 封禁状态
_banned_accounts: Dict[str, dict] = {}
_banned_lock = threading.Lock()

# ChainState 版本
_chain_states: Dict[str, dict] = {}
_chain_lock = threading.Lock()


def _clean_stale_nodes():
    """清理超时未心跳的节点"""
    now = time.time()
    with _nodes_lock:
        stale = [
            name for name, info in _nodes.items()
            if now - info.get("last_heartbeat", 0) > NODE_TIMEOUT
        ]
        for name in stale:
            logger.warning("节点超时: %s", name)
            del _nodes[name]


def _pick_best_node() -> Optional[str]:
    """负载均衡: 选择任务数最少的活跃节点"""
    _clean_stale_nodes()
    with _nodes_lock:
        if not _nodes:
            return None
        best = min(
            _nodes.items(),
            key=lambda kv: kv[1].get("task_count", 999999),
        )
        return best[0]


# ============================================================
# 任务分发 — 核心
# ============================================================

@app.route("/api/tasks", methods=["POST"])
def dispatch_task():
    """
    前端提交任务。
    Body: {platform, level_id, count, type}
    Header: Authorization: Bearer <token>
    """
    data = request.json or {}
    token = request.headers.get("Authorization", "")
    expected = f"Bearer {API_TOKEN}"
    if token != expected:
        return jsonify({"status": "error", "message": "unauthorized"}), 403

    platform = data.get("platform", "").lower()
    level_id = data.get("level_id", "").strip()
    count = int(data.get("count", 0))
    task_type = data.get("type", "like")

    if not platform or not level_id or count <= 0:
        return jsonify({"status": "error", "message": "参数不完整"}), 400

    node_name = _pick_best_node()
    if not node_name:
        return jsonify({"status": "error", "message": "暂无可用节点，请稍后再试！"}), 503

    node_info = _nodes[node_name]
    node_url = f"http://{node_info['public_ip']}:{node_info['listen_port']}"

    import uuid
    task_id = str(uuid.uuid4())[:8]

    payload = {
        "task_id": task_id,
        "platform": platform,
        "level_id": level_id,
        "total_count": count,
        "task_type": task_type,
    }

    try:
        resp = requests.post(
            f"{node_url}/api/v2/task/submit",
            json=payload,
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "queued":
                with _nodes_lock:
                    _nodes[node_name]["task_count"] = _nodes[node_name].get("task_count", 0) + 1
                logger.info("任务 %s 分发到 %s (%s %s x%d)",
                           task_id, node_name, platform, task_type, count)
                return jsonify({
                    "status": "success",
                    "task_id": task_id,
                    "node": {
                        "name": node_name,
                        "public_ip": node_info.get("public_ip"),
                    },
                    "node_assigned": node_name,
                })
        raise Exception(f"节点返回 {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error("分发到 %s 失败: %s", node_name, e)
        return jsonify({"status": "error", "message": "节点响应异常，请稍后重试"}), 503


# ============================================================
# 节点管理
# ============================================================

@app.route("/nodes/register", methods=["POST"])
def node_register():
    """任务节点注册"""
    data = request.json or {}
    node_name = data.get("node_name", "").strip()
    if not node_name:
        return jsonify({"status": "error", "message": "缺少 node_name"}), 400

    public_ip = request.remote_addr
    listen_port = data.get("listen_port", 39902)
    version = data.get("version", "unknown")
    runtime_mode = data.get("runtime_mode", "active")

    with _nodes_lock:
        old = _nodes.get(node_name, {})
        _nodes[node_name] = {
            "name": node_name,
            "public_ip": public_ip,
            "listen_port": listen_port,
            "version": version,
            "runtime_mode": runtime_mode,
            "task_count": data.get("task_count", old.get("task_count", 0)),
            "registered_at": time.time(),
            "last_heartbeat": time.time(),
        }

    logger.info("节点注册: %s @ %s:%d (mode=%s)",
               node_name, public_ip, listen_port, runtime_mode)
    return jsonify({"status": "ok", "mode": runtime_mode})


@app.route("/nodes/heartbeat", methods=["POST"])
def node_heartbeat():
    """任务节点心跳"""
    data = request.json or {}
    node_name = data.get("node_name", "").strip()
    now = time.time()

    with _nodes_lock:
        if node_name not in _nodes:
            public_ip = request.remote_addr
            _nodes[node_name] = {
                "name": node_name,
                "public_ip": public_ip,
                "listen_port": 39902,
                "version": "unknown",
                "runtime_mode": "active",
                "task_count": 0,
                "registered_at": now,
                "last_heartbeat": now,
            }

        info = _nodes[node_name]
        info["last_heartbeat"] = now
        info["runtime_mode"] = data.get("runtime_mode", info.get("runtime_mode", "active"))
        info["task_count"] = data.get("task_count", info.get("task_count", 0))

    # 同步封禁
    response = {
        "status": "ok",
        "mode": info.get("runtime_mode", "active"),
    }

    return jsonify(response)


@app.route("/nodes/list", methods=["GET"])
def node_list():
    """查看所有节点状态"""
    _clean_stale_nodes()
    nodes = []
    with _nodes_lock:
        for name, info in _nodes.items():
            nodes.append({
                "name": name,
                "public_ip": info.get("public_ip"),
                "port": info.get("listen_port"),
                "runtime_mode": info.get("runtime_mode", "unknown"),
                "task_count": info.get("task_count", 0),
                "last_heartbeat": info.get("last_heartbeat", 0),
                "seconds_since_heartbeat": time.time() - info.get("last_heartbeat", 0),
            })
    return jsonify({"nodes": nodes, "total": len(nodes)})


# ============================================================
# 账号异常上报
# ============================================================

@app.route("/internal/accounts/report-error", methods=["POST"])
def account_report_error():
    """节点上报账号异常 (40024 etc)"""
    data = request.json or {}
    items = data.get("items", [])
    with _banned_lock:
        for item in items:
            pi = item.get("pi", "")
            if pi:
                _banned_accounts[pi] = {
                    "pi": pi,
                    "ui": item.get("ui", ""),
                    "platform": item.get("platform", ""),
                    "reason": item.get("reason", ""),
                    "reported_at": time.time(),
                }
    logger.info("收到%d条账号异常上报", len(items))
    return jsonify({"status": "ok", "count": len(items)})


@app.route("/internal/accounts/banned", methods=["GET"])
def account_banned_list():
    """查询封禁列表"""
    return jsonify({"accounts": list(_banned_accounts.values()), "version": int(time.time())})


@app.route("/internal/accounts/ns/batch", methods=["POST"])
def account_ns_batch():
    """接收 ns 上传"""
    return jsonify({"status": "ok"})


@app.route("/internal/accounts/chain-state", methods=["GET"])
def account_chain_state():
    """查询最新 chain_state"""
    return jsonify({"accounts": []})


# ============================================================
# 启动
# ============================================================

def run(port: int = 39900):
    logger.info("调度层启动，端口 %d", port)
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=16)
    except ImportError:
        app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()