from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from backend.core.database import AsyncJsonDB

log = logging.getLogger("qwen2api.video_tasks")

VIDEO_TASK_PENDING_STATUSES = {"queued", "running"}


def hash_task_owner(token: str) -> str:
    """返回任务归属哈希，避免在任务库中保存客户端 API Key 明文。"""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def sanitize_error_message(value: Any, *, max_chars: int = 500) -> str:
    """脱敏错误文本中的签名 URL 查询串，避免日志和任务响应泄露敏感参数。"""
    text = str(value or "unknown")

    def _strip_query(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        try:
            parts = urlsplit(raw_url)
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except Exception:
            return raw_url.split("?", 1)[0]

    text = re.sub(r"https?://[^\s\"'<>]+", _strip_query, text)
    return text[:max_chars]


class VideoTaskStore:
    """基于 AsyncJsonDB 的轻量视频任务存储。"""

    def __init__(self, db: AsyncJsonDB, *, ttl_seconds: int = 86400):
        self.db = db
        self.ttl_seconds = max(1, int(ttl_seconds or 86400))
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """加载任务数据，并将异常形态归一为列表。"""
        data = await self.db.load()
        if not isinstance(data, list):
            await self.db.save([])

    async def create(self, owner_hash: str, request_data: dict[str, Any]) -> dict[str, Any]:
        """创建 queued 状态的视频任务。"""
        now = int(time.time())
        task = {
            "id": f"video_task_{uuid.uuid4().hex}",
            "object": "video.generation.task",
            "owner_hash": owner_hash,
            "status": "queued",
            "request": request_data,
            "result": None,
            "error": None,
            "mode": request_data.get("generation_chat_type") or "t2v",
            "model": request_data.get("model") or "",
            "created": now,
            "updated": now,
            "started": None,
            "finished": None,
            "expires": now + self.ttl_seconds,
        }
        async with self._lock:
            tasks = await self._tasks_unlocked()
            tasks.append(task)
            await self.db.save(tasks)
        return task

    async def get(self, task_id: str) -> dict[str, Any] | None:
        """按任务 ID 查询任务，不做权限判断。"""
        async with self._lock:
            return self._copy_task(await self._find_unlocked(task_id))

    async def get_visible(self, task_id: str, owner_hash: str, *, is_admin: bool = False) -> dict[str, Any] | None:
        """按任务 ID 和调用方归属查询任务，无权访问时返回 None。"""
        task = await self.get(task_id)
        if not task:
            return None
        if not is_admin and task.get("owner_hash") != owner_hash:
            return None
        return task

    async def mark_running(self, task_id: str) -> dict[str, Any] | None:
        """将 queued 任务切换为 running，返回可执行任务快照。"""
        now = int(time.time())
        async with self._lock:
            tasks = await self._tasks_unlocked()
            task = self._find_in_list(tasks, task_id)
            if not task or task.get("status") != "queued":
                return None
            if int(task.get("expires") or 0) <= now:
                task.update({"status": "expired", "updated": now, "finished": now})
                await self.db.save(tasks)
                return None
            task.update({"status": "running", "updated": now, "started": now})
            await self.db.save(tasks)
            return self._copy_task(task)

    async def mark_succeeded(self, task_id: str, result: dict[str, Any]) -> None:
        """保存成功结果并结束任务。"""
        await self._update_terminal(task_id, "succeeded", result=result, error=None)

    async def mark_failed(self, task_id: str, message: Any, *, code: str = "video_generation_failed") -> None:
        """保存失败信息并结束任务。"""
        error = {"code": code, "message": sanitize_error_message(message)}
        await self._update_terminal(task_id, "failed", result=None, error=error)

    async def mark_interrupted_pending(self) -> int:
        """服务启动时将遗留 queued/running 任务标记为 interrupted。"""
        now = int(time.time())
        changed = 0
        async with self._lock:
            tasks = await self._tasks_unlocked()
            for task in tasks:
                if task.get("status") in VIDEO_TASK_PENDING_STATUSES:
                    task.update({
                        "status": "interrupted",
                        "updated": now,
                        "finished": now,
                        "error": {"code": "interrupted", "message": "Task was interrupted by service restart"},
                    })
                    changed += 1
            if changed:
                await self.db.save(tasks)
        return changed

    async def mark_expired_tasks(self) -> int:
        """将超过过期时间且尚未终态的任务标记为 expired。"""
        now = int(time.time())
        changed = 0
        async with self._lock:
            tasks = await self._tasks_unlocked()
            for task in tasks:
                if task.get("status") in {"queued", "running"} and int(task.get("expires") or 0) <= now:
                    task.update({"status": "expired", "updated": now, "finished": now})
                    changed += 1
            if changed:
                await self.db.save(tasks)
        return changed

    async def _update_terminal(self, task_id: str, status: str, *, result: Any, error: Any) -> None:
        """保存任务终态，统一维护更新时间和完成时间。"""
        now = int(time.time())
        async with self._lock:
            tasks = await self._tasks_unlocked()
            task = self._find_in_list(tasks, task_id)
            if not task:
                return
            task.update({"status": status, "result": result, "error": error, "updated": now, "finished": now})
            await self.db.save(tasks)

    async def _tasks_unlocked(self) -> list[dict[str, Any]]:
        """读取当前任务列表；调用方必须持有 store 锁。"""
        tasks = await self.db.get()
        if not isinstance(tasks, list):
            tasks = []
            await self.db.save(tasks)
        return tasks

    async def _find_unlocked(self, task_id: str) -> dict[str, Any] | None:
        """在当前任务列表中查找任务；调用方必须持有 store 锁。"""
        return self._find_in_list(await self._tasks_unlocked(), task_id)

    @staticmethod
    def _find_in_list(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
        """在给定任务列表中查找任务引用。"""
        for task in tasks:
            if isinstance(task, dict) and task.get("id") == task_id:
                return task
        return None

    @staticmethod
    def _copy_task(task: dict[str, Any] | None) -> dict[str, Any] | None:
        """返回任务浅拷贝，避免调用方直接修改内存状态。"""
        return dict(task) if isinstance(task, dict) else None


class VideoTaskRunner:
    """单进程轻量视频任务运行器。"""

    def __init__(self, app: Any, store: VideoTaskStore, *, concurrency: int = 1):
        self.app = app
        self.store = store
        self.concurrency = max(1, int(concurrency or 1))
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动 worker 与过期任务清理循环。"""
        for index in range(self.concurrency):
            worker = asyncio.create_task(self._worker_loop(), name=f"video-task-worker-{index + 1}")
            self._workers.append(worker)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="video-task-cleanup")

    async def stop(self) -> None:
        """停止 worker 与清理循环。"""
        tasks = [*self._workers]
        if self._cleanup_task:
            tasks.append(self._cleanup_task)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        self._cleanup_task = None

    async def enqueue(self, task_id: str) -> None:
        """将任务 ID 放入后台队列。"""
        await self._queue.put(task_id)

    async def _worker_loop(self) -> None:
        """串行消费任务队列，复用现有视频生成逻辑。"""
        while True:
            task_id = await self._queue.get()
            try:
                await self._run_one(task_id)
            finally:
                self._queue.task_done()

    async def _run_one(self, task_id: str) -> None:
        """执行单个视频任务并落库结果。"""
        task = await self.store.mark_running(task_id)
        if not task:
            return
        try:
            # 延迟导入避免 main -> services -> api 的循环导入。
            from backend.api.videos import _generate_video_data

            result = await _generate_video_data(self.app, task.get("request") or {})
            await self.store.mark_succeeded(task_id, result)
            log.info("[VideoTask] 任务完成 task_id=%s", task_id)
        except Exception as exc:
            await self.store.mark_failed(task_id, exc)
            log.warning("[VideoTask] 任务失败 task_id=%s error=%s", task_id, sanitize_error_message(exc))

    async def _cleanup_loop(self) -> None:
        """周期性标记过期任务，避免任务长期停留在 running/queued。"""
        while True:
            await asyncio.sleep(60)
            changed = await self.store.mark_expired_tasks()
            if changed:
                log.info("[VideoTask] 已标记 %s 个过期任务", changed)
