import json
import os
from datetime import datetime
from threading import Lock
from typing import Any, Dict, Optional


_LOG_LOCK = Lock()
_DEFAULT_LOG_PATH = os.path.join(os.path.dirname(__file__), "login_failures.log")
_LOG_PATH = os.getenv("PVZ2_LOGIN_FAIL_LOG", _DEFAULT_LOG_PATH)


def 记录登录失败(channel: str, step: str, reason: str, details: Optional[Dict[str, Any]] = None) -> None:
    entry: Dict[str, Any] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "channel": channel,
        "step": step,
        "reason": str(reason),
    }
    if details:
        entry["details"] = details

    try:
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with _LOG_LOCK:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        # 日志写入失败不影响主流程
        pass
