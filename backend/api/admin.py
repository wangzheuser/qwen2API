import asyncio
import math

from fastapi import APIRouter, Body, Depends, HTTPException, Header, Query, Request
from pydantic import BaseModel
from backend.core.config import (
    KEEPALIVE_MAX_INTERVAL,
    KEEPALIVE_MIN_INTERVAL,
    get_keepalive_config,
    settings,
    update_keepalive_config,
)
from backend.core.database import AsyncJsonDB
from backend.core.account_pool import AccountPool
import secrets

router = APIRouter()
_verify_all_lock = asyncio.Lock()

def verify_admin(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split("Bearer ")[1]

    from backend.core.config import API_KEYS, settings as backend_settings

    # 允许使用默认管理员 Key (ADMIN_KEY) 或者任何已生成的 API_KEYS 作为管理凭证
    if token != backend_settings.ADMIN_KEY and token not in API_KEYS:
        raise HTTPException(status_code=403, detail="Forbidden: Admin Key Mismatch")
    return token

class UserCreate(BaseModel):
    name: str
    quota: int = 1000000

class User(BaseModel):
    id: str
    name: str
    quota: int
    used_tokens: int


class ApiKeyCreate(BaseModel):
    mode: str = "auto"
    key: str = ""

def _parse_bool(value, default: bool = False) -> bool:
    """解析查询参数或 JSON 字段中的布尔值。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def _repair_requested(request: Request, default: bool = False) -> bool:
    """读取 repair 开关；查询参数优先，其次兼容 JSON body。"""
    if "repair" in request.query_params:
        return _parse_bool(request.query_params.get("repair"), default)
    try:
        body = await request.json()
    except Exception:
        return default
    if isinstance(body, dict) and "repair" in body:
        return _parse_bool(body.get("repair"), default)
    return default


def _account_summary_item(acc, pool: AccountPool, *, include_secret: bool = False) -> dict:
    """构造管理端账号条目，默认不返回敏感凭证。"""
    item = {
        "email": acc.email,
        "username": acc.username,
        "activation_pending": acc.activation_pending,
        "status_code": acc.get_status_code(),
        "status_text": acc.get_status_text(),
        "last_error": acc.last_error,
        "source": acc.source,
        "env_name": acc.env_name,
        "last_request_started": acc.last_request_started,
        "last_request_finished": acc.last_request_finished,
        "consecutive_failures": acc.consecutive_failures,
        "rate_limit_strikes": acc.rate_limit_strikes,
        "heal_cooldown_until": getattr(acc, "heal_cooldown_until", 0.0),
        "valid": acc.valid,
        "inflight": acc.inflight,
        "rate_limited_until": acc.rate_limited_until,
        "max_inflight": getattr(pool, "max_inflight_per_account", 0),
    }
    if include_secret:
        item.update({
            "password": acc.password,
            "token": acc.token,
            "cookies": acc.cookies,
        })
    return item


@router.get("/status", dependencies=[Depends(verify_admin)])
async def get_system_status(
    request: Request,
    detail: bool = Query(False),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    pool = request.app.state.account_pool

    # 账号层细粒度 inflight / 状态
    per_account = []
    accounts = getattr(pool, "accounts", [])
    if detail:
        for acc in accounts[offset:offset + limit]:
            per_account.append({
                "email": acc.email,
                "status": acc.get_status_code(),
                "inflight": getattr(acc, "inflight", 0),
                "max_inflight": getattr(pool, "max_inflight_per_account", 0),
                "consecutive_failures": getattr(acc, "consecutive_failures", 0),
                "rate_limit_strikes": getattr(acc, "rate_limit_strikes", 0),
                "last_request_finished": getattr(acc, "last_request_finished", 0),
            })

    # chat_id 预热池指标（若已启用）
    chat_id_pool_stats = None
    cp = getattr(request.app.state, "chat_id_pool", None)
    if cp is not None:
        try:
            per_account_pool: dict[str, int] = {}
            if detail:
                for acc in accounts[offset:offset + limit]:
                    per_account_pool[acc.email] = await cp.size(acc.email)
            chat_id_pool_stats = {
                "total_cached": await cp.total_size(),
                "target_per_account": cp.target,
                "configured_target_per_account": getattr(cp, "configured_target", cp.target),
                "ttl_seconds": cp._ttl,
                "large_pool_suppressed": cp.is_large_pool_prewarm_suppressed() if hasattr(cp, "is_large_pool_prewarm_suppressed") else False,
            }
            if detail:
                chat_id_pool_stats["per_account"] = per_account_pool
        except Exception:
            chat_id_pool_stats = {"error": "snapshot failed"}

    # 向运行时拿全局任务计数 / asyncio 状态
    import asyncio
    try:
        tasks = asyncio.all_tasks()
        running_tasks = sum(1 for t in tasks if not t.done())
    except Exception:
        running_tasks = -1

    from backend.core.browser_engine import get_browser_metrics

    payload = {
        "accounts": pool.status(),
        "chat_id_pool": chat_id_pool_stats,
        "runtime": {
            "asyncio_running_tasks": running_tasks,
        },
        "request_runtime": {
            "mode": "direct_http",
            "browser_required_for_requests": False,
            "description": "普通请求直连 HTTP，不经过浏览器",
        },
        "browser_automation": {
            "mode": "on_demand_registration_only",
            "description": "仅注册/激活/刷新 Token 时按需启动真实浏览器",
            "metrics": get_browser_metrics(),
        }
    }
    if detail:
        payload["per_account"] = per_account
        payload["pagination"] = {
            "limit": limit,
            "offset": offset,
            "total": len(accounts),
        }
    return payload

@router.get("/users", dependencies=[Depends(verify_admin)])
async def list_users(request: Request):
    db: AsyncJsonDB = request.app.state.users_db
    data = await db.get()
    return {"users": data}

@router.post("/users", dependencies=[Depends(verify_admin)])
async def create_user(user: UserCreate, request: Request):
    import uuid
    db: AsyncJsonDB = request.app.state.users_db
    data = await db.get()
    new_user = {
        "id": f"sk-{uuid.uuid4().hex}",
        "name": user.name,
        "quota": user.quota,
        "used_tokens": 0
    }
    data.append(new_user)
    await db.save(data)
    return new_user

@router.post("/accounts", dependencies=[Depends(verify_admin)])
async def add_account(request: Request):
    import time
    from backend.core.account_pool import Account, AccountPool
    from backend.services.qwen_client import QwenClient

    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, detail="Invalid JSON body")

    token = data.get("token", "")
    if not token:
        raise HTTPException(400, detail="token is required")

    acc = Account(
        email=data.get("email", f"manual_{int(time.time())}@qwen"),
        password=data.get("password", ""),
        token=token,
        cookies=data.get("cookies", ""),
        username=data.get("username", "")
    )

    is_valid = await client.verify_token(token)
    if not is_valid:
        return {"ok": False, "error": "Invalid token (验证失败，请确认Token有效)"}

    await pool.add(acc)
    return {"ok": True, "email": acc.email}


@router.get("/accounts", dependencies=[Depends(verify_admin)])
async def list_accounts(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    include_secret: bool = Query(False),
):
    pool: AccountPool = request.app.state.account_pool
    accounts = list(pool.accounts)
    total = len(accounts)
    start = (page - 1) * page_size
    end = start + page_size
    accs = [_account_summary_item(a, pool, include_secret=include_secret) for a in accounts[start:end]]
    return {
        "accounts": accs,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, math.ceil(total / page_size)) if total else 1,
        "has_more": end < total,
        "summary": pool.status(),
    }

@router.post("/accounts/register", dependencies=[Depends(verify_admin)])
async def register_new_account(request: Request):
    """一键调用浏览器无头注册新千问账号"""
    import logging
    from backend.services.auth_resolver import register_qwen_account
    from backend.core.account_pool import AccountPool
    pool: AccountPool = request.app.state.account_pool

    log = logging.getLogger("backend.api.admin")

    client_ip = request.client.host if request.client else "127.0.0.1"
    log.info(f"[注册] 管理员触发注册，来源IP: {client_ip}")

    # 简单的频率限制保护
    current = len(pool.accounts)
    if current >= 100:
        return {"ok": False, "error": "账号池已满，请先清理死号"}

    try:
        acc = await register_qwen_account()
        if acc:
            await pool.add(acc)
            log.info(f"[注册] 注册成功: {acc.email}（当前账号数: {len(pool.accounts)}/100）")
            return {"ok": True, "email": acc.email, "message": "新账号注册成功并已入池"}
        return {"ok": False, "error": "自动化注册失败，可能遇到风控或页面元素改变"}
    except Exception as e:
        return {"ok": False, "error": f"注册发生异常: {str(e)}"}

@router.post("/verify", dependencies=[Depends(verify_admin)])
async def verify_all_accounts(request: Request):
    """逐个到 chat.qwen.ai 官网验证账号；repair=true 时才允许浏览器修复。"""
    from backend.core.account_pool import AccountPool
    from backend.services.qwen_client import QwenClient

    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client
    repair = await _repair_requested(request)

    if _verify_all_lock.locked():
        raise HTTPException(status_code=409, detail="全量巡检正在进行中，请稍后再试")

    async with _verify_all_lock:
        results = []
        for acc in pool.accounts:
            results.append(await client.verify_account(acc, repair=repair, persist=False, reset_pool=False))
        await pool.save()
        pool._reset_concurrency_limits()

    summary = {
        "total": len(results),
        "valid": sum(1 for item in results if item.get("valid")),
        "refreshed": sum(1 for item in results if item.get("refreshed")),
        "repaired": sum(1 for item in results if item.get("repaired")),
        "banned": sum(1 for item in results if item.get("status_code") == "banned"),
        "failed": sum(1 for item in results if not item.get("valid")),
    }
    return {"ok": True, "results": results, "summary": summary, "concurrency": 1, "repair": repair}

@router.post("/accounts/{email}/activate", dependencies=[Depends(verify_admin)])
async def activate_account(email: str, request: Request):
    """单独激活某个账号"""
    from backend.services.auth_resolver import activate_account as activate_logic
    from backend.core.account_pool import AccountPool

    pool: AccountPool = request.app.state.account_pool
    acc = pool.get_by_email(email)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    # 防止并发点击：检查一个运行时标志
    if getattr(acc, "_is_activating", False):
        return {"ok": False, "error": "该账号正在激活中，请勿重复点击"}

    try:
        setattr(acc, "_is_activating", True)
        success = await activate_logic(acc)
        if success:
            acc.valid = True
            acc.activation_pending = False
            await pool.add(acc) # 这会触发覆盖保存
            return {"ok": True, "message": "账号激活成功"}
        return {"ok": False, "error": "未能找到激活链接或获取Token"}
    finally:
        setattr(acc, "_is_activating", False)

@router.post("/accounts/{email}/verify", dependencies=[Depends(verify_admin)])
async def verify_account(email: str, request: Request):
    """单独到 chat.qwen.ai 官网验证账号；repair=true 时才允许浏览器修复。"""
    from backend.services.qwen_client import QwenClient
    from backend.core.account_pool import AccountPool

    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client

    acc = pool.get_by_email(email)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    repair = await _repair_requested(request)
    return await client.verify_account(acc, repair=repair)

@router.delete("/accounts/{email}", dependencies=[Depends(verify_admin)])
async def delete_account(email: str, request: Request):
    from backend.core.account_pool import AccountPool
    pool: AccountPool = request.app.state.account_pool
    acc = pool.get_by_email(email)
    if acc and getattr(acc, "source", "") == "env":
        raise HTTPException(status_code=400, detail="环境变量注入账号不能在面板删除，请移除对应环境变量后重启服务")
    await pool.remove(email)
    return {"ok": True}

@router.get("/settings", dependencies=[Depends(verify_admin)])
async def get_settings(request: Request):
    from backend.core.config import MODEL_MAP
    from backend.core.config import settings as backend_settings

    safe_map = {k: v for k, v in MODEL_MAP.items()}
    pool = getattr(request.app.state, "chat_id_pool", None)
    acc_pool = getattr(request.app.state, "account_pool", None)
    keepalive_config = await get_keepalive_config(request.app.state.config_db)
    keepalive_service = getattr(request.app.state, "keepalive_service", None)
    return {
        "version": "2.0.0",
        "max_inflight_per_account": backend_settings.MAX_INFLIGHT_PER_ACCOUNT,
        "global_max_inflight": getattr(acc_pool, "global_max_inflight", 0),
        "max_queue_size": getattr(acc_pool, "max_queue_size", 0),
        "account_ready_set_threshold": backend_settings.ACCOUNT_READY_SET_THRESHOLD,
        "account_ready_set_enabled": getattr(acc_pool, "ready_set_enabled", False),
        "chat_id_pool_target": pool.target if pool else 0,
        "chat_id_pool_configured_target": getattr(pool, "configured_target", pool.target) if pool else 0,
        "chat_id_pool_ttl_seconds": pool.ttl if pool else 0,
        "chat_id_pool_max_concurrency": pool.max_concurrency if pool else 0,
        "chat_id_pool_large_pool_threshold": backend_settings.CHAT_ID_PREWARM_LARGE_POOL_THRESHOLD,
        "chat_id_pool_large_pool_enabled": backend_settings.CHAT_ID_PREWARM_LARGE_POOL_ENABLED,
        "auto_heal_on_auth_failure": backend_settings.AUTO_HEAL_ON_AUTH_FAILURE,
        "auto_heal_cooldown_seconds": backend_settings.AUTO_HEAL_COOLDOWN_SECONDS,
        "keepalive_url": keepalive_config["keepalive_url"],
        "keepalive_interval": keepalive_config["keepalive_interval"],
        "keepalive_env_locked": keepalive_config["env_locked"],
        "keepalive_running": keepalive_service.is_running if keepalive_service else False,
        "keepalive_status": keepalive_service.status() if keepalive_service else {},
        "model_aliases": safe_map,
    }

@router.put("/settings", dependencies=[Depends(verify_admin)])
async def update_settings(data: dict, request: Request):
    from backend.core.config import MODEL_MAP
    if "max_inflight_per_account" in data:
        try:
            val = int(data["max_inflight_per_account"])
            settings.MAX_INFLIGHT_PER_ACCOUNT = val
            pool = getattr(request.app.state, "account_pool", None)
            if pool is not None and hasattr(pool, "set_max_inflight"):
                pool.set_max_inflight(val)
        except (TypeError, ValueError):
            pass
    if "account_ready_set_threshold" in data:
        try:
            val = max(1, int(data["account_ready_set_threshold"]))
            settings.ACCOUNT_READY_SET_THRESHOLD = val
            pool = getattr(request.app.state, "account_pool", None)
            if pool is not None and hasattr(pool, "_reset_concurrency_limits"):
                pool._reset_concurrency_limits()
        except (TypeError, ValueError):
            pass
    if "auto_heal_on_auth_failure" in data:
        settings.AUTO_HEAL_ON_AUTH_FAILURE = _parse_bool(data.get("auto_heal_on_auth_failure"), False)
    if "auto_heal_cooldown_seconds" in data:
        try:
            settings.AUTO_HEAL_COOLDOWN_SECONDS = max(0, int(data["auto_heal_cooldown_seconds"]))
        except (TypeError, ValueError):
            pass
    if "chat_id_pool_large_pool_threshold" in data:
        try:
            settings.CHAT_ID_PREWARM_LARGE_POOL_THRESHOLD = max(1, int(data["chat_id_pool_large_pool_threshold"]))
        except (TypeError, ValueError):
            pass
    if "chat_id_pool_large_pool_enabled" in data:
        settings.CHAT_ID_PREWARM_LARGE_POOL_ENABLED = _parse_bool(data.get("chat_id_pool_large_pool_enabled"), False)
    if "global_max_inflight" in data:
        try:
            val = int(data["global_max_inflight"])
            pool = getattr(request.app.state, "account_pool", None)
            if pool is not None and val > 0:
                pool.global_max_inflight = val
        except (TypeError, ValueError):
            pass
    if "chat_id_pool_target" in data or "chat_id_pool_ttl_seconds" in data or "chat_id_pool_max_concurrency" in data:
        cp = getattr(request.app.state, "chat_id_pool", None)
        if cp is not None:
            await cp.apply_config(
                target=data.get("chat_id_pool_target"),
                ttl_seconds=data.get("chat_id_pool_ttl_seconds"),
                max_concurrency=data.get("chat_id_pool_max_concurrency"),
            )
    if "chat_id_pool_large_pool_threshold" in data or "chat_id_pool_large_pool_enabled" in data:
        cp = getattr(request.app.state, "chat_id_pool", None)
        if cp is not None:
            await cp.prune_to_target()
    if "keepalive_url" in data or "keepalive_interval" in data:
        if "keepalive_url" in data and not isinstance(data["keepalive_url"], str):
            raise HTTPException(status_code=400, detail="保活 URL 必须是字符串")
        if "keepalive_interval" in data:
            try:
                interval = int(data["keepalive_interval"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="保活间隔必须是有效整数")
            if interval < KEEPALIVE_MIN_INTERVAL or interval > KEEPALIVE_MAX_INTERVAL:
                raise HTTPException(status_code=400, detail="保活间隔必须在 5 - 86400 秒之间")
            data["keepalive_interval"] = interval
        await update_keepalive_config(request.app.state.config_db, data)
        keepalive_service = getattr(request.app.state, "keepalive_service", None)
        if keepalive_service is not None:
            await keepalive_service.restart()
    if "model_aliases" in data:
        MODEL_MAP.clear()
        MODEL_MAP.update(data["model_aliases"])
    return {"ok": True}

@router.get("/keys", dependencies=[Depends(verify_admin)])
async def get_keys():
    from backend.core.config import list_api_key_items

    items = list_api_key_items()
    return {"keys": [item["key"] for item in items], "items": items}

@router.post("/keys", dependencies=[Depends(verify_admin)])
async def create_key(payload: ApiKeyCreate | None = Body(default=None)):
    from backend.core.config import API_KEYS, add_api_key

    mode = (payload.mode if payload else "auto").strip().lower()
    if mode == "custom":
        new_key = (payload.key if payload else "").strip()
        if not new_key:
            raise HTTPException(status_code=400, detail="自定义 Key 不能为空")
        if any(ch.isspace() for ch in new_key):
            raise HTTPException(status_code=400, detail="自定义 Key 不能包含空白字符")
    else:
        new_key = f"sk-{secrets.token_hex(24)}"

    if new_key in API_KEYS or not add_api_key(new_key):
        raise HTTPException(status_code=409, detail="API Key 已存在")
    return {"ok": True, "key": new_key}

@router.delete("/keys/{key}", dependencies=[Depends(verify_admin)])
async def delete_key(key: str):
    from backend.core.config import remove_api_key

    result = remove_api_key(key)
    if result == "env":
        raise HTTPException(status_code=400, detail="环境变量注入 Key 不能在面板删除，请移除对应环境变量后重启服务")
    return {"ok": True}
