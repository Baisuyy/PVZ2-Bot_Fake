import base64
import importlib.util
import json
import os
import re
import time
from threading import Lock

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from flask import Flask, abort, jsonify, render_template, request, session
import pymysql

from login_failure_logger import 记录登录失败
from pvz2_common import 可用渠道

app = Flask(__name__)
app.secret_key = os.getenv("PVZ2_WEB_SECRET", "pvz2-web-secret-change-me")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONTEXT_TTL_SECONDS = 600
LINK_ACCESS_WINDOW_SECONDS = int(os.getenv("PVZ2_LOGIN_LINK_TTL_SECONDS", "180"))
USER_LINK_ATTEMPT_WINDOW_SECONDS = int(os.getenv("PVZ2_USER_LINK_ATTEMPT_WINDOW_SECONDS", "300"))
USER_LINK_ATTEMPT_MAX_TIMES = int(os.getenv("PVZ2_USER_LINK_ATTEMPT_MAX_TIMES", "30"))
INTERNAL_API_TOKEN = os.getenv("PVZ2_INTERNAL_API_TOKEN", "pvz2-internal-token-change-me")

jiuyou_contexts = {}
vivo_contexts = {}
yyb_qr_contexts = {}
yyb_wx_qr_contexts = {}
user_link_attempts = {}
jiuyou_contexts_lock = Lock()
vivo_contexts_lock = Lock()
yyb_qr_contexts_lock = Lock()
yyb_wx_qr_contexts_lock = Lock()
user_link_attempts_lock = Lock()
module_cache = {}
module_cache_lock = Lock()
db_ready = False
db_lock = Lock()

