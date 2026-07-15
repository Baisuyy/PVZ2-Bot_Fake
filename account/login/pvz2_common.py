import json
import os
from dataclasses import dataclass
from typing import Dict, Optional

import requests

from CNNetwork import DEFAULT_SECRET, decode_cloud_response, rsa_encrypt_v202
from login_failure_logger import 记录登录失败
from proxy_pool import 启用全局代理

启用全局代理()

APP_ID = "109"
APP_VERSION = "1.0"
TALKWEB_SDK_VERSION = "3.0.0"
DEFAULT_CV = "4.12"
DEFAULT_TIMEOUT = 15
CLOUD_URL = "http://cloudpvz2android.ditwan.cn/index.php"
EK_SECRET = os.getenv("PVZ2_EK_SECRET", DEFAULT_SECRET)


@dataclass(frozen=True)
class 渠道配置:
    key: str
    ci: str
    channel_id: str
    channel_sdk_version: str
    li: str
    r: str
    s: str
    ev: int = 3
    cv: Optional[str] = None


渠道配置表: Dict[str, 渠道配置] = {
    "4399": 渠道配置(
        key="4399",
        ci="91",
        channel_id="54",
        channel_sdk_version="dj2.2-3.14.4.574",
        li="e5b02d30ba2ceed09f3b892f92bdafef",
        r="1141076184",
        s="367de9ed556fd49e36619ec233bd6180",
        cv="9.9.3.1625",
    ),
    "九游": 渠道配置(
        key="九游",
        ci="72",
        channel_id="24",
        channel_sdk_version="dj2.0-9.7.12.0_7.7.1.0",
        li="096f265b89496f4a02a65f9cdd6d4a40",
        r="2147459649",
        s="b16c38837cb24b41b4e213f68c2f92ca",
        cv="4.12",
    ),
    "应用宝": 渠道配置(
        key="应用宝",
        ci="113",
        channel_id="132",
        channel_sdk_version="dj2.0-2.2.3",
        li="096f265b89496f4a02a65f9cdd6d4a40",
        r="2147459649",
        s="b16c38837cb24b41b4e213f68c2f92ca",
        cv="4.12",
    ),
    "vivo": 渠道配置(
        key="vivo",
        ci="103",
        channel_id="1027",
        channel_sdk_version="dj2.0-4.8.2.0",
        li="096f265b89496f4a02a65f9cdd6d4a40",
        r="2147459649",
        s="b16c38837cb24b41b4e213f68c2f92ca",
        cv="4.12",
    ),
    "miui": 渠道配置(
        key="miui",
        ci="74",
        channel_id="1013",
        channel_sdk_version="dj2.0-3.4.6",
        li="096f265b89496f4a02a65f9cdd6d4a40",
        r="2147459649",
        s="b16c38837cb24b41b4e213f68c2f92ca",
        cv="4.12",
    ),
    "拓维官服": 渠道配置(
        key="拓维官服",
        ci="0",
        channel_id="208",
        channel_sdk_version="dj2.0-2.0.0",
        li="c9f9d8ea4c0051af841b55bb96bb4350",
        r="1744646338",
        s="11d1c214a4c18be2ca615fb44bb73409",
        cv="9.9.9.1695",
    ),
    "拓维Tap": 渠道配置(
        key="拓维Tap",
        ci="93",
        channel_id="250",
        channel_sdk_version="dj2.0-2.0.0",
        li="ccb93ff163d4d43da05913487aa0d2c9",
        r="343781003",
        s="a1e4c20b0d5fb13d4acf3267d921b7af",
        cv="9.9.9.1695",
    ),
    "拓维好游快爆": 渠道配置(
        key="拓维好游快爆",
        ci="93",
        channel_id="261",
        channel_sdk_version="dj2.0-2.0.0",
        li="ccb93ff163d4d43da05913487aa0d2c9",
        r="96173049",
        s="4dc01917d93618bd38fdb9b6ca936f1a",
        cv="9.9.9.1695",
    ),
}


def 可用渠道():
    return list(渠道配置表.keys())


def 构建V202请求(channel_key: str, account_id: str, token: str, cv: Optional[str] = None):
    if channel_key not in 渠道配置表:
        raise ValueError(f"未知渠道: {channel_key}")

    配置 = 渠道配置表[channel_key]
    version = cv or 配置.cv or DEFAULT_CV

    return {
        "ci": 配置.ci,
        "cv": version,
        "di": "",
        "ek": EK_SECRET,
        "head": {
            "appId": APP_ID,
            "appVersion": APP_VERSION,
            "channelId": 配置.channel_id,
            "channelSdkVersion": 配置.channel_sdk_version,
            "talkwebSdkVersion": TALKWEB_SDK_VERSION,
        },
        "li": 配置.li,
        "oi": f"{APP_ID}{配置.channel_id}X{account_id}",
        "pi": "",
        "r": 配置.r,
        "s": 配置.s,
        "t": token,
        "ui": "",
    }


def 请求UI_SK(channel_key: str, account_id: str, token: str, cv: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
    payload = 构建V202请求(channel_key=channel_key, account_id=account_id, token=token, cv=cv)
    try:
        encrypted_payload = rsa_encrypt_v202(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        response = requests.post(
            CLOUD_URL,
            data={"req": "V202", "e": encrypted_payload, "ev": 渠道配置表[channel_key].ev},
            timeout=timeout,
        )
        response.raise_for_status()

        try:
            raw_response = response.json()
        except json.JSONDecodeError:
            raw_preview = (response.text or "")[:160]
            raise ValueError(f"游戏服响应非JSON: {raw_preview!r}; channel={channel_key}, cv={payload['cv']}")
        try:
            parsed = decode_cloud_response(raw_response, EK_SECRET)
        except Exception as decrypt_error:
            raw_preview = (response.text or "")[:160]
            raise ValueError(
                f"游戏服响应解密失败: {decrypt_error}; raw前缀: {raw_preview!r}; "
                f"channel={channel_key}, cv={payload['cv']}"
            )

        if "d" not in parsed:
            raise ValueError("游戏服响应缺少 d")

        data = parsed["d"]
        return {
            "ui": data.get("ui", ""),
            "sk": data.get("sk", ""),
            "ek": EK_SECRET,
            "raw": parsed,
            "payload": payload,
        }
    except Exception as error:
        记录登录失败(
            channel_key,
            "请求V202",
            str(error),
            {"account_id": account_id, "cv": payload["cv"]},
        )
        raise
