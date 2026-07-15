"""
PVZ2 云端 API 客户端 — 所有加密请求的统一入口

支持所有 API 命令码: V201, V203, V206, V303, V312, V722, V733, V726, V876, V900
"""
import json
import logging
import random
import time
from typing import Optional

import requests

from .config import (
    ANDROID_CLOUD_URL, IOS_CLOUD_URL, IOS_REGISTER_URL,
    PLATFORMS, DEFAULT_SECRET,
    REQUEST_TIMEOUT, MIN_REQUEST_INTERVAL, MAX_REQUEST_INTERVAL, REQUEST_JITTER,
    MAX_RETRY_COUNT, RETRY_BASE_DELAY, RETRY_BACKOFF_FACTOR,
)
from .crypto import encrypt, decode_cloud_response, render_eu

logger = logging.getLogger("cloud_client")

_USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; SM-S908E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.7 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Dalvik/2.1.0 (Linux; U; Android 13; SM-G991B Build/TP1A.220624.014)",
    "Dalvik/2.1.0 (Linux; U; Android 12; SM-S908E Build/SP1A.210812.016)",
]

_RETRYABLE_REASONS = {
    "timeout", "request_error", "unknown",
    "http_5xx", "http_429", "http_408",
}


def random_user_agent() -> str:
    return random.choice(_USER_AGENTS)


def _random_sleep_duration() -> float:
    base = random.uniform(MIN_REQUEST_INTERVAL, MAX_REQUEST_INTERVAL)
    jitter = random.uniform(-REQUEST_JITTER, REQUEST_JITTER)
    return max(MIN_REQUEST_INTERVAL * 0.5, base + jitter)


def _build_payload(acc: dict, level_id: str, platform: str) -> dict:
    """构造通用请求 payload: {id, pi, sk, ui, t} + extra_params"""
    payload = {
        "id": level_id,
        "pi": acc["pi"],
        "sk": acc["sk"],
        "ui": acc["ui"],
        "t": "1",
    }
    payload.update(PLATFORMS[platform]["extra_params"])
    return payload


