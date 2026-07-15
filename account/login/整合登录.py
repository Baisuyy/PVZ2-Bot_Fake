import argparse
import base64
import importlib.util
import json
import os
import secrets
import sys
import threading
import time
from types import SimpleNamespace

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from pvz2_common import 可用渠道, 请求UI_SK

_WEB_BASE_URL = os.getenv("PVZ2_WEB_BASE_URL", "http://dl.qxpvz.top/").rstrip("/")
_USER_AES_KEY = os.getenv("PVZ2_USER_AES_KEY", "1739902253ii")
_LOGIN_LINK_TTL_SECONDS = int(os.getenv("PVZ2_LOGIN_LINK_TTL_SECONDS", "180"))
_LOGIN_GATE_TTL_SECONDS = int(os.getenv("PVZ2_LOGIN_GATE_TTL_SECONDS", "60"))
_LOGIN_GATE_MAX_ATTEMPTS = int(os.getenv("PVZ2_LOGIN_GATE_MAX_ATTEMPTS", "3"))
_LOGIN_GATE_LOCK = threading.Lock()
_LOGIN_GATE_STORE = {}


def _pkcs7_pad(raw: bytes) -> bytes:
    block_size = 16
    pad_len = block_size - (len(raw) % block_size)
    return raw + bytes([pad_len]) * pad_len


def _cleanup_gate_locked(now_ts: float | None = None):
    now_value = now_ts if now_ts is not None else time.time()
    expired_keys = [k for k, v in _LOGIN_GATE_STORE.items() if now_value >= v.get("expires_at", 0)]
    for key in expired_keys:
        _LOGIN_GATE_STORE.pop(key, None)


