"""主动式定时刷新 token 服务。

作者：wangqiupei

周期性扫描账号池，把临近过期且具备重登条件的账号交给 AuthResolver 重登换新。
结构对齐 KeepAliveService，与被动自愈（AuthResolver.schedule_auto_heal）通过
healing / heal_cooldown_until 字段互斥，避免同账号被双开浏览器。
"""
import asyncio
import logging
import time
from typing import Optional

from backend.core.config import settings
from backend.services.auth_resolver import token_expiry

log = logging.getLogger("qwen2api.token_refresh")


class TokenRefreshService:
    def __init__(self, account_pool, auth_resolver):
        self.account_pool = account_pool
        self.auth_resolver = auth_resolver
        self._task: Optional[asyncio.Task] = None
        self.last_run_at: float = 0.0
        self.last_refresh_at: float = 0.0
        self.refreshed_total: int = 0
        self.failed_total: int = 0
        self.last_error: str = ""

    def _due_accounts(self, now: float) -> list:
        """筛出临近过期且可主动刷新的账号。

        条件：有密码、非 env 源、当前未在自愈、不在冷却期，
        且 token 剩余寿命落在 (0, AHEAD_SECONDS) 区间——已过期或解析失败一律跳过。
        """
        ahead = settings.TOKEN_REFRESH_AHEAD_SECONDS
        due = []
        for acc in getattr(self.account_pool, "accounts", []):
            if not getattr(acc, "password", ""):
                continue
            if getattr(acc, "source", "") == "env":
                continue
            if getattr(acc, "healing", False):
                continue
            if float(getattr(acc, "heal_cooldown_until", 0.0) or 0.0) > now:
                continue
            remaining = token_expiry(getattr(acc, "token", "")) - now
            if 0 < remaining < ahead:
                due.append(acc)
        return due

    async def _run(self) -> None:
        interval = settings.TOKEN_REFRESH_CHECK_INTERVAL
        stagger = settings.TOKEN_REFRESH_STAGGER_MS / 1000.0
        log.info(
            "[TokenRefresh] 主动刷新任务启动，间隔=%ss，提前量=%ss",
            interval, settings.TOKEN_REFRESH_AHEAD_SECONDS,
        )
        while True:
            try:
                now = time.time()
                self.last_run_at = now
                due = self._due_accounts(now)
                if due:
                    log.info("[TokenRefresh] 本轮待刷新账号 %s 个", len(due))
                for acc in due:
                    try:
                        # save=False：整轮结束统一落盘一次，避免频繁写文件。
                        ok = await self.auth_resolver.refresh_token(acc, save=False)
                        if ok:
                            self.refreshed_total += 1
                            self.last_refresh_at = time.time()
                        else:
                            self.failed_total += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.failed_total += 1
                        self.last_error = str(exc)
                        log.warning("[TokenRefresh] %s 刷新异常: %s", getattr(acc, "email", "?"), exc)
                    await asyncio.sleep(stagger)
                if due:
                    await self.account_pool.save()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("[TokenRefresh] 扫描轮异常: %s", exc)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not settings.TOKEN_REFRESH_ENABLED:
            log.info("[TokenRefresh] 未启用（TOKEN_REFRESH_ENABLED=false），服务不启动")
            return
        self._task = asyncio.create_task(self._run(), name="qwen2api_token_refresh")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            log.info("[TokenRefresh] 服务已停止")
        self._task = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {
            "running": self.is_running,
            "enabled": settings.TOKEN_REFRESH_ENABLED,
            "check_interval": settings.TOKEN_REFRESH_CHECK_INTERVAL,
            "ahead_seconds": settings.TOKEN_REFRESH_AHEAD_SECONDS,
            "last_run_at": self.last_run_at,
            "last_refresh_at": self.last_refresh_at,
            "refreshed_total": self.refreshed_total,
            "failed_total": self.failed_total,
            "last_error": self.last_error,
        }
