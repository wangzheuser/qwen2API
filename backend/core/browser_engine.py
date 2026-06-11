import asyncio
import os
import sys
from contextlib import asynccontextmanager

from backend.core.config import settings

_CAMOUFOX_OPTS = {
    "headless": True,
    "humanize": False,
    "i_know_what_im_doing": True,
    "firefox_user_prefs": {
        "layers.acceleration.disabled": True,
        "gfx.webrender.enabled": False,
        "gfx.webrender.all": False,
        "gfx.webrender.software": False,
        "gfx.canvas.azure.backends": "skia",
        "media.hardware-video-decoding.enabled": False,
    },
}

_browser_semaphore: asyncio.Semaphore | None = None
_browser_semaphore_limit = 0
_browser_active = 0
_browser_waiting = 0
_browser_launched_total = 0
_browser_failed_total = 0


def _browser_limit() -> int:
    """返回当前进程允许同时运行的浏览器实例数。"""
    return max(1, int(getattr(settings, "BROWSER_POOL_SIZE", 1) or 1))


def _get_browser_semaphore() -> asyncio.Semaphore:
    """懒加载浏览器信号量，支持测试中动态调整配置。"""
    global _browser_semaphore, _browser_semaphore_limit
    limit = _browser_limit()
    if _browser_semaphore is None or _browser_semaphore_limit != limit:
        _browser_semaphore = asyncio.Semaphore(limit)
        _browser_semaphore_limit = limit
    return _browser_semaphore


def get_browser_metrics() -> dict[str, int]:
    """返回 Camoufox/Playwright 运行指标，供管理面板展示。"""
    return {
        "limit": _browser_limit(),
        "active": _browser_active,
        "waiting": _browser_waiting,
        "launched_total": _browser_launched_total,
        "failed_total": _browser_failed_total,
    }


@asynccontextmanager
async def _new_browser():
    from camoufox.async_api import AsyncCamoufox

    global _browser_active, _browser_waiting, _browser_launched_total, _browser_failed_total

    semaphore = _get_browser_semaphore()
    _browser_waiting += 1
    try:
        await semaphore.acquire()
    finally:
        _browser_waiting = max(0, _browser_waiting - 1)
    _browser_active += 1

    manager = AsyncCamoufox(**_CAMOUFOX_OPTS)
    exc_info = (None, None, None)
    try:
        try:
            browser = await manager.__aenter__()
            _browser_launched_total += 1
        except Exception:
            _browser_failed_total += 1
            raise

        try:
            yield browser
        except BaseException:
            exc_info = sys.exc_info()
            raise
        finally:
            try:
                await manager.__aexit__(*exc_info)
            except Exception:
                _browser_failed_total += 1
                raise
    finally:
        _browser_active = max(0, _browser_active - 1)
        semaphore.release()


async def ensure_browser_installed():
    import subprocess
    import sys

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                [sys.executable, "-m", "camoufox", "path"],
                capture_output=True,
                text=True,
                timeout=10,
            ),
        )
        cache_dir = result.stdout.strip()
        if cache_dir:
            exe_name = "camoufox.exe" if os.name == "nt" else "camoufox"
            exe_path = os.path.join(cache_dir, exe_name)
            if os.path.exists(exe_path):
                return
    except Exception:
        pass

    loop = asyncio.get_event_loop()

    def _do_install():
        from camoufox.pkgman import CamoufoxFetcher

        CamoufoxFetcher().install()

    await loop.run_in_executor(None, _do_install)