def _encrypt_user_id(user_id: str, key: str, issued_at: int, expires_at: int) -> str:
    key_bytes = key.ljust(16)[:16].encode("utf-8")
    payload = json.dumps(
        {"u": str(user_id), "iat": int(issued_at), "exp": int(expires_at)},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    plain = _pkcs7_pad(payload.encode("utf-8"))
    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(plain) + encryptor.finalize()
    return base64.urlsafe_b64encode(iv + ciphertext).decode("ascii").rstrip("=")


def 生成登录页面链接(
    user_id: str,
    base_url: str | None = None,
    key: str | None = None,
    ttl_seconds: int | None = None,
) -> str:
    if not user_id:
        raise ValueError("user_id 不能为空")
    ttl = int(ttl_seconds if ttl_seconds is not None else _LOGIN_LINK_TTL_SECONDS)
    issued_at = int(time.time())
    expires_at = issued_at + max(1, ttl)
    encrypted = _encrypt_user_id(
        user_id=user_id,
        key=key or _USER_AES_KEY,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    root = (base_url or _WEB_BASE_URL).rstrip("/")
    return f"{root}/{encrypted}"


def 开启登录授权(user_id: str, ttl_seconds: int | None = None, max_attempts: int | None = None) -> dict:
    if not user_id:
        raise ValueError("user_id 不能为空")
    ttl = ttl_seconds if ttl_seconds is not None else _LOGIN_GATE_TTL_SECONDS
    attempts = max_attempts if max_attempts is not None else _LOGIN_GATE_MAX_ATTEMPTS
    now_ts = time.time()
    gate = {
        "user_id": str(user_id),
        "ticket": secrets.token_urlsafe(16),
        "expires_at": now_ts + max(1, int(ttl)),
        "remaining_attempts": max(1, int(attempts)),
        "created_at": now_ts,
    }
    with _LOGIN_GATE_LOCK:
        _cleanup_gate_locked(now_ts)
        _LOGIN_GATE_STORE[str(user_id)] = gate
    return {
        "ticket": gate["ticket"],
        "expires_at": int(gate["expires_at"]),
        "remaining_attempts": gate["remaining_attempts"],
        "ttl_seconds": int(gate["expires_at"] - now_ts),
    }


def 消耗授权并生成登录链接(user_id: str, ticket: str, base_url: str | None = None, key: str | None = None) -> dict:
    if not user_id or not ticket:
        return {"ok": False, "reason": "missing_user_or_ticket"}
    now_ts = time.time()
    with _LOGIN_GATE_LOCK:
        _cleanup_gate_locked(now_ts)
        gate = _LOGIN_GATE_STORE.get(str(user_id))
        if not gate:
            return {"ok": False, "reason": "gate_not_found"}
        if gate.get("ticket") != ticket:
            return {"ok": False, "reason": "ticket_mismatch"}
        if now_ts >= gate.get("expires_at", 0):
            _LOGIN_GATE_STORE.pop(str(user_id), None)
            return {"ok": False, "reason": "gate_expired"}
        remaining = int(gate.get("remaining_attempts", 0))
        if remaining <= 0:
            _LOGIN_GATE_STORE.pop(str(user_id), None)
            return {"ok": False, "reason": "attempts_exhausted"}

        remaining -= 1
        gate["remaining_attempts"] = remaining
        gate["last_used_at"] = now_ts
        if remaining <= 0:
            _LOGIN_GATE_STORE.pop(str(user_id), None)
        else:
            _LOGIN_GATE_STORE[str(user_id)] = gate

    login_url = 生成登录页面链接(user_id=user_id, base_url=base_url, key=key)
    return {
        "ok": True,
        "login_url": login_url,
        "remaining_attempts": remaining,
        "expires_at": int(gate["expires_at"]),
        "seconds_left": max(0, int(gate["expires_at"] - now_ts)),
    }


def 加载模块(module_alias: str, file_name: str):
    base_dir = os.path.dirname(__file__)
    path = os.path.join(base_dir, file_name)
    spec = importlib.util.spec_from_file_location(module_alias, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {file_name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def 执行token模式(args):
    result = 请求UI_SK(
        channel_key=args.channel,
        account_id=args.account_id,
        token=args.token,
        cv=args.cv,
    )
    return {"channel": args.channel, "result": result}


def 执行full模式(args):
    if args.channel == "4399":
        if not args.username or not args.password:
            raise ValueError("4399 需要 --username 和 --password")
        m = 加载模块("channel_4399", "4399登录.py")
        login_result = m.login_4399(args.username, args.password)
        if not login_result or "result" not in login_result:
            raise ValueError("4399 登录失败")
        info = login_result["result"]
        result = m.执行注册(info.get("state", ""), info.get("uid", ""))
        if "error" in result:
            raise ValueError(result["error"])
        return {"channel": "4399", "result": result}

    if args.channel == "九游":
        if not args.mobile or not args.password:
            raise ValueError("九游 需要 --mobile 和 --password")
        m = 加载模块("channel_9game", "九游.py")
        results = m.login_九游(args.mobile, args.password)
        if not results:
            raise ValueError("九游 登录失败")
        return {
            "channel": "九游",
            "accounts": [
                {"account_id": item.get("account_id", ""), "ui": item.get("ui", ""), "sk": item.get("sk", "")}
                for item in results
            ],
            "raw": results,
        }

    if args.channel == "vivo":
        if not args.vivo_json and not args.vivo_file:
            raise ValueError("vivo 需要 --vivo-json 或 --vivo-file")
        m = 加载模块("channel_vivo", "vivo.py")
        if args.vivo_file:
            with open(args.vivo_file, "r", encoding="utf-8") as f:
                data = f.read()
        else:
            data = args.vivo_json

        parsed = m.extract_vivo_info(data)
        if "error" in parsed:
            raise ValueError(parsed["error"])
        token = parsed.get("open_token")
        if not token:
            raise ValueError("vivo 未找到 openToken")

        accounts = []
        for oid in parsed.get("sub_open_ids", []):
            item = m.发送游戏请求(oid, token)
            accounts.append(
                {
                    "sub_open_id": oid,
                    "status": item.get("status"),
                    "ui": item.get("ui", ""),
                    "sk": item.get("sk", ""),
                    "message": item.get("message", ""),
                }
            )
        return {"channel": "vivo", "accounts": accounts}

    if args.channel == "miui":
        if not args.auth_code:
            raise ValueError("miui 需要 --auth-code")
        m = 加载模块("channel_miui", "miui登录指令版.py")
        result = m.处理授权流程(args.auth_code)
        return {"channel": "miui", "result": result}

    if args.channel == "应用宝":
        m = 加载模块("channel_qq", "应用宝渠道登录.py")
        result = m.login_应用宝()
        return {"channel": "应用宝", "result": result}

    raise ValueError(f"不支持的渠道: {args.channel}")


def 执行请求(params):
    args = SimpleNamespace(**params)
    if getattr(args, "mode", "token") == "token":
        if not getattr(args, "account_id", None) or not getattr(args, "token", None):
            raise ValueError("token 模式需要 account_id 和 token")
        return 执行token模式(args)
    return 执行full模式(args)


def main():
    parser = argparse.ArgumentParser(description="PVZ2 多渠道整合登录入口。")
    parser.add_argument("--mode", choices=["token", "full"], default="token", help="token=直连V202，full=完整渠道登录")
    parser.add_argument("-c", "--channel", required=True, choices=可用渠道(), help="渠道名")
    parser.add_argument("-a", "--account-id", help="token模式：渠道账号ID")
    parser.add_argument("-t", "--token", help="token模式：渠道登录令牌")
    parser.add_argument("--cv", default=None, help="可选：覆盖客户端版本号")
    parser.add_argument("--username", help="full模式-4399：账号")
    parser.add_argument("--password", help="full模式-4399/九游：密码")
    parser.add_argument("--mobile", help="full模式-九游：手机号")
    parser.add_argument("--auth-code", help="full模式-miui：授权码")
    parser.add_argument("--vivo-json", help="full模式-vivo：完整JSON字符串")
    parser.add_argument("--vivo-file", help="full模式-vivo：JSON文件路径")
    parser.add_argument("--json", action="store_true", help="以JSON输出完整结果")
    args = parser.parse_args()

    try:
        output = 执行请求(
            {
                "mode": args.mode,
                "channel": args.channel,
                "account_id": args.account_id,
                "token": args.token,
                "cv": args.cv,
                "username": args.username,
                "password": args.password,
                "mobile": args.mobile,
                "auth_code": args.auth_code,
                "vivo_json": args.vivo_json,
                "vivo_file": args.vivo_file,
            }
        )
    except Exception as error:
        print(f"请求失败: {error}")
        sys.exit(1)

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    print(f"渠道: {args.channel}")
    if "result" in output and isinstance(output["result"], dict) and "ui" in output["result"]:
        print(f"UI: {output['result']['ui']}")
        print(f"SK: {output['result']['sk']}")
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
