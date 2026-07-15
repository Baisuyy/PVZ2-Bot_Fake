import hashlib
import json
import uuid

import requests

from CNNetwork import DES加解密
from login_failure_logger import 记录登录失败

TOUWEI_LOGIN_URL = "https://tgpay.talkyun.com.cn/tw-sdk/sdk-api/user/loginNew"
TOUWEI_LOGIN_SALT = "b0b29851-b8a1-4df5-abcb-a8ea158bea20"
TOUWEI_HEAD = "C2AADAA23B2D1DB95AD4F18BA6BAF35FD4C3127E361C909A1E68365A998B3E67E309847F81C45A74D7A8BADB8D498746AEEC39DEA89B9737"


def _encrypt(text: str) -> str:
    return DES加解密(text).upper()


def _decrypt(text: str) -> str:
    return DES加解密(text)


def 登录拓维账号(phone: str, password: str, timeout: int = 15) -> dict:
    phone = (phone or "").strip()
    password = (password or "").strip()
    if not phone or not password:
        raise ValueError("请填写拓维手机号和密码")

    payload = {
        "password": password,
        "phone": phone,
        "token": str(uuid.uuid4()),
    }
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encrypted_login = _encrypt(raw_json)
    sign = hashlib.md5((raw_json + TOUWEI_LOGIN_SALT).encode("utf-8")).hexdigest()

    try:
        resp = requests.post(
            TOUWEI_LOGIN_URL,
            data={"head": TOUWEI_HEAD, "login": encrypted_login, "md5": sign},
            timeout=timeout,
        )
        resp.raise_for_status()
    except Exception as error:
        记录登录失败("拓维", "渠道鉴权", str(error), {"phone": phone})
        raise RuntimeError(f"拓维登录请求失败: {error}") from error

    try:
        outer_text = _decrypt(resp.text)
        outer_obj = json.loads(outer_text)
        content_encrypted = outer_obj.get("content")
        if not content_encrypted:
            raise ValueError("拓维响应缺少content")
        content_text = _decrypt(str(content_encrypted))
        content_obj = json.loads(content_text)
    except Exception as error:
        记录登录失败("拓维", "响应解析", str(error), {"phone": phone})
        raise RuntimeError(f"拓维响应解析失败: {error}") from error

    token = str(content_obj.get("token") or "").strip()
    tw_user_id = str(content_obj.get("userId") or "").strip()
    if not token or not tw_user_id:
        raise RuntimeError("拓维响应缺少 token 或 userId")

    return {
        "token": token,
        "tw_user_id": tw_user_id,
        "raw": content_obj,
    }