DB_CONFIG = {
    "host": os.getenv("PVZ2_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("PVZ2_DB_PORT", "3306")),
    "user": os.getenv("PVZ2_DB_USER", "root"),
    "password": os.getenv("PVZ2_DB_PASSWORD", "1739902253"),
    "db": os.getenv("PVZ2_DB_NAME", "mcp数据库"),
    "charset": os.getenv("PVZ2_DB_CHARSET", "utf8mb4"),
    "autocommit": True,
}
USER_AES_KEY = os.getenv("PVZ2_USER_AES_KEY", "1739902253ii")


def _load_module(module_alias: str, file_name: str):
    spec = importlib.util.spec_from_file_location(module_alias, file_name)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"加载模块失败: {file_name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _get_module(key: str, alias: str, path: str):
    try:
        current_mtime = os.path.getmtime(path)
    except Exception:
        current_mtime = -1.0
    with module_cache_lock:
        cached = module_cache.get(key)
        if isinstance(cached, dict):
            if cached.get("path") == path and cached.get("mtime") == current_mtime and cached.get("module") is not None:
                return cached["module"]
        elif cached is not None:
            return cached
        module = _load_module(alias, path)
        module_cache[key] = {"module": module, "path": path, "mtime": current_mtime}
        return module


def _cleanup_expired(context_map: dict, lock=None):
    now = time.time()
    if lock is None:
        expired_keys = [k for k, v in context_map.items() if now - v.get("created_at", 0) > CONTEXT_TTL_SECONDS]
        for key in expired_keys:
            context_map.pop(key, None)
        return
    with lock:
        expired_keys = [k for k, v in context_map.items() if now - v.get("created_at", 0) > CONTEXT_TTL_SECONDS]
        for key in expired_keys:
            context_map.pop(key, None)


def _ok(data):
    return jsonify({"ok": True, **data})


def _err(message, code=400):
    return jsonify({"ok": False, "error": message}), code


def _illegal_access():
    return jsonify({"ok": False, "error": "非法访问"}), 403


def _clear_bound_session():
    session.pop("bound_user_id", None)
    session.pop("bound_user_cipher", None)
    session.pop("bound_user_iat", None)
    session.pop("bound_user_exp", None)


def _bind_user_session(user_id: str, encrypted_user: str, issued_at: int, expires_at: int):
    session["bound_user_id"] = user_id
    session["bound_user_cipher"] = encrypted_user
    session["bound_user_iat"] = int(issued_at)
    session["bound_user_exp"] = int(expires_at)


def _is_bound_session_valid(now_ts: float | None = None) -> bool:
    now = now_ts if now_ts is not None else time.time()
    user_id = (session.get("bound_user_id") or "").strip()
    issued_at = int(session.get("bound_user_iat") or 0)
    expires_at = int(session.get("bound_user_exp") or 0)
    if not user_id or issued_at <= 0 or expires_at <= issued_at:
        return False
    if now > expires_at:
        return False
    if now < issued_at - 5:
        return False
    return True


def _log_login_failure(channel: str, step: str, reason: str, details: dict | None = None):
    记录登录失败(channel=channel, step=step, reason=reason, details=details)


def _pkcs7_unpad(raw: bytes) -> bytes:
    return raw[:-ord(raw[-1:])]


def _decode_encrypted_blob(token: str) -> bytes:
    raw = (token or "").strip()
    if not raw:
        raise ValueError("密文为空")
    if len(raw) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", raw):
        return bytes.fromhex(raw)
    normalized = raw.replace("-", "+").replace("_", "/")
    normalized += "=" * ((4 - len(normalized) % 4) % 4)
    return base64.b64decode(normalized)


def _decrypt_user_payload(encrypted_text: str, key: str) -> dict:
    key_bytes = key.ljust(16)[:16].encode("utf-8")
    data = _decode_encrypted_blob(encrypted_text)
    if len(data) < 32:
        raise ValueError("密文长度不足")
    iv, ciphertext = data[:16], data[16:]
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    plain = _pkcs7_unpad(padded).decode("utf-8").strip()
    payload = json.loads(plain)
    if not isinstance(payload, dict):
        raise ValueError("payload 非对象")
    user_id = str(payload.get("u", "")).strip()
    issued_at = int(payload.get("iat", 0))
    expires_at = int(payload.get("exp", 0))
    if not user_id or issued_at <= 0 or expires_at <= issued_at:
        raise ValueError("payload 字段无效")
    return {"user_id": user_id, "issued_at": issued_at, "expires_at": expires_at}


def _allow_user_link_attempt(user_id: str, encrypted_user: str, now_ts: float | None = None) -> bool:
    now = now_ts if now_ts is not None else time.time()
    max_times = int(USER_LINK_ATTEMPT_MAX_TIMES)
    if max_times <= 0:
        return True
    key = f"{user_id}:{encrypted_user[:32]}"
    with user_link_attempts_lock:
        timestamps = user_link_attempts.get(key, [])
        timestamps = [ts for ts in timestamps if now - ts < USER_LINK_ATTEMPT_WINDOW_SECONDS]
        if len(timestamps) >= max_times:
            user_link_attempts[key] = timestamps
            return False
        timestamps.append(now)
        user_link_attempts[key] = timestamps
    return True


def _db_connect():
    return pymysql.connect(**DB_CONFIG)


def _ensure_db_ready():
    global db_ready
    if db_ready:
        return
    with db_lock:
        if db_ready:
            return
        with _db_connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS channel_login_records (
                        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                        channel VARCHAR(32) NOT NULL,
                        user_id VARCHAR(128) NOT NULL DEFAULT '',
                        login_content LONGTEXT NOT NULL,
                        ui VARCHAR(256) NOT NULL,
                        sk VARCHAR(512) NOT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (id),
                        KEY idx_channel_created_at (channel, created_at),
                        KEY idx_user_id_created_at (user_id, created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cursor.execute(
                    """
                    DELETE c1
                    FROM channel_login_records c1
                    JOIN channel_login_records c2
                      ON c1.user_id = c2.user_id
                     AND c1.user_id <> ''
                     AND c1.id < c2.id
                    """
                )
        db_ready = True


def _with_v202_secret(login_content: dict, game_result: dict | None = None) -> dict:
    content = dict(login_content or {})
    if isinstance(game_result, dict):
        ek = game_result.get("ek") or (game_result.get("payload") or {}).get("ek")
        if ek:
            content["ek"] = ek
            content["v202_ek"] = ek
    return content


def _insert_login_record(
    channel: str,
    login_content: dict,
    ui: str,
    sk: str,
    user_id: str = "",
    game_result: dict | None = None,
) -> int:
    _ensure_db_ready()
    login_content = _with_v202_secret(login_content, game_result)
    content_json = json.dumps(login_content, ensure_ascii=False, separators=(",", ":"))
    with _db_connect() as conn:
        with conn.cursor() as cursor:
            if user_id:
                cursor.execute(
                    """
                    INSERT INTO channel_login_records (channel, user_id, login_content, ui, sk)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        id = LAST_INSERT_ID(id),
                        channel = VALUES(channel),
                        login_content = VALUES(login_content),
                        ui = VALUES(ui),
                        sk = VALUES(sk),
                        created_at = CURRENT_TIMESTAMP
                    """,
                    (channel, user_id, content_json, ui, sk),
                )
                return int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO channel_login_records (channel, user_id, login_content, ui, sk)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (channel, user_id, content_json, ui, sk),
            )
            return int(cursor.lastrowid)


def _extract_account_label(channel: str, login_content_raw: str) -> str:
    try:
        payload = json.loads(login_content_raw or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if channel == "4399":
        return str(payload.get("username") or "").strip()
    if channel == "九游":
        return str(payload.get("account_id") or "").strip()
    if channel == "vivo":
        return str(payload.get("sub_open_id") or "").strip()
    if channel == "应用宝":
        login_type = str(payload.get("login_type") or "").strip().lower()
        if login_type == "wx":
            wx_openid = str(payload.get("wx_openid") or "").strip()
            if wx_openid:
                return wx_openid
            return "微信扫码账号"
        callback_raw = str(payload.get("callback_raw") or "")
        matched = re.search(r"openid=([^&'\\\"]+)", callback_raw)
        if matched:
            return matched.group(1).strip()
        return "QQ扫码账号"
    if channel == "miui":
        return "MIUI授权"
    if channel in {"拓维官服", "拓维Tap", "拓维好游快爆"}:
        phone = str(payload.get("phone") or "").strip()
        if phone:
            return phone
        return str(payload.get("tw_user_id") or "").strip()
    return ""


def _query_latest_login_record_by_user(user_id: str) -> dict | None:
    if not user_id:
        return None
    _ensure_db_ready()
    with _db_connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, channel, login_content, created_at, UNIX_TIMESTAMP(created_at) AS created_ts
                FROM channel_login_records
                WHERE user_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()
    if not row:
        return None
    record = {
        "id": int(row[0]),
        "channel": str(row[1]),
        "login_content": row[2] or "",
        "created_at": str(row[3]),
        "created_ts": float(row[4] or 0),
    }
    record["account_label"] = _extract_account_label(record["channel"], record["login_content"])
    return record


def _stored_result(channel: str, record_id: int, user_id: str = ""):
    result = {"stored": True, "record_id": record_id, "channel": channel, "message": "登录成功"}
    if user_id:
        result["user_id"] = user_id
    return _ok({"result": result})


def _get_bound_user_id() -> str:
    return (session.get("bound_user_id") or "").strip()


def _check_internal_token() -> bool:
    token = (request.args.get("token") or "").strip()
    return bool(token) and token == INTERNAL_API_TOKEN


@app.before_request
def _guard_api_with_bound_token():
    path = request.path or ""
    if not path.startswith("/api/"):
        return None
    if path.startswith("/api/internal/"):
        return None
    if _is_bound_session_valid():
        return None
    _clear_bound_session()
    return _illegal_access()


@app.route("/", methods=["GET"])
def index():
    _clear_bound_session()
    return _illegal_access()


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    return ("", 204)


@app.route("/<encrypted_user>", methods=["GET"])
def index_with_encrypted_user(encrypted_user: str):
    if encrypted_user in {"api", "static", "favicon.ico", "robots.txt", "apple-touch-icon.png", "apple-touch-icon-precomposed.png"}:
        abort(404)
    _clear_bound_session()
    try:
        payload = _decrypt_user_payload((encrypted_user or "").rstrip("/"), USER_AES_KEY)
    except Exception:
        return _illegal_access()
    user_id = payload["user_id"]
    now_ts = time.time()
    max_expires_at = payload["issued_at"] + LINK_ACCESS_WINDOW_SECONDS
    effective_expires_at = min(payload["expires_at"], max_expires_at)
    if now_ts > effective_expires_at:
        return _illegal_access()
    if now_ts < payload["issued_at"] - 5:
        return _illegal_access()
    if not _allow_user_link_attempt(user_id, encrypted_user, now_ts):
        return _illegal_access()
    _bind_user_session(
        user_id=user_id,
        encrypted_user=encrypted_user,
        issued_at=payload["issued_at"],
        expires_at=effective_expires_at,
    )
    return render_template("index.html", channels=可用渠道(), user_id=user_id, token_valid=True)


@app.route("/api/me/login-record", methods=["GET"])
def api_me_login_record():
    user_id = _get_bound_user_id()
    if not user_id:
        return _err("缺少用户ID，请使用带密文token的链接打开页面")
    record = _query_latest_login_record_by_user(user_id)
    if not record:
        return _ok({"has_login": False})
    return _ok(
        {
            "has_login": True,
            "record": {
                "channel": record["channel"],
                "account_label": record["account_label"],
                "created_at": record["created_at"],
            },
        }
    )


@app.route("/api/internal/login-event", methods=["GET"])
def api_internal_login_event():
    if not _check_internal_token():
        return _illegal_access()
    user_id = (request.args.get("user_id") or "").strip()
    if not user_id:
        return _err("缺少user_id")
    try:
        since = float((request.args.get("since") or "0").strip() or 0)
    except Exception:
        since = 0.0
    record = _query_latest_login_record_by_user(user_id)
    if not record:
        return _ok({"logged_in": False})
    if record["created_ts"] <= (since - 1):
        return _ok({"logged_in": False})
    return _ok(
        {
            "logged_in": True,
            "event": {
                "channel": record["channel"],
                "created_at": record["created_at"],
            },
        }
    )


@app.route("/api/4399/login", methods=["POST"])
def api_4399_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    user_id = _get_bound_user_id()
    if not username or not password:
        _log_login_failure("4399", "参数校验", "缺少账号或密码")
        return _err("请填写 4399 账号和密码")
    if not user_id:
        _log_login_failure("4399", "参数校验", "缺少用户ID")
        return _err("缺少用户ID，请使用带密文token的链接打开页面")
    try:
        login4399_module = _get_module("4399", "channel_4399", os.path.join(BASE_DIR, "4399登录.py"))
        login_result = login4399_module.login_4399(username, password)
        if not login_result or "result" not in login_result:
            _log_login_failure("4399", "渠道鉴权", "4399登录失败", {"username": username, "user_id": user_id})
            return _err("4399 登录失败")
        info = login_result["result"]
        result = login4399_module.执行注册(info.get("state", ""), info.get("uid", ""))
        if "error" in result:
            _log_login_failure("4399", "渠道注册", result["error"], {"uid": info.get("uid", ""), "user_id": user_id})
            return _err(result["error"])
        record_id = _insert_login_record(
            channel="4399",
            user_id=user_id,
            login_content={
                "user_id": user_id,
                "username": username,
                "password": password,
                "uid": info.get("uid", ""),
                "state": info.get("state", ""),
            },
            ui=result.get("ui", ""),
            sk=result.get("sk", ""),
            game_result=result,
        )
        return _stored_result("4399", record_id, user_id=user_id)
    except Exception as error:
        _log_login_failure("4399", "接口异常", str(error), {"username": username, "user_id": user_id})
        return _err(str(error))


@app.route("/api/miui/login", methods=["POST"])
def api_miui_login():
    data = request.get_json(silent=True) or {}
    auth_code = (data.get("auth_code") or "").strip()
    user_id = _get_bound_user_id()
    if not auth_code:
        _log_login_failure("miui", "参数校验", "缺少授权码")
        return _err("请填写 MIUI 授权码")
    if not user_id:
        _log_login_failure("miui", "参数校验", "缺少用户ID")
        return _err("缺少用户ID，请使用带密文token的链接打开页面")
    try:
        miui_module = _get_module("miui", "channel_miui", os.path.join(BASE_DIR, "miui登录指令版.py"))
        result = miui_module.处理授权流程(auth_code)
        record_id = _insert_login_record(
            channel="miui",
            user_id=user_id,
            login_content={"auth_code": auth_code, "user_id": user_id},
            ui=result.get("ui", ""),
            sk=result.get("sk", ""),
            game_result=result,
        )
        return _stored_result("miui", record_id, user_id=user_id)
    except Exception as error:
        _log_login_failure("miui", "接口异常", str(error), {"auth_code": auth_code, "user_id": user_id})
        return _err(str(error))


@app.route("/api/jiuyou/accounts", methods=["POST"])
def api_jiuyou_accounts():
    data = request.get_json(silent=True) or {}
    mobile = (data.get("mobile") or "").strip()
    password = (data.get("password") or "").strip()
    user_id = _get_bound_user_id()
    if not mobile or not password:
        _log_login_failure("九游", "参数校验", "缺少手机号或密码")
        return _err("请填写九游手机号和密码")
    if not user_id:
        _log_login_failure("九游", "参数校验", "缺少用户ID")
        return _err("缺少用户ID，请使用带密文token的链接打开页面")
    try:
        jiuyou_module = _get_module("jiuyou", "channel_jiuyou", os.path.join(BASE_DIR, "九游.py"))
        results = jiuyou_module.login_九游(mobile, password)
        if not results:
            _log_login_failure("九游", "渠道鉴权", "九游登录失败", {"mobile": mobile, "user_id": user_id})
            return _err("九游登录失败")
        context_id = str(uuid.uuid4())
        with jiuyou_contexts_lock:
            _cleanup_expired(jiuyou_contexts, jiuyou_contexts_lock)
            jiuyou_contexts[context_id] = {
                "accounts": results,
                "mobile": mobile,
                "password": password,
                "user_id": user_id,
                "created_at": time.time(),
            }
        return _ok({
            "context_id": context_id,
            "accounts": [item.get("account_id", "") for item in results],
        })
    except Exception as error:
        _log_login_failure("九游", "接口异常", str(error), {"mobile": mobile, "user_id": user_id})
        return _err(str(error))


@app.route("/api/jiuyou/login", methods=["POST"])
def api_jiuyou_login():
    data = request.get_json(silent=True) or {}
    context_id = (data.get("context_id") or "").strip()
    account_id = (data.get("account_id") or "").strip()
    user_id = _get_bound_user_id()
    if not context_id or not account_id:
        _log_login_failure("九游", "参数校验", "缺少context_id或account_id")
        return _err("缺少context_id或account_id")
    if not user_id:
        _log_login_failure("九游", "参数校验", "缺少用户ID")
        return _err("缺少用户ID，请使用带密文token的链接打开页面")
    try:
        with jiuyou_contexts_lock:
            context = jiuyou_contexts.get(context_id)
            if not context:
                return _err("context已过期，请重新获取账号列表")
        target_account = None
        for item in context["accounts"]:
            if item.get("account_id") == account_id:
                target_account = item
                break
        if not target_account:
            return _err("账号不存在")
        record_id = _insert_login_record(
            channel="九游",
            user_id=user_id,
            login_content={
                "mobile": context["mobile"],
                "password": context["password"],
                "account_id": account_id,
                "user_id": user_id,
            },
            ui=target_account.get("ui", ""),
            sk=target_account.get("sk", ""),
            game_result=target_account,
        )
        return _stored_result("九游", record_id, user_id=user_id)
    except Exception as error:
        _log_login_failure("九游", "接口异常", str(error), {"account_id": account_id, "user_id": user_id})
        return _err(str(error))


@app.route("/api/vivo/accounts", methods=["POST"])
def api_vivo_accounts():
    data = request.get_json(silent=True) or {}
    vivo_json = (data.get("vivo_json") or "").strip()
    user_id = _get_bound_user_id()
    if not vivo_json:
        _log_login_failure("vivo", "参数校验", "缺少vivo_json")
        return _err("请填写vivo JSON数据")
    if not user_id:
        _log_login_failure("vivo", "参数校验", "缺少用户ID")
        return _err("缺少用户ID，请使用带密文token的链接打开页面")
    try:
        vivo_module = _get_module("vivo", "channel_vivo", os.path.join(BASE_DIR, "vivo.py"))
        parsed = vivo_module.extract_vivo_info(vivo_json)
        if "error" in parsed:
            _log_login_failure("vivo", "解析账号信息", parsed["error"], {"user_id": user_id})
            return _err(parsed["error"])
        context_id = str(uuid.uuid4())
        with vivo_contexts_lock:
            _cleanup_expired(vivo_contexts, vivo_contexts_lock)
            vivo_contexts[context_id] = {
                "sub_open_ids": parsed["sub_open_ids"],
                "open_token": parsed["open_token"],
                "user_id": user_id,
                "created_at": time.time(),
            }
        return _ok({
            "context_id": context_id,
            "accounts": parsed["sub_open_ids"],
        })
    except Exception as error:
        _log_login_failure("vivo", "接口异常", str(error), {"user_id": user_id})
        return _err(str(error))


@app.route("/api/vivo/login", methods=["POST"])
def api_vivo_login():
    data = request.get_json(silent=True) or {}
    context_id = (data.get("context_id") or "").strip()
    sub_open_id = (data.get("sub_open_id") or "").strip()
    user_id = _get_bound_user_id()
    if not context_id or not sub_open_id:
        _log_login_failure("vivo", "参数校验", "缺少context_id或sub_open_id")
        return _err("缺少context_id或sub_open_id")
    if not user_id:
        _log_login_failure("vivo", "参数校验", "缺少用户ID")
        return _err("缺少用户ID，请使用带密文token的链接打开页面")
    try:
        with vivo_contexts_lock:
            context = vivo_contexts.get(context_id)
            if not context:
                return _err("context已过期，请重新获取账号列表")
        if sub_open_id not in context["sub_open_ids"]:
            return _err("账号不存在")
        vivo_module = _get_module("vivo", "channel_vivo", os.path.join(BASE_DIR, "vivo.py"))
        game_result = vivo_module.发送游戏请求(sub_open_id, context["open_token"])
        if game_result.get("status") != "success":
            return _err(game_result.get("message", "游戏请求失败"))
        record_id = _insert_login_record(
            channel="vivo",
            user_id=user_id,
            login_content={
                "sub_open_id": sub_open_id,
                "user_id": user_id,
            },
            ui=game_result.get("ui", ""),
            sk=game_result.get("sk", ""),
            game_result=game_result,
        )
        return _stored_result("vivo", record_id, user_id=user_id)
    except Exception as error:
        _log_login_failure("vivo", "接口异常", str(error), {"sub_open_id": sub_open_id, "user_id": user_id})
        return _err(str(error))


@app.route("/api/yyb/qr/create", methods=["POST"])
def api_yyb_qr_create():
    user_id = _get_bound_user_id()
    if not user_id:
        _log_login_failure("应用宝", "参数校验", "缺少用户ID")
        return _err("缺少用户ID，请使用带密文token的链接打开页面")
    try:
        yyb_module = _get_module("yyb", "channel_yyb", os.path.join(BASE_DIR, "应用宝渠道登录.py"))
        动态凭证 = yyb_module.获取QQ动态凭证()
        qr_id = str(uuid.uuid4())
        with yyb_qr_contexts_lock:
            _cleanup_expired(yyb_qr_contexts, yyb_qr_contexts_lock)
            yyb_qr_contexts[qr_id] = {
                "cookies": 动态凭证,
                "user_id": user_id,
                "created_at": time.time(),
            }
        import base64
        qr_base64 = base64.b64encode(动态凭证["qr_bytes"]).decode("ascii")
        return _ok({
            "qr_id": qr_id,
            "qr_image_base64": qr_base64,
        })
    except Exception as error:
        _log_login_failure("应用宝", "QQ二维码创建", str(error), {"user_id": user_id})
        return _err(str(error))


@app.route("/api/yyb/qr/poll", methods=["GET"])
def api_yyb_qr_poll():
    qr_id = (request.args.get("qr_id") or "").strip()
    user_id = _get_bound_user_id()
    if not qr_id:
        return _err("缺少qr_id")
    if not user_id:
        return _err("缺少用户ID")
    try:
        with yyb_qr_contexts_lock:
            context = yyb_qr_contexts.get(qr_id)
            if not context:
                return _ok({"status": "expired"})
        yyb_module = _get_module("yyb", "channel_yyb", os.path.join(BASE_DIR, "应用宝渠道登录.py"))
        登录结果 = yyb_module.轮询QQ登录状态(context["cookies"])
        account_id, access_token = yyb_module.提取参数(登录结果)
        result = yyb_module.用QQ登录(登录结果)
        record_id = _insert_login_record(
            channel="应用宝",
            user_id=user_id,
            login_content={
                "account_id": account_id,
                "access_token": access_token,
                "login_type": "qq",
                "user_id": user_id,
            },
            ui=result.get("ui", ""),
            sk=result.get("sk", ""),
            game_result=result,
        )
        with yyb_qr_contexts_lock:
            yyb_qr_contexts.pop(qr_id, None)
        return _stored_result("应用宝", record_id, user_id=user_id)
    except Exception as error:
        _log_login_failure("应用宝", "QQ二维码轮询", str(error), {"qr_id": qr_id, "user_id": user_id})
        return _err(str(error))


@app.route("/api/yyb/wx/qr/create", methods=["POST"])
def api_yyb_wx_qr_create():
    user_id = _get_bound_user_id()
    if not user_id:
        _log_login_failure("应用宝", "参数校验", "缺少用户ID")
        return _err("缺少用户ID，请使用带密文token的链接打开页面")
    try:
        yyb_module = _get_module("yyb", "channel_yyb", os.path.join(BASE_DIR, "应用宝渠道登录.py"))
        qr_data = yyb_module.获取微信二维码()
        qr_id = str(uuid.uuid4())
        with yyb_wx_qr_contexts_lock:
            _cleanup_expired(yyb_wx_qr_contexts, yyb_wx_qr_contexts_lock)
            yyb_wx_qr_contexts[qr_id] = {
                "uuid": qr_data["uuid"],
                "user_id": user_id,
                "created_at": time.time(),
            }
        return _ok({
            "qr_id": qr_id,
            "qr_image_base64": qr_data["qr_base64"],
        })
    except Exception as error:
        _log_login_failure("应用宝", "微信二维码创建", str(error), {"user_id": user_id})
        return _err(str(error))


@app.route("/api/yyb/wx/qr/poll", methods=["GET"])
def api_yyb_wx_qr_poll():
    qr_id = (request.args.get("qr_id") or "").strip()
    user_id = _get_bound_user_id()
    if not qr_id:
        return _err("缺少qr_id")
    if not user_id:
        return _err("缺少用户ID")
    try:
        with yyb_wx_qr_contexts_lock:
            context = yyb_wx_qr_contexts.get(qr_id)
            if not context:
                return _ok({"status": "expired"})
        yyb_module = _get_module("yyb", "channel_yyb", os.path.join(BASE_DIR, "应用宝渠道登录.py"))
        status_result = yyb_module.检查微信登录状态(context["uuid"])
        if status_result["status"] != "success":
            return _ok(status_result)
        result = yyb_module.用微信登录(status_result["openid"], status_result["atk"])
        record_id = _insert_login_record(
            channel="应用宝",
            user_id=user_id,
            login_content={
                "wx_openid": status_result["openid"],
                "wx_atk": status_result["atk"],
                "login_type": "wx",
                "user_id": user_id,
            },
            ui=result.get("ui", ""),
            sk=result.get("sk", ""),
            game_result=result,
        )
        with yyb_wx_qr_contexts_lock:
            yyb_wx_qr_contexts.pop(qr_id, None)
        return _stored_result("应用宝", record_id, user_id=user_id)
    except Exception as error:
        _log_login_failure("应用宝", "微信二维码轮询", str(error), {"qr_id": qr_id, "user_id": user_id})
        return _err(str(error))


@app.route("/api/tuowei/login", methods=["POST"])
def api_tuowei_login():
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    password = (data.get("password") or "").strip()
    sub_channel = (data.get("sub_channel") or "").strip()
    user_id = _get_bound_user_id()
    if not phone or not password or not sub_channel:
        _log_login_failure("拓维", "参数校验", "缺少手机号、密码或子渠道")
        return _err("请填写拓维手机号、密码和子渠道")
    if not user_id:
        _log_login_failure("拓维", "参数校验", "缺少用户ID")
        return _err("缺少用户ID，请使用带密文token的链接打开页面")
    
    channel_map = {
        "official": "拓维官服",
        "tap": "拓维Tap", 
        "goodswimming": "拓维好游快爆"
    }
    channel = channel_map.get(sub_channel)
    if not channel:
        return _err("无效的子渠道")
    
    try:
        tuowei_module = _get_module("tuowei", "channel_tuowei", os.path.join(BASE_DIR, "拓维账号登录.py"))
        login_result = tuowei_module.登录拓维账号(phone, password)
        result = 请求UI_SK(channel_key=channel, account_id=login_result["tw_user_id"], token=login_result["token"])
        record_id = _insert_login_record(
            channel=channel,
            user_id=user_id,
            login_content={
                "phone": phone,
                "password": password,
                "tw_user_id": login_result["tw_user_id"],
                "user_id": user_id,
            },
            ui=result.get("ui", ""),
            sk=result.get("sk", ""),
            game_result=result,
        )
        return _stored_result(channel, record_id, user_id=user_id)
    except Exception as error:
        _log_login_failure("拓维", "接口异常", str(error), {"phone": phone, "user_id": user_id})
        return _err(str(error))