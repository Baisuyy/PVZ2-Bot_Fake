"""
配置 — 所有端点、密钥、常量集中管理

通过环境变量覆盖默认值，适合容器化部署。
"""
import os
import re
from typing import Dict
from urllib.parse import urlparse


# ============================================================
# AES 密钥
# ============================================================
DEFAULT_SECRET = "1geh6fvq4r20M02s"
UNIFIED_PASSWORD = "1739902253"  # 调度层 / 任务节点 API 统一 token
RSA_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAw8N6nNnnW8diYTOj/vcB
8L2+3P9pHZ5ZTRkNRcRZ/1ItgTXlx5GX5ju8EgTxGiVWAl9920UlMGPgeBd+m4Jo
Baxc0uAGsNb/pPloydWoT4ntr5/+Hg9Q+EB2DkQi3JgUxyC/AjwB8odz4jOT85vy
fXmzrttg2W7cYoMTfBOjLJGqERZP47hjueiKusArsGaY4r1rWyShmorct0jDNQH6
tLj9fJLvwIgzKK002z9zke2DdXg52WcreailN1cf02cTHsOMwUQzSEL6h/K2J3xV
VgD53y9AF9kr0m0pdzaf6uxC1iDkp9fbVv97ZZlsOBB51EnxOBgLEZTB/ybg3nKU
4wIDAQAB
-----END PUBLIC KEY-----"""

# ============================================================
# 云端端点
# ============================================================
ANDROID_CLOUD_URL = "http://cloudpvz2android.ditwan.cn/index.php"
IOS_CLOUD_URL = "http://cloudpvz2ios.ditwan.cn/index.php"

# iOS 注册特殊 URL (https)
IOS_REGISTER_URL = "https://cloudpvz2ios.ditwan.cn/index.php"

# ============================================================
# 调度层 (Scheduler) 通信
# ============================================================
SCHEDULER_URL = os.getenv(
    "PVZ_SCHEDULER_URL", "http://127.0.0.1:39900"
).rstrip("/")
SCHEDULER_TIMEOUT = float(os.getenv("PVZ_SCHEDULER_TIMEOUT", "3.0"))
HEARTBEAT_INTERVAL = float(os.getenv("PVZ_HEARTBEAT_INTERVAL", "5"))
REGISTER_RETRY_SECONDS = float(os.getenv("PVZ_REGISTER_RETRY_SECONDS", "5"))

# ============================================================
# 账号管理 API (Account Manager)
# ============================================================
ACCOUNT_API_BASE = os.getenv(
    "PVZ_ACCOUNT_API_URL", "http://127.0.0.1:8000"
).rstrip("/")

# ============================================================
# 任务执行节点配置
# ============================================================
NODE_NAME = os.getenv("PVZ_NODE_NAME", os.getenv("COMPUTERNAME", "node-v2"))
NODE_VERSION = "cluster-v2"
NODE_LISTEN_PORT = int(os.getenv("PVZ_NODE_PORT", "39902"))

# 请求控制
REQUEST_TIMEOUT = float(os.getenv("PVZ_REQUEST_TIMEOUT", "5.0"))
MIN_REQUEST_INTERVAL = float(os.getenv("PVZ_MIN_INTERVAL", "0.4"))
COOLDOWN_ON_403 = float(os.getenv("PVZ_COOLDOWN_ON_403", "600"))
MAX_TASK_COUNT = int(os.getenv("PVZ_MAX_TASK_COUNT", "80000"))

# Webhook 回调
WEBHOOK_TIMEOUT = float(os.getenv("PVZ_WEBHOOK_TIMEOUT", "3.0"))

# ============================================================
# 日志保留
# ============================================================
LOG_RETENTION_SECONDS = int(os.getenv("PVZ_LOG_RETENTION", "86400"))  # 24h
LOG_CLEANUP_INTERVAL = int(os.getenv("PVZ_LOG_CLEANUP_INTERVAL", "86400"))
LOG_RESPONSE_LIMIT = int(os.getenv("PVZ_LOG_RESPONSE_LIMIT", "4000"))
LOG_SUCCESS_RESPONSES = os.getenv("PVZ_LOG_SUCCESS_RESPONSES", "1") == "1"

# ============================================================
# 数据库路径
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_SQLITE_DB = os.path.join(BASE_DIR, "..", "accounts.db")
RUNTIME_SQLITE_DB = os.path.join(BASE_DIR, "..", "node_runtime_v2.db")

# ============================================================
# Postgres + Redis (整合登录)
# ============================================================
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "123456")
PG_DATABASE = os.getenv("PG_DATABASE", "account_pool")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# ============================================================
# API 命令码参考
# ============================================================
API_CODES = {
    "V722": "点赞 (like)",
    "V733": "游玩 (play)",
    "V726": "查询关卡信息",
    "V900": "初始化 (sync resources)",
    "V876": "填写邀请码",
    "V303": "加载进度 (激活/验证)",
    "V312": "改名 (安卓激活)",
    "V206": "4399登录 (安卓激活)",
    "V203": "进度设置 (iOS激活)",
    "V201": "创建账号 (iOS注册)",
}

RESPONSE_CODES = {
    0: "成功",
    75051: "成功 (变体)",
    40024: "账号已封禁 (banned)",
    20024: "需要新NS (waiting ns)",
    20001: "封禁 (banned)",
    20022: "限流/暂停 (iOS注册)",
    10306: "限流/暂停 (iOS注册)",
}

# ============================================================
# 平台定义
# ============================================================
PLATFORMS: Dict[str, dict] = {
    "android": {
        "name": "Android",
        "base_url": ANDROID_CLOUD_URL,
        "extra_params": {},
        "invite_pattern": re.compile(r"^[A-Za-z]+$"),       # 邀请码纯字母
    },
    "ios": {
        "name": "iOS",
        "base_url": IOS_CLOUD_URL,
        "extra_params": {"ver_": "newest_version_1", "t": "1"},
        "invite_pattern": re.compile(r"^\d+$"),              # 邀请码纯数字
    },
}

# ============================================================
# 文本常量
# ============================================================
TASK_STATUS_TEXT = {
    "queued": "已排队",
    "processing": "执行中",
    "completed": "已完成",
    "failed": "失败",
    "paused": "已暂停",
    "cancelled": "已取消",
    "interrupted": "已中断",
}
TASK_TYPE_TEXT = {"like": "点赞", "play": "游玩", "both": "点赞+游玩"}
MODE_TEXT = {"active": "活跃", "draining": "排空中", "paused": "已暂停", "unknown": "未知"}
PLATFORM_TEXT = {"android": "安卓", "ios": "iOS"}

# ============================================================
# 邀请服务 (Invite Service)
# ============================================================
INVITE_SERVICE_PORT = int(os.getenv("PVZ_INVITE_PORT", "5000"))
INVITE_TARGET_COUNT = int(os.getenv("PVZ_INVITE_COUNT", "12"))


# ============================================================
# 工具函数
# ============================================================
def valid_callback_url(url: str | None) -> bool:
    """检查回调URL是否合法"""
    if not url:
        return True
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def mask(value: str | None, left: int = 3, right: int = 3) -> str:
    """脱敏显示"""
    if not value:
        return ""
    value = str(value)
    if len(value) <= left + right:
        return "*" * len(value)
    return f"{value[:left]}***{value[-right:]}"


def truncate_text(value: str | None, limit: int | None = None) -> str:
    """截断过长文本"""
    if value is None:
        return ""
    text = str(value)
    limit = limit or LOG_RESPONSE_LIMIT
    if len(text) <= limit:
        return text
    return text[:limit] + "...[已截断]"