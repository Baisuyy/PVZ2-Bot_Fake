"""
iOS 自动注册 — V201→V203 完整流程

流程:
  V201: RSA加密注册请求 → ui + sk
  V203: AES加密进度设置 → pi

自动批量上传到 Account Manager。
"""
import hashlib
import logging
import random
import threading
import time

import requests

from .config import IOS_REGISTER_URL, ACCOUNT_API_BASE, DEFAULT_SECRET, RSA_PUBLIC_KEY_PEM
from .crypto import (
    encrypt, decrypt,
    to_std_b64, from_std_b64,
    render_eu,
)
from .cloud_client import payload_v203
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
import base64

logger = logging.getLogger("ios_register")

# ============================================================
# RSA 加密
# ============================================================

def rsa_encrypt(plain: str) -> str:
    key = RSA.import_key(RSA_PUBLIC_KEY_PEM)
    cipher = PKCS1_v1_5.new(key)
    block_size = key.size_in_bytes() - 11
    plain_bytes = plain.encode("utf-8")
    encrypted = b"".join(
        cipher.encrypt(plain_bytes[i:i + block_size])
        for i in range(0, len(plain_bytes), block_size)
    )
    return from_std_b64(base64.b64encode(encrypted).decode("ascii"))


# ============================================================
# V201 注册请求构建
# ============================================================

def build_v201() -> tuple:
    """
    构建 V201 注册请求

    Returns:
        (udid, body_dict, encrypted_e)
    """
    udid = str(random.randint(1000000000000, 9999990000000000))
    device_id = hashlib.md5(udid.encode("utf-8")).hexdigest()
    r = str(random.randint(10009999999000000, 20009999999000000))
    sig = hashlib.md5((device_id + r + "B7108D8B5TABE").encode("utf-8")).hexdigest()

    body = {
        "ek": DEFAULT_SECRET,
        "cv": "9.9.3",
        "kr": "1",
        "di": device_id,
        "r": r,
        "s": sig,
        "_ver": "9.9.3",
    }

    import json
    plain = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    encrypted = rsa_encrypt(plain)
    return udid, body, encrypted


# ============================================================
# 云端请求
# ============================================================

def ios_post(req: str, e: str, ev: int = 3, eu: str = "") -> dict:
    headers = {}
    if eu:
        headers["eu"] = eu
    resp = requests.post(
        IOS_REGISTER_URL,
        data={"req": req, "e": e, "ev": ev},
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


# ============================================================
# 批量上传缓冲区
# ============================================================

_upload_buffer: list = []
_buffer_lock = threading.Lock()
BATCH_SIZE = 50


def _do_upload(batch: list):
    """执行一次批量上传到 Account Manager"""
    try:
        accounts_payload = []
        for acc in batch:
            accounts_payload.append({
                "platform": "ios",
                "ui": str(acc["ui"]),
                "sk": str(acc["sk"]),
                "secret": acc["secret"],
                "udid": str(acc["udid"]),
                "pi": str(acc["pi"]),
            })
        resp = requests.post(
            f"{ACCOUNT_API_BASE}/api/accounts/upload/batch",
            json={"accounts": accounts_payload},
            timeout=15,
        )
        result = resp.json()
        logger.info("批量上传(%d条): 成功=%d, 跳过=%d, 错误=%s",
                   len(batch), result.get("success", 0),
                   result.get("skipped", 0), result.get("errors", []))
    except Exception as e:
        logger.error("批量上传失败: %s", e)


def save_account(ui: str, sk: str, pi: str, udid: str):
    """将账号加入缓冲区，满 BATCH_SIZE 条自动批量上传"""
    account = {
        "ui": str(ui),
        "sk": str(sk),
        "secret": DEFAULT_SECRET,
        "udid": str(udid),
        "pi": str(pi),
    }
    batch_to_send = None
    with _buffer_lock:
        _upload_buffer.append(account)
        if len(_upload_buffer) >= BATCH_SIZE:
            batch_to_send = _upload_buffer[:]
            _upload_buffer.clear()
    if batch_to_send:
        _do_upload(batch_to_send)


def flush_upload_buffer():
    """上传缓冲区中剩余的账号"""
    with _buffer_lock:
        batch = _upload_buffer[:]
        _upload_buffer.clear()
    if batch:
        _do_upload(batch)


# ============================================================
# 暂停逻辑
# ============================================================

_last_pause_time = 0


def pause_if_needed(seconds: int = 120):
    global _last_pause_time
    now = time.time()
    if now - _last_pause_time < seconds:
        return
    logger.info("暂停 %d 分钟...", seconds // 60)
    _last_pause_time = now
    time.sleep(seconds)
    logger.info("暂停结束，继续注册")


# ============================================================
# 注册一个账号
# ============================================================

def register_one() -> bool:
    """
    V201 → V203 完整流程注册一个 iOS 账号

    Returns: True/False
    """
    try:
        # V201
        udid, v201_body, v201_e = build_v201()
        resp = ios_post(req="V201", e=v201_e, ev=3)
        decoded = json.loads(decrypt(resp["e"], DEFAULT_SECRET, resp["i"]))

        if decoded.get("r") != 0:
            r = decoded.get("r")
            logger.warning("V201 失败: r=%d", r)
            if r in (20001, 20022, 10306):
                pause_if_needed(120)
            return False

        d = decoded.get("d", {})
        ui = str(d.get("ui", ""))
        sk = d.get("sk", "")
        if not ui or not sk:
            logger.error("V201 响应缺少 ui/sk")
            return False

        logger.debug("V201 成功: ui=%s sk=%s...", ui, sk[:16])

        # V203
        v203_payload = payload_v203(ui, sk,
                                    pr="H4sIAAAAAAAAE21Y...", s="eyJzZHMiOi...")
        import json as _json
        plain = _json.dumps(v203_payload, separators=(",", ":"), ensure_ascii=False)
        v203_e = encrypt(plain, DEFAULT_SECRET, "V203")
        v203_eu = render_eu(ui, DEFAULT_SECRET, "V203")

        resp = ios_post(req="V203", e=v203_e, ev=3, eu=v203_eu)
        decoded = json.loads(decrypt(resp["e"], DEFAULT_SECRET, resp["i"]))

        if decoded.get("r") != 0:
            logger.error("V203 失败: r=%d", decoded.get("r"))
            return False

        pi = str(decoded.get("d", {}).get("pi", ""))
        logger.debug("V203 成功: pi=%s", pi)

        save_account(ui, sk, pi, udid)
        return True

    except Exception as e:
        logger.exception("注册异常: %s", e)
        return False


# ============================================================
# 入口
# ============================================================

def run(total: int, sleep_seconds: float = 0.5):
    """启动 iOS 单线程注册

    Args:
        total: 目标注册数量
        sleep_seconds: 每次注册间隔（秒）
    """
    import sys
    success_count = 0

    logger.info("开始 iOS 注册，目标: %d 个，上传至: %s", total, ACCOUNT_API_BASE)

    try:
        while success_count < total:
            if register_one():
                success_count += 1
                logger.info("✅ %d/%d", success_count, total)
                time.sleep(sleep_seconds)
            else:
                logger.warning("❌ 失败 (%d/%d)", success_count, total)
                time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        logger.info("收到退出信号")
    finally:
        flush_upload_buffer()
        logger.info("完成，总成功: %d", success_count)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--total", type=int, default=1000000)
    parser.add_argument("--sleep", type=float, default=0.5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    run(args.total, args.sleep)