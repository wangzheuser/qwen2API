import asyncio
import json
import logging
import os
from pathlib import Path
import threading
from typing import Any

log = logging.getLogger("qwen2api.db")

class AsyncJsonDB:
    """带异步读写锁的 JSON 文件存储，防止并发损坏。"""
    def __init__(self, path: str | Path, default_data: Any = None):
        self.path = Path(path)
        self.default_data = default_data if default_data is not None else []
        self._lock = asyncio.Lock()
        self._data: Any = None
        self._init_file()

    def _init_file(self):
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.default_data, indent=2, ensure_ascii=False), encoding="utf-8")

    async def load(self) -> Any:
        async with self._lock:
            if not self.path.exists():
                self._data = self.default_data
                return self._data
            try:
                # 大账号池下 JSON 文件可能较大，读文件放到线程池避免阻塞事件循环。
                content = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
                self._data = json.loads(content)
            except Exception as e:
                log.error(f"Failed to load JSON from {self.path}: {e}")
                self._data = self.default_data
            return self._data

    async def save(self, data: Any):
        async with self._lock:
            self._data = data
            try:
                # 先写临时文件再原子替换，避免进程异常时留下半截 JSON。
                await asyncio.to_thread(self._write_atomic, self._data)
            except Exception as e:
                log.error(f"Failed to save JSON to {self.path}: {e}")

    async def get(self) -> Any:
        if self._data is None:
            return await self.load()
        return self._data

    def _write_atomic(self, data: Any) -> None:
        """在线程池中完成 JSON 序列化和原子落盘。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        tmp_path = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, self.path)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