def _single_call(
    session: requests.Session,
    req_code: str,
    payload: dict,
    secret: str,
    ui: str,
    platform: str,
) -> tuple[bool, str, dict | None, int | None]:
    base_url = PLATFORMS[platform]["base_url"]

    try:
        plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        e = encrypt(plain, secret, req_code)
        eu = render_eu(ui, secret, req_code) if ui else ""
        headers = {"eu": eu} if eu else {}
        headers["User-Agent"] = random_user_agent()

        resp = session.post(
            base_url,
            data={"req": req_code, "e": e, "ev": 3},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

        if resp.status_code == 403:
            return False, "http_403", None, 403
        if resp.status_code == 429:
            return False, "http_429", None, 429
        if resp.status_code == 408:
            return False, "http_408", None, 408
        if resp.status_code >= 500:
            return False, "http_5xx", None, resp.status_code
        if resp.status_code >= 400:
            return False, f"http_{resp.status_code}", None, resp.status_code

        try:
            resp_json = resp.json()
        except ValueError:
            return False, "invalid_json", None, resp.status_code

        decrypted, decrypt_error = decode_cloud_response(resp_json, [secret, DEFAULT_SECRET])
        if decrypt_error:
            return False, "decrypt_error", None, resp.status_code

        code = decrypted.get("r")
        if code in (0, 75051):
            return True, "ok", decrypted, resp.status_code
        return False, f"business_{code}", decrypted, resp.status_code

    except requests.Timeout:
        return False, "timeout", None, None
    except requests.RequestException:
        return False, "request_error", None, None
    except Exception:
        return False, "unknown", None, None


def call(
    session: requests.Session,
    req_code: str,
    payload: dict,
    secret: str,
    ui: str = "",
    platform: str = "android",
) -> tuple[bool, str, dict | None, int | None]:
    """
    发送加密请求到 PVZ2 云端（带智能重试 + 反检测伪装）。

    重试策略:
      - 仅对可重试错误（超时、网络错误、5xx、429）进行重试
      - 指数退避: base_delay * (backoff_factor ^ attempt)
      - 每次重试都随机化 User-Agent
      - 请求间隔随机化并加入抖动

    Args:
        session: 复用连接池
        req_code: API命令码 (V722/V733/V900/V876/V303/V312/V201/V203 等)
        payload: 明文payload dict (不含req/ev)
        secret: AES密钥
        ui: 用于生成eu头
        platform: android/ios

    Returns:
        (success, reason, decoded_body_or_None, http_status_code)
    """
    start = time.time()
    last_result: tuple = (False, "unknown", None, None)
    attempts = 0

    for attempt in range(MAX_RETRY_COUNT + 1):
        attempts = attempt + 1
        success, reason, body, status = _single_call(
            session, req_code, payload, secret, ui, platform
        )
        last_result = (success, reason, body, status)

        if success:
            break

        if reason not in _RETRYABLE_REASONS:
            break

        if attempt < MAX_RETRY_COUNT:
            delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
            delay = delay * random.uniform(0.7, 1.3)
            logger.debug(
                "请求失败(%s)，第%d/%d次重试，延迟%.2fs",
                reason, attempt + 1, MAX_RETRY_COUNT, delay,
            )
            time.sleep(delay)

    elapsed = time.time() - start
    sleep_time = _random_sleep_duration() - elapsed
    if sleep_time > 0:
        time.sleep(sleep_time)

    if attempts > 1:
        logger.debug("请求共尝试%d次，最终结果: %s", attempts, last_result[1])

    return last_result


# ============================================================
# 预定义的 Payload 构建器
# ============================================================

def payload_v722_v733(acc: dict, level_id: str, platform: str = "android") -> dict:
    """V722(点赞)/V733(游玩) payload"""
    return _build_payload(acc, level_id, platform)


def payload_v900(pi: str, ui: str, sk: str) -> dict:
    """V900 初始化 payload: pi/ui/sk + pl(10种道具x数量)"""
    return {
        "pi": pi,
        "ui": ui,
        "sk": sk,
        "pl": [
            {"i": 1101, "q": 10}, {"i": 1001, "q": 1},
            {"i": 1102, "q": 10}, {"i": 1002, "q": 1},
            {"i": 1103, "q": 10}, {"i": 1003, "q": 1},
            {"i": 1104, "q": 10}, {"i": 1004, "q": 1},
            {"i": 1105, "q": 10}, {"i": 1005, "q": 1},
        ],
    }


def payload_v876(pi: str, sk: str, ui: str, invite_code: str) -> dict:
    """V876 邀请码填写"""
    return {
        "code": str(invite_code),
        "pi": pi,
        "sk": sk,
        "star": "66",
        "ui": ui,
    }


def payload_v303(pi: str, sk: str, ui: str, ver: str = "9.9.3") -> dict:
    """V303 进度加载"""
    return {
        "al": [{"id": 10868, "abi": 0, "type": 1, "config_version": 1}],
        "ci": "93",
        "cs": "0",
        "pack": "com.popcap.pvz2cthdbk",
        "pi": pi,
        "sk": sk,
        "ui": ui,
        "v": ver,
        "_ver": ver,
    }


def payload_v312(ui: str, sk: str, name: str = "拓小维") -> dict:
    """V312 改名（安卓激活）"""
    return {
        "n": name,
        "pi": ui,
        "sk": sk,
        "ui": ui,
        "ver_": "newest_version_1",
        "_ver": "9.9.3",
    }


def payload_v206(ui: str, sk: str, name: str = "拓小维", m_hash: str = "9d8fe592ba17620784befca768f69e23",
                 pr: str = "") -> dict:
    """V206 4399登录（安卓激活）

    pr 和 s 是从实际登录抓包获取的加密进度和签名字符串。
    """
    return {
        "m": m_hash,
        "pi": ui,
        "n": name,
        "pcl": {},
        "sk": sk,
        "ui": ui,
        "_ver": "9.9.3",
    }


def payload_v203(ui: str, sk: str, pr: str, s: str) -> dict:
    """V203 iOS进度设置"""
    return {
        "_id": 203,
        "c": "0",
        "cl": [],
        "dcl": {},
        "dl": [],
        "g": "0",
        "m": "9d8fe592ba17620784befca768f69e23",
        "n": "自信的bird",
        "pcl": {},
        "pl": [1001, 1002, 1003, 1004],
        "pr": pr,
        "s": s,
        "sk": sk,
        "ui": ui,
    }