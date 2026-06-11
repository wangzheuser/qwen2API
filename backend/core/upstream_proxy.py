"""Qwen 上游代理解析与运行时账号绑定。"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from backend.core.config import settings

log = logging.getLogger("qwen2api.upstream_proxy")

_UUID_TOKEN = "{uuid}"
_GLOBAL_BINDING_KEY = "__global__"


@dataclass
class _ProxyBinding:
    """记录账号运行期解析出的代理节点。"""

    proxy_url: str
    created_at: float
    failures: int = 0
    last_failed_at: float = 0.0


_lock = threading.RLock()
_bindings: dict[str, _ProxyBinding] = {}
_failures_total = 0
_last_failure_at = 0.0


def _raw_proxy() -> str:
    """读取当前代理模板。"""
    return str(getattr(settings, "QWEN_UPSTREAM_PROXY", "") or "").strip()


def is_proxy_enabled() -> bool:
    """判断 Qwen 上游代理是否启用。"""
    return bool(getattr(settings, "QWEN_PROXY_ENABLED", False) and _raw_proxy())


def is_template_proxy(proxy: str | None = None) -> bool:
    """判断代理配置是否包含 UUID 模板占位符。"""
    return _UUID_TOKEN in (proxy if proxy is not None else _raw_proxy())


def _binding_key(account: Any | None) -> str:
    """生成运行时代理绑定 key。"""
    if account is None:
        return _GLOBAL_BINDING_KEY
    email = str(getattr(account, "email", "") or "").strip()
    if email:
        return email
    token = str(getattr(account, "token", "") or "").strip()
    if token:
        return f"token:{token[:32]}"
    return _GLOBAL_BINDING_KEY


def _render_template(proxy: str) -> str:
    """将 {uuid} 渲染为无横线 UUID。"""
    return proxy.replace(_UUID_TOKEN, uuid.uuid4().hex)


def resolve_proxy(account: Any | None = None, *, force_refresh: bool = False) -> str | None:
    """解析当前请求应使用的代理 URL。

    静态代理直接返回配置；包含 {uuid} 的代理模板按账号运行时绑定。
    """
    if not is_proxy_enabled():
        return None

    proxy = _raw_proxy()
    if not is_template_proxy(proxy):
        return proxy

    bind_per_account = bool(getattr(settings, "QWEN_PROXY_POOL_BIND_PER_ACCOUNT", True))
    key = _binding_key(account) if bind_per_account else _GLOBAL_BINDING_KEY

    with _lock:
        binding = _bindings.get(key)
        if binding is not None and not force_refresh:
            return binding.proxy_url

        resolved = _render_template(proxy)
        _bindings[key] = _ProxyBinding(proxy_url=resolved, created_at=time.time())
        return resolved


def mark_proxy_failure(account: Any | None, proxy_url: str | None, exc: BaseException | None = None) -> None:
    """标记代理网络异常，并清理该账号运行时绑定。"""
    if not proxy_url:
        return

    global _failures_total, _last_failure_at
    key = _binding_key(account)
    now = time.time()
    with _lock:
        _failures_total += 1
        _last_failure_at = now
        binding = _bindings.get(key)
        if binding is not None:
            binding.failures += 1
            binding.last_failed_at = now
            if binding.proxy_url == proxy_url:
                _bindings.pop(key, None)

    log.warning(
        "[UpstreamProxy] proxy failure key=%s proxy=%s error=%s",
        key,
        mask_proxy_url(proxy_url),
        exc,
    )


def clear_proxy_bindings() -> None:
    """清空运行时代理绑定。"""
    with _lock:
        _bindings.clear()


def is_proxy_network_error(exc: BaseException) -> bool:
    """判断异常是否属于代理/网络层失败，避免误判 401/403/429。"""
    if isinstance(
        exc,
        (
            httpx.ProxyError,
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "proxy",
            "socks",
            "err_tunnel",
            "err_proxy",
            "connection refused",
            "connection reset",
            "connect call failed",
            "network is unreachable",
        )
    )


def mask_proxy_url(proxy_url: str | None) -> str:
    """脱敏展示代理 URL，避免泄露密码。"""
    if not proxy_url:
        return ""
    try:
        parts = urlsplit(proxy_url)
        if not parts.username:
            return proxy_url

        username = unquote(parts.username)
        masked_user = username[:2] + "***" if len(username) > 2 else "***"
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = quote(masked_user, safe="*")
        if parts.password is not None:
            netloc += ":***"
        netloc += f"@{host}"
        if parts.port is not None:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return "***"


def to_playwright_proxy(proxy_url: str | None) -> dict[str, str] | None:
    """转换为 Playwright/Camoufox launch proxy 参数。"""
    if not proxy_url:
        return None
    parts = urlsplit(proxy_url)
    if not parts.scheme or not parts.hostname:
        return {"server": proxy_url}

    host = parts.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    server = f"{parts.scheme}://{host}"
    if parts.port is not None:
        server += f":{parts.port}"

    result = {"server": server}
    if parts.username:
        result["username"] = unquote(parts.username)
    if parts.password:
        result["password"] = unquote(parts.password)
    return result


def proxy_status() -> dict[str, Any]:
    """返回管理端可展示的代理聚合状态。"""
    raw = _raw_proxy()
    with _lock:
        bound_accounts = len(_bindings)
        failures_total = _failures_total
        last_failure_at = _last_failure_at
    return {
        "enabled": is_proxy_enabled(),
        "configured": bool(raw),
        "template_mode": is_template_proxy(raw),
        "bind_per_account": bool(getattr(settings, "QWEN_PROXY_POOL_BIND_PER_ACCOUNT", True)),
        "failure_cooldown_seconds": int(getattr(settings, "QWEN_PROXY_FAILURE_COOLDOWN_SECONDS", 300) or 0),
        "masked_proxy": mask_proxy_url(raw),
        "bound_accounts": bound_accounts,
        "failures_total": failures_total,
        "last_failure_at": last_failure_at,
    }
