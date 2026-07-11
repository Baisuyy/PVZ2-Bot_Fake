"""
邀请码填写服务 — Flask API (端口 5000)

从 Account Manager 领取已激活账号 → V876 填写邀请码 → 返回统计

API:
  POST /api/invite  {platform: "android"|"ios", invite_code: "xxx"}
"""
import logging
import threading
import time

import requests
from flask import Flask, request, jsonify

from .config import (
    ACCOUNT_API_BASE, PLATFORMS, DEFAULT_SECRET,
    INVITE_SERVICE_PORT, INVITE_TARGET_COUNT,
)
from .cloud_client import call, payload_v876

logger = logging.getLogger("invite_service")

app = Flask(__name__)
_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})


# ============================================================
# 账号领取
# ============================================================

def fetch_activated_accounts(platform: str, count: int = 12) -> list:
    """
    循环调用 Account Manager /distribute 获取已激活账号。
    每次返回1个，需要多次调用。
    Returns: [(account_id, ui, sk, pi, secret), ...]
    """
    accounts = []
    for _ in range(count):
        try:
            resp = _session.get(
                f"{ACCOUNT_API_BASE}/api/accounts/distribute",
                params={"platform": platform, "purpose": "invite"},
                timeout=10,
            )
            if resp.status_code == 404:
                logger.info("账号池已耗尽，已获取 %d 个", len(accounts))
                break
            resp.raise_for_status()
            data = resp.json()
            accounts.append((
                data["id"],
                data["ui"],
                data["sk"],
                data.get("pi") or data["ui"],
                data.get("secret") or DEFAULT_SECRET,
            ))
        except Exception as e:
            logger.error("获取账号失败: %s", e)
            break
    return accounts


# ============================================================
# 邀请码填写
# ============================================================

def fill_invite_code(ui: str, sk: str, pi: str, invite_code: str,
                     platform: str, secret: str, session: requests.Session) -> bool:
    """V876 填写邀请码"""
    payload = payload_v876(pi, sk, ui, invite_code)
    success, reason, decoded, status = call(session, "V876", payload, secret, ui, platform)
    if success:
        return True
    logger.error("[%s] V876 失败: r=%s, reason=%s",
                 platform, decoded.get("r") if decoded else "?", reason)
    return False


# ============================================================
# 批量执行
# ============================================================

def execute_invite(platform: str, invite_code: str, count: int = 12) -> dict:
    label = PLATFORMS[platform]["name"]
    success_count = 0
    failure_count = 0
    used_ids = []

    logger.info("[%s] 开始邀请，目标: %d 个", label, count)
    accounts = fetch_activated_accounts(platform, count)
    if not accounts:
        return {"success": 0, "fail": 0, "used": [], "error": "无可用已激活账号"}

    logger.info("[%s] 获取到 %d 个账号", label, len(accounts))
    session = requests.Session()

    for account_id, ui, sk, pi, secret in accounts:
        if success_count >= count:
            break
        ok = fill_invite_code(ui, sk, pi, invite_code, platform, secret, session)
        if ok:
            used_ids.append(account_id)
            success_count += 1
            logger.info("[%s] ✅ 进度: %d/%d", label, success_count, count)
        else:
            failure_count += 1
            logger.warning("[%s] ❌ 失败", label)

    return {"success": success_count, "fail": failure_count, "used": used_ids}


# ============================================================
# Flask API
# ============================================================

@app.route("/api/invite", methods=["POST"])
def api_invite():
    data = request.json or {}
    platform = data.get("platform", "").lower().strip()
    invite_code = data.get("invite_code", "").strip()

    if not platform or not invite_code:
        return jsonify({"status": "error", "message": "缺少 platform 或 invite_code"}), 400

    if platform not in ("android", "ios"):
        return jsonify({"status": "error", "message": "platform 必须为 android 或 ios"}), 400

    pattern = PLATFORMS[platform]["invite_pattern"]
    if not pattern.match(invite_code):
        if platform == "android":
            msg = "安卓邀请码必须为纯字母"
        else:
            msg = "iOS 邀请码必须为纯数字"
        return jsonify({"status": "error", "message": msg}), 400

    try:
        result = execute_invite(platform, invite_code, INVITE_TARGET_COUNT)
        if "error" in result:
            return jsonify({"status": "error", "message": result["error"]}), 500
        return jsonify({
            "status": "success",
            "platform": platform,
            "invite_code": invite_code,
            "success_count": result["success"],
            "fail_count": result["fail"],
            "used_accounts": result["used"],
        })
    except Exception as e:
        logger.exception("邀请执行异常")
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
# 启动
# ============================================================

def run(port: int | None = None):
    port = port or INVITE_SERVICE_PORT
    print(f"🚀 邀请服务启动，端口 {port}")
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=16)
    except ImportError:
        app.run(host="0.0.0.0", port=port, debug=False)