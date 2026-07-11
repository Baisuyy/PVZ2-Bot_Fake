"""
AES-CBC 加密模块 — 所有 PVZ2 云端通信的底层加密协议

加密流程:
  MD5(secret + req_name) → key (16 bytes)
  MD5(key)              → iv  (16 bytes)
  AES-CBC + PKCS7 pad   → base64 → URL-safe base64

URL-safe base64: '+'→'-', '/'→'_', '='→','
"""
import base64
import hashlib
import json
from typing import List

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


def to_std_b64(text: str) -> str:
    """URL-safe base64 → 标准 base64"""
    text = text.replace("-", "+").replace("_", "/").replace(",", "=")
    return text + "=" * ((4 - len(text) % 4) % 4)


def from_std_b64(text: str) -> str:
    """标准 base64 → URL-safe base64"""
    return text.replace("+", "-").replace("/", "_").replace("=", ",")


def key_iv(secret: str, req: str) -> tuple:
    """从 secret + req_name 派生 AES key 和 iv

    Args:
        secret: AES 密钥 (如 "1geh6fvq4r20M02s")
        req:    请求名称 (如 "V722", "V733", "V900", "V876")

    Returns:
        (key, iv): 各 16 bytes
    """
    key = hashlib.md5((secret + req).encode("utf-8")).hexdigest().encode("ascii")
    iv = hashlib.md5(key).hexdigest().encode("ascii")[:16]
    return key, iv


def encrypt(plain: str, secret: str, req: str) -> str:
    """AES-CBC 加密，返回 URL-safe base64 字符串

    Args:
        plain:  JSON 明文字符串
        secret: AES 密钥
        req:    请求名称 (V722/V733/V900/V876/V303/V312/V201/V203 等)

    Returns:
        URL-safe base64 密文
    """
    key, iv = key_iv(secret, req)
    encrypted = AES.new(key, AES.MODE_CBC, iv).encrypt(
        pad(plain.encode("utf-8"), AES.block_size)
    )
    return from_std_b64(base64.b64encode(encrypted).decode("ascii"))


def decrypt(encrypted_text: str, secret: str, req: str) -> str:
    """AES-CBC 解密，返回明文字符串

    Args:
        encrypted_text: URL-safe base64 密文
        secret:         AES 密钥
        req:            请求名称

    Returns:
        JSON 明文
    """
    key, iv = key_iv(secret, req)
    raw = base64.b64decode(to_std_b64(encrypted_text))
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(raw)
    return unpad(plain, AES.block_size).decode("utf-8")


def decode_cloud_response(resp_json: dict, secrets: List[str]) -> tuple:
    """解密云端响应 {i: req_name, e: encrypted_body}

    依次尝试 secrets 列表中的每个密钥。

    Args:
        resp_json: 云端返回的 JSON {i, e, ev}
        secrets:   尝试的密钥列表

    Returns:
        (decoded_dict, error_msg): 成功时 error_msg=""，失败时 decoded_dict=原始resp_json
    """
    if "e" not in resp_json:
        return resp_json, ""

    req_name = resp_json.get("i") or resp_json.get("req") or ""
    errors = []
    tried = []

    for secret in secrets:
        if not secret or secret in tried:
            continue
        tried.append(secret)
        try:
            decoded_raw = decrypt(resp_json["e"], secret, req_name)
            return json.loads(decoded_raw), ""
        except Exception as exc:
            errors.append(f"{secret[:4]}...: {exc}")

    return resp_json, "; ".join(errors) if errors else "解密失败"


def wrap_request(req_name: str, payload: dict, secret: str) -> dict:
    """构造标准加密请求 body

    Args:
        req_name: API 命令码 (V722, V733, V900, V876 等)
        payload:  明文 payload dict
        secret:   AES 密钥

    Returns:
        {"req": req_name, "e": encrypted_payload, "ev": 3}
    """
    plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return {
        "req": req_name,
        "e": encrypt(plain, secret, req_name),
        "ev": 3,
    }


def render_eu(ui: str, secret: str, req: str) -> str:
    """生成加密的 eu 头（用于 V722/V733/V876/V303/V203 等）"""
    return encrypt(ui, secret, req)