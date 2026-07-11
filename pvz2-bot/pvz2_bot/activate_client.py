"""
账号激活客户端 — 从 Account Manager 领号 → 云端激活 → 上报状态

流程:
  安卓: V206 → V900 → V303 → V312
  iOS:  V303 (pi≠ui)

批量状态上报通过后台线程合并发送到 Account Manager。
"""
import logging
import threading
import time
import queue as _queue_mod
from typing import Optional

import requests

from .config import ACCOUNT_API_BASE, DEFAULT_SECRET
from .cloud_client import (
    call, payload_v206, payload_v900, payload_v303, payload_v312,
)
from .crypto import render_eu

logger = logging.getLogger("activate_client")


# ============================================================
# 全局 HTTP Session
# ============================================================
_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})


# ============================================================
# 批量状态上报队列
# ============================================================
_status_queue = _queue_mod.Queue()


def _status_uploader():
    """后台线程：批量上报状态变更"""
    while True:
        batch = []
        try:
            item = _status_queue.get(timeout=3)
            batch.append(item)
            deadline = time.time() + 0.5
            while len(batch) < 50:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    batch.append(_status_queue.get(timeout=remaining))
                except _queue_mod.Empty:
                    break
        except _queue_mod.Empty:
            continue

        try:
            resp = _session.post(
                f"{ACCOUNT_API_BASE}/api/accounts/batch/status",
                json={"updates": batch},
                timeout=15,
            )
            result = resp.json()
            ok = result.get("success", 0)
            errs = result.get("errors", [])
            logger.info("批量上报(%d条): 成功=%d, 错误=%s", len(batch), ok, errs)
        except Exception as e:
            logger.error("批量上报失败: %s", e)


def _report_status(account_id: int, status: str):
    """入队状态变更"""
    _status_queue.put({"id": account_id, "status": status})


# ============================================================
# 账号领取
# ============================================================

def fetch_account(platform: str) -> tuple:
    """
    从 Account Manager 领取一个 inactive 账号。
    Returns: (account_id, ui, sk, pi, secret) 或全 None
    """
    try:
        resp = _session.get(
            f"{ACCOUNT_API_BASE}/api/accounts/distribute",
            params={"platform": platform, "mark_used": "false"},
            timeout=10,
        )
        if resp.status_code == 404:
            return None, None, None, None, None
        resp.raise_for_status()
        data = resp.json()
        account_id = data["id"]
        ui = data["ui"]
        sk = data["sk"]
        pi = data.get("pi") or ui
        secret = data.get("secret") or DEFAULT_SECRET
        return account_id, ui, sk, pi, secret
    except requests.exceptions.ConnectionError:
        logger.error("无法连接 Account Manager (%s)", ACCOUNT_API_BASE)
        return None, None, None, None, None
    except Exception as e:
        logger.error("领取账号异常: %s", e)
        return None, None, None, None, None


# ============================================================
# 激活逻辑
# ============================================================

def activate_android(ui: str, sk: str, secret: str, session: requests.Session) -> bool:
    """
    安卓激活流程: V206 → V900 → V303 → V312

    注意: V206 需要 pr/s 参数（4399登录抓包值），
    这里使用原代码中的硬编码值。
    """
    # V206
    v206_payload = payload_v206(ui, sk)
    success, reason, _, _ = call(session, "V206", v206_payload, secret, ui, "android")
    if not success:
        logger.error("[安卓] V206 失败: %s", reason)
        return False

    # V900
    v900_payload = payload_v900(ui, ui, sk)
    call(session, "V900", v900_payload, secret, ui, "android")
    # V900 不阻挡流程

    # V303
    v303_payload = payload_v303(ui, sk, ui)
    success, reason, _, _ = call(session, "V303", v303_payload, secret, ui, "android")
    if not success:
        logger.error("[安卓] V303 失败: %s", reason)

    # V312
    v312_payload = payload_v312(ui, sk)
    success2, reason2, _, _ = call(session, "V312", v312_payload, secret, ui, "android")
    if not success2:
        logger.error("[安卓] V312 失败: %s", reason2)

    return True


def activate_ios(ui: str, sk: str, pi: str, secret: str, session: requests.Session) -> bool:
    """iOS 激活: 只发 V303 (pi≠ui)"""
    v303_payload = payload_v303(pi, sk, ui=ui)
    success, reason, _, _ = call(session, "V303", v303_payload, secret, ui, "ios")
    if not success:
        logger.error("[iOS] V303 失败: %s", reason)
        return False
    return True


# ============================================================
# 平台主循环
# ============================================================

def platform_loop(platform: str, label: str):
    """单平台持续激活循环"""
    session = requests.Session()
    session.trust_env = False
    success_count = 0
    failure_count = 0
    no_account_wait = 0

    logger.info("[%s] 启动激活循环", label)

    while True:
        account_id, ui, sk, pi, secret = fetch_account(platform)
        if account_id is None:
            no_account_wait += 1
            if no_account_wait % 6 == 1:
                logger.info("[%s] 无可用账号，等待中... (连续%d次)", label, no_account_wait)
            time.sleep(5)
            continue

        no_account_wait = 0
        logger.info("[%s] 领取账号 id=%d ui=%s", label, account_id, ui[:8])

        if platform == "ios":
            ok = activate_ios(ui, sk, pi, secret, session)
        else:
            ok = activate_android(ui, sk, secret, session)

        if ok:
            _report_status(account_id, "activated")
            success_count += 1
            logger.info("[%s] ✅ 成功=%d 失败=%d", label, success_count, failure_count)
        else:
            _report_status(account_id, "inactive")
            failure_count += 1
            logger.warning("[%s] ❌ 成功=%d 失败=%d", label, success_count, failure_count)

        if (success_count + failure_count) % 10 == 0:
            try:
                stats = _session.get(
                    f"{ACCOUNT_API_BASE}/api/accounts/stats", timeout=5
                ).json()
                p = stats.get(platform, {})
                logger.info("[%s] 统计: inactive=%d activated=%d used=%d",
                           label, p.get("inactive", 0),
                           p.get("activated", 0), p.get("used", 0))
            except Exception:
                pass


# ============================================================
# 入口
# ============================================================

def run():
    """启动双平台激活客户端（阻塞）"""
    logger.info("启动双平台激活客户端 → %s", ACCOUNT_API_BASE)

    t_status = threading.Thread(target=_status_uploader, name="StatusUploader", daemon=True)
    t_status.start()

    t_android = threading.Thread(
        target=platform_loop, args=("android", "安卓"), name="AndroidActivator", daemon=True,
    )
    t_ios = threading.Thread(
        target=platform_loop, args=("ios", "iOS"), name="iOSActivator", daemon=True,
    )
    t_android.start()
    t_ios.start()

    logger.info("双平台激活就绪")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到退出信号")
        pass