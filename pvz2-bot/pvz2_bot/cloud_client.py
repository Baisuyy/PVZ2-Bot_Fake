"""
PVZ2 云端 API 客户端 — 所有加密请求的统一入口

支持所有 API 命令码: V201, V203, V206, V303, V312, V722, V733, V726, V876, V900
"""
import json
import logging
import time
from typing import Optional

import requests

from .config import (
    ANDROID_CLOUD_URL, IOS_CLOUD_URL, IOS_REGISTER_URL,
    PLATFORMS, DEFAULT_SECRET,
    REQUEST_TIMEOUT, MIN_REQUEST_INTERVAL,
)
from .crypto import encrypt, decode_cloud_response, render_eu

logger = logging.getLogger("cloud_client")


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


def call(
    session: requests.Session,
    req_code: str,
    payload: dict,
    secret: str,
    ui: str = "",
    platform: str = "android",
) -> tuple[bool, str, dict | None, int | None]:
    """
    发送单次加密请求到 PVZ2 云端。

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
    base_url = PLATFORMS[platform]["base_url"]

    try:
        plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        e = encrypt(plain, secret, req_code)
        eu = render_eu(ui, secret, req_code) if ui else ""
        headers = {"eu": eu} if eu else None

        resp = session.post(
            base_url,
            data={"req": req_code, "e": e, "ev": 3},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

        if resp.status_code == 403:
            return False, "http_403", None, 403
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
    finally:
        elapsed = time.time() - start
        sleep_time = MIN_REQUEST_INTERVAL - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)


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