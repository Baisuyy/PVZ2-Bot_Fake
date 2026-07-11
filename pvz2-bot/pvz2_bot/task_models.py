"""
任务数据模型 + Master 运行时状态
"""
import json
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Dict, Optional

from .config import (
    TASK_STATUS_TEXT, TASK_TYPE_TEXT, PLATFORM_TEXT, MODE_TEXT,
    RUNTIME_SQLITE_DB, UNIFIED_PASSWORD,
)



@dataclass
class Task:
    id: str
    platform: str
    task_type: str       # like | play | both
    total_count: int
    level_id: str
    payload_json: str    # 原始请求JSON

    status: str = "queued"
    executed_count: int = 0
    success_like: int = 0
    success_play: int = 0
    failed_like: int = 0
    failed_play: int = 0
    account_count: int = 0
    message: str = ""
    callback_url: str = ""
    created_at: float = field(default_factory=time.time)
    start_time: float = 0.0
    finish_time: float = 0.0
    updated_at: float = field(default_factory=time.time)
    error_summary: Dict[str, int] = field(default_factory=dict)
    runtime_mode_snapshot: str = ""
    is_cancelled: bool = False

    def to_record(self) -> dict:
        data = asdict(self)
        data["cancel_requested"] = 1 if self.is_cancelled else 0
        data["status_text"] = TASK_STATUS_TEXT.get(self.status, self.status)
        data["task_type_text"] = TASK_TYPE_TEXT.get(self.task_type, self.task_type)
        data["platform_text"] = PLATFORM_TEXT.get(self.platform, self.platform)
        data["runtime_mode_snapshot_text"] = MODE_TEXT.get(
            self.runtime_mode_snapshot, self.runtime_mode_snapshot
        )
        return data

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        return cls(
            id=row["task_id"],
            platform=row["platform"],
            task_type=row["task_type"],
            total_count=int(row["total_count"]),
            level_id=row["level_id"],
            payload_json=row["payload_json"],
            status=row["status"],
            executed_count=int(row["executed_count"] or 0),
            success_like=int(row["success_like"] or 0),
            success_play=int(row["success_play"] or 0),
            failed_like=int(row["failed_like"] or 0),
            failed_play=int(row["failed_play"] or 0),
            account_count=int(row["account_count"] or 0),
            message=row["message"] or "",
            callback_url=row["callback_url"] or "",
            created_at=float(row["created_at"]),
            start_time=float(row["start_time"] or 0),
            finish_time=float(row["finish_time"] or 0),
            updated_at=float(row["updated_at"]),
            error_summary=json.loads(row["error_summary_json"] or "{}"),
            runtime_mode_snapshot=row["runtime_mode_snapshot"] or "",
            is_cancelled=bool(row["cancel_requested"]),
        )


def save_task_result(task: Task):
    """将执行完毕的任务结果存入 runtime SQLite"""
    conn = sqlite3.connect(RUNTIME_SQLITE_DB, timeout=10)
    try:
        conn.execute(
            """INSERT INTO node_tasks
               (task_id, status, platform, task_type, level_id, total_count,
                executed_count, success_like, success_play, failed_like, failed_play,
                message, callback_url, payload_json,
                created_at, start_time, finish_time, updated_at,
                account_count, error_summary_json, runtime_mode_snapshot, cancel_requested)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task.id, task.status, task.platform, task.task_type,
                task.level_id, task.total_count,
                task.executed_count, task.success_like, task.success_play,
                task.failed_like, task.failed_play,
                task.message, task.callback_url, task.payload_json,
                task.created_at, task.start_time, task.finish_time, task.updated_at,
                task.account_count, json.dumps(task.error_summary),
                task.runtime_mode_snapshot,
                1 if task.is_cancelled else 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@dataclass
class RequestResult:
    success: bool
    reason: str
    status_code: Optional[int] = None


class RuntimeStore:
    """Scheduler 注册/心跳用的运行时状态"""

    def __init__(self, node_name: str = "", version: str = ""):
        self.runtime_mode = "active"
        self.registered = False
        self.registration_error = ""
        self.scheduler_last_ok_at = 0.0
        self.scheduler_last_error = ""
        self.scheduler_mode = "unknown"
        self.node_name = node_name
        self.version = version

    def set_mode(self, new_mode: str):
        if new_mode in ("active", "draining", "paused"):
            self.runtime_mode = new_mode

    def register_payload(self) -> dict:
        return {
            "node_name": self.node_name,
            "version": self.version,
            "runtime_mode": self.runtime_mode,
        }

    def heartbeat_payload(self) -> dict:
        return {
            "node_name": self.node_name,
            "runtime_mode": self.runtime_mode,
        }