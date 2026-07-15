"""
任务调度引擎 — 点赞/游玩任务执行核心

架构:
  - TaskQueue: 线程安全的任务队列（队列模式供调度层推入）
  - TaskRunner: 单任务执行器（取号→加密→云端请求→回调）
  - TaskEngine: 引擎控制器（启停、暂停、排空、流水线）

优化特性:
  - 线程池并发执行，大幅提升刷赞速度
  - 智能重试 + 指数退避
  - 随机UA + 请求抖动，反检测伪装
"""
import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Dict, Optional

import requests

from .config import (
    ACCOUNT_API_BASE, UNIFIED_PASSWORD,
    MAX_TASK_COUNT, COOLDOWN_ON_403,
    WEBHOOK_TIMEOUT, LOG_SUCCESS_RESPONSES,
    PLATFORMS, CONCURRENT_WORKERS_PER_TASK,
)
from .cloud_client import call, payload_v722_v733
from .task_models import Task, RequestResult

logger = logging.getLogger("task_engine")


# ============================================================
# 任务队列
# ============================================================

class TaskQueue:
    """线程安全 FIFO 任务队列"""

    def __init__(self, max_size: int = MAX_TASK_COUNT):
        self._queue = queue.Queue(maxsize=max_size)
        self._active_ids: set = set()
        self._lock = threading.Lock()
        self._running = True

    def put(self, task: Task) -> bool:
        """推入队列，已存在或满则拒绝"""
        with self._lock:
            if task.id in self._active_ids:
                return False
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            return False
        with self._lock:
            self._active_ids.add(task.id)
        return True

    def get(self, timeout: float = 1.0) -> Optional[Task]:
        """非阻塞获取下一个任务"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def remove_active(self, task_id: str):
        with self._lock:
            self._active_ids.discard(task_id)

    def clear(self):
        """清空队列（排空模式时用）"""
        with self._lock:
            self._active_ids.clear()
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active_ids)


# ============================================================
# 账号获取
# ============================================================

def fetch_task_account(platform: str) -> tuple:
    """
    从 Account Manager 获取一个已激活账号作为任务机器人
    Returns: (ui, sk, pi, secret) 或全 None
    """
    try:
        resp = requests.get(
            f"{ACCOUNT_API_BASE}/api/accounts/distribute",
            params={"platform": platform, "purpose": "invite"},  # 复用 invite purpose 取 activated
            timeout=10,
        )
        if resp.status_code == 404:
            return None, None, None, None
        resp.raise_for_status()
        data = resp.json()
        return (
            data["ui"], data["sk"],
            data.get("pi") or data["ui"],
            data.get("secret") or "1geh6fvq4r20M02s",
        )
    except Exception as e:
        logger.error("获取任务账号失败: %s", e)
        return None, None, None, None


# ============================================================
# 单任务执行器（线程池并发版）
# ============================================================

class TaskRunner:
    """执行单个任务 — 线程池并发执行，大幅提升刷赞速度"""

    def __init__(self, task: Task):
        self.task = task
        self._lock = threading.Lock()

        self.executed = 0
        self.success_like = 0
        self.success_play = 0
        self.failed_like = 0
        self.failed_play = 0
        self.errors: Dict[str, int] = {}
        self.account_count = 0

        self._cooldown_until = 0.0
        self._check_cancel = None

    def _record_error_locked(self, reason: str, status_code: int | None):
        key = reason
        if status_code == 403:
            key = "http_403"
        elif reason.startswith("business_"):
            key = reason
        elif reason == "timeout":
            key = "timeout"
        else:
            key = "other"
        with self._lock:
            self.errors[key] = self.errors.get(key, 0) + 1
            if key == "http_403":
                self._cooldown_until = time.time() + COOLDOWN_ON_403

    def _update_stats_locked(self, s_like: int, s_play: int, got_account: bool):
        with self._lock:
            self.executed += 1
            self.success_like += s_like
            self.success_play += s_play
            if s_like == 0:
                self.failed_like += 1
            if s_play == 0:
                self.failed_play += 1
            if got_account:
                self.account_count += 1
            executed = self.executed
            success_like = self.success_like
            failed_like = self.failed_like
            success_play = self.success_play
            failed_play = self.failed_play
        if executed % 10 == 0:
            logger.info(
                "[Task] %s 进度: %d/%d, like=%d/%d, play=%d/%d",
                self.task.id, executed, self.task.total_count,
                success_like, failed_like,
                success_play, failed_play,
            )

    def _execute_one_worker(self) -> bool:
        """
        单个Worker线程：取一个账号并执行点赞/游玩
        Returns: True 表示应该继续，False 表示应该停止
        """
        if self._check_cancel and self._check_cancel():
            return False

        with self._lock:
            if self.executed >= self.task.total_count:
                return False

        cooldown_remaining = self._cooldown_until - time.time()
        if cooldown_remaining > 0:
            sleep_for = min(cooldown_remaining, 2.0)
            time.sleep(sleep_for)
            return True

        ui, sk, pi, secret = fetch_task_account(self.task.platform)
        if ui is None:
            time.sleep(2)
            return True

        session = requests.Session()
        session.trust_env = False

        s_like, s_play = 0, 0

        if self.task.task_type in ("like", "both"):
            payload = payload_v722_v733(
                {"ui": ui, "sk": sk, "pi": pi},
                self.task.level_id, self.task.platform,
            )
            ok, reason, _, status = call(
                session, "V722", payload, secret, ui, self.task.platform,
            )
            if ok:
                s_like = 1
            else:
                self._record_error_locked(reason, status)

        if self.task.task_type in ("play", "both"):
            payload = payload_v722_v733(
                {"ui": ui, "sk": sk, "pi": pi},
                self.task.level_id, self.task.platform,
            )
            ok, reason, _, status = call(
                session, "V733", payload, secret, ui, self.task.platform,
            )
            if ok:
                s_play = 1
            else:
                self._record_error_locked(reason, status)

        self._update_stats_locked(s_like, s_play, got_account=True)

        with self._lock:
            if self.executed >= self.task.total_count:
                return False
        return True

    def run(self) -> Task:
        """执行完整任务，返回更新后的 Task（线程池并发版）"""
        self.task.status = "processing"
        self.task.start_time = time.time()
        workers = max(1, min(CONCURRENT_WORKERS_PER_TASK, self.task.total_count))
        logger.info(
            "[Task] %s 开始执行, total=%d, workers=%d",
            self.task.id, self.task.total_count, workers,
        )

        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"runner-{self.task.id[:6]}") as pool:
                futures: set = set()

                while True:
                    if self._check_cancel and self._check_cancel():
                        self.task.status = "cancelled"
                        break

                    with self._lock:
                        if self.executed >= self.task.total_count:
                            break

                    while len(futures) < workers:
                        futures.add(pool.submit(self._execute_one_worker))

                    done_futures = {f for f in futures if f.done()}
                    if done_futures:
                        for f in done_futures:
                            futures.remove(f)
                            try:
                                f.result()
                            except Exception:
                                pass
                    else:
                        time.sleep(0.1)

                for f in futures:
                    f.cancel()

            if self.task.status != "cancelled":
                self.task.status = "completed"

        except Exception as e:
            logger.exception("[Task] %s 异常", self.task.id)
            self.task.status = "failed"
            self.task.message = str(e)

        finally:
            self.task.finish_time = time.time()
            self.task.executed_count = self.executed
            self.task.success_like = self.success_like
            self.task.success_play = self.success_play
            self.task.failed_like = self.failed_like
            self.task.failed_play = self.failed_play
            self.task.error_summary = self.errors
            self.task.account_count = self.account_count
            self.task.updated_at = time.time()

            if self.task.callback_url:
                self._send_callback()

            logger.info(
                "[Task] %s 完成: status=%s, like=%d/%d, play=%d/%d, errors=%s",
                self.task.id, self.task.status,
                self.success_like, self.failed_like,
                self.success_play, self.failed_play,
                self.errors,
            )

        return self.task

    def _send_callback(self):
        """发送任务结果到 callback_url"""
        try:
            payload = self.task.to_record()
            payload["error_summary_json"] = json.dumps(self.task.error_summary)
            requests.post(
                self.task.callback_url,
                json=payload,
                timeout=WEBHOOK_TIMEOUT,
            )
        except Exception as e:
            logger.warning("[Task] %s 回调异常: %s", self.task.id, e)


# ============================================================
# 引擎控制器
# ============================================================

class TaskEngine:
    """任务引擎 — 管理多个并发 task runner 的启停"""

    def __init__(self):
        self.queue = TaskQueue()
        self._workers: Dict[str, threading.Thread] = {}
        self._workers_lock = threading.Lock()
        self.mode = "active"  # active | draining | paused
        self._max_workers = 10

    @property
    def worker_count(self) -> int:
        with self._workers_lock:
            return len(self._workers)

    @property
    def active_tasks(self) -> list:
        result = []
        with self._workers_lock:
            for task_id, _ in self._workers.items():
                result.append(task_id)
        return result

    def submit(self, task: Task) -> bool:
        """提交任务到队列"""
        if self.mode == "paused":
            return False
        return self.queue.put(task)

    def set_mode(self, mode: str):
        self.mode = mode
        if mode == "paused":
            pass  # 不停止正在运行的 worker
        elif mode == "draining":
            self.queue.clear()
        elif mode == "active":
            pass

    def _run_worker(self, task: Task):
        """Worker 线程 — 执行一个任务"""
        runner = TaskRunner(task)
        runner._check_cancel = lambda: self.mode == "paused"
        result = runner.run()

        # 存储结果到 runtime DB
        from .task_models import save_task_result
        save_task_result(result)

        with self._workers_lock:
            self._workers.pop(task.id, None)
        self.queue.remove_active(task.id)

    def dispatch(self):
        """从队列取任务启动 worker（在 heartbeat 循环中调用）"""
        while self.worker_count < self._max_workers:
            task = self.queue.get(timeout=0)
            if task is None:
                break
            if self.mode == "paused":
                self.queue.put(task)  # 放回去
                break
            thread = threading.Thread(
                target=self._run_worker,
                args=(task,),
                name=f"task-{task.id[:8]}",
                daemon=True,
            )
            with self._workers_lock:
                self._workers[task.id] = thread
            thread.start()
            logger.info("[Engine] 启动 worker: %s (%s/%s)",
                       task.id, task.task_type, task.platform)