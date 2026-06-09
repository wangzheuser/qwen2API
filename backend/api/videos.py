"""
视频生成接口 — 兼容 OpenAI 风格的 /v1/videos/generations。
"""
import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import mimetypes
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.api.images import _extract_upstream_failure, _normalize_image_size
from backend.services.qwen_client import QwenClient
from backend.services.video_task_store import hash_task_owner

log = logging.getLogger("qwen2api.videos")
router = APIRouter()

DEFAULT_VIDEO_MODEL = "qwen3.6-plus"
DEFAULT_I2V_VIDEO_MODEL = "qwen3.7-plus"
MAX_FIRST_FRAME_BYTES = 20 * 1024 * 1024
MAX_FIRST_FRAME_REDIRECTS = 3

VIDEO_MODEL_MAP = {
    "qwen-video": "qwen3.6-plus",
    "qwen-video-plus": "qwen3.6-plus",
    "qwen-video-turbo": "qwen3.6-plus",
    "qwen3.6-plus-video": "qwen3.6-plus",
    "qwen-i2v": DEFAULT_I2V_VIDEO_MODEL,
    "qwen-image-to-video": DEFAULT_I2V_VIDEO_MODEL,
    "qwen-video-i2v": DEFAULT_I2V_VIDEO_MODEL,
    "qwen3.7-plus-i2v": DEFAULT_I2V_VIDEO_MODEL,
}

VIDEO_URL_KEYS = {
    "url",
    "video",
    "src",
    "videoUrl",
    "video_url",
    "videoURL",
    "preview_url",
    "previewUrl",
    "download_url",
    "downloadUrl",
    "origin_url",
    "originUrl",
    "oss_url",
    "ossUrl",
    "signed_url",
    "signedUrl",
}

TASK_ID_KEYS = {
    "task_id",
    "taskId",
    "wanx_task_id",
    "wanxTaskId",
}

VIDEO_RUNNING_STATUSES = {"running", "pending", "queued", "processing", "created"}
VIDEO_SUCCESS_STATUSES = {"success", "succeeded", "finished", "completed"}


def _looks_like_video_url(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return False
    lowered = value.lower()
    if re.search(r"\.(?:mp4|webm|mov|m3u8)(?:[?#][^\s\"'<>]*)?$", lowered):
        return True
    video_hosts = ("cdn.qwenlm.ai", "wanx.alicdn.com", "alicdn.com")
    return any(host in lowered for host in video_hosts) and any(marker in lowered for marker in ("video", "mp4", "t2v"))


def _collect_video_urls_from_obj(value: Any, urls: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and (key in VIDEO_URL_KEYS or _looks_like_video_url(item)):
                if _looks_like_video_url(item):
                    urls.append(item)
            else:
                _collect_video_urls_from_obj(item, urls)
        return
    if isinstance(value, list):
        for item in value:
            _collect_video_urls_from_obj(item, urls)


def _collect_task_ids_from_obj(value: Any, task_ids: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in TASK_ID_KEYS and isinstance(item, str) and item:
                task_ids.append(item)
                continue
            _collect_task_ids_from_obj(item, task_ids)
        return
    if isinstance(value, list):
        for item in value:
            _collect_task_ids_from_obj(item, task_ids)


def _extract_video_urls(text: str) -> list[str]:
    urls: list[str] = []

    for u in re.findall(r'!\[.*?\]\((https?://[^\s\)]+)\)', text):
        if _looks_like_video_url(u):
            urls.append(u.rstrip(").,;"))

    for u in re.findall(r'"(?:url|video|src|videoUrl|video_url)"\s*:\s*"(https?://[^"]+)"', text):
        if _looks_like_video_url(u):
            urls.append(u)

    video_pattern = r'https?://[^\s"<>]+\.(?:mp4|webm|mov|m3u8)(?:[^\s"<>]*)'
    for u in re.findall(video_pattern, text, re.IGNORECASE):
        urls.append(u.rstrip(".,;)\"'>"))

    for match in re.finditer(r"[\{\[]", text or ""):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[match.start():])
        except Exception:
            continue
        _collect_video_urls_from_obj(obj, urls)

    seen: set[str] = set()
    result: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def _extract_task_ids(text: str) -> list[str]:
    task_ids: list[str] = []
    for match in re.finditer(r"[\{\[]", text or ""):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[match.start():])
        except Exception:
            continue
        _collect_task_ids_from_obj(obj, task_ids)

    seen: set[str] = set()
    result: list[str] = []
    for task_id in task_ids:
        if task_id not in seen:
            seen.add(task_id)
            result.append(task_id)
    return result


def _resolve_video_model(requested: str | None, *, generation_chat_type: str = "t2v") -> str:
    """按生成模式解析视频模型别名，I2V 默认使用实测可用模型。"""
    from backend.core.config import resolve_model
    from backend.services.model_modes import parse_model_mode

    default_model = DEFAULT_I2V_VIDEO_MODEL if generation_chat_type == "i2v" else DEFAULT_VIDEO_MODEL
    if not requested:
        return default_model
    aliased = VIDEO_MODEL_MAP.get(str(requested).strip(), str(requested).strip())
    mode = parse_model_mode(aliased, default_model=default_model)
    return resolve_model(mode.base_model or default_model)


def _get_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _is_image_content_type(content_type: str) -> bool:
    """校验首帧文件 MIME，避免把普通文档传入 I2V 链路。"""
    return (content_type or "").split(";", 1)[0].strip().lower().startswith("image/")


def _image_extension(content_type: str, fallback: str = ".png") -> str:
    """根据 MIME 推导本地缓存文件扩展名。"""
    ext = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip().lower()) or fallback
    return ".jpg" if ext == ".jpe" else ext


def _assert_first_frame_meta(meta: dict[str, Any]) -> None:
    """校验本地首帧元数据是否满足 I2V 上传要求。"""
    size = int(meta.get("size") or 0)
    content_type = str(meta.get("content_type") or "")
    if size <= 0:
        raise HTTPException(status_code=400, detail="first frame image is empty")
    if size > MAX_FIRST_FRAME_BYTES:
        raise HTTPException(status_code=400, detail=f"first frame image exceeds {MAX_FIRST_FRAME_BYTES} bytes")
    if not _is_image_content_type(content_type):
        raise HTTPException(status_code=400, detail=f"first frame must be an image MIME type, got {content_type or 'unknown'}")


def _normalize_first_frame_source(body: dict[str, Any]) -> tuple[str, str] | None:
    """从兼容字段中提取唯一首帧来源。"""
    sources: list[tuple[str, str]] = []

    file_id = str(body.get("file_id") or "").strip()
    if file_id:
        sources.append(("file_id", file_id))

    image_url_value = body.get("image_url")
    if isinstance(image_url_value, dict):
        image_url_value = image_url_value.get("url")
    image_url = str(image_url_value or "").strip()
    if image_url:
        sources.append(("url", image_url))

    first_frame = body.get("first_frame")
    if isinstance(first_frame, str) and first_frame.strip():
        sources.append(("url", first_frame.strip()))
    elif isinstance(first_frame, dict):
        frame_file_id = str(first_frame.get("file_id") or "").strip()
        frame_url = str(first_frame.get("url") or first_frame.get("image_url") or "").strip()
        if frame_file_id and frame_url:
            raise HTTPException(status_code=400, detail="first_frame accepts either file_id or url, not both")
        if frame_file_id:
            sources.append(("file_id", frame_file_id))
        if frame_url:
            sources.append(("url", frame_url))
    elif first_frame not in (None, ""):
        raise HTTPException(status_code=400, detail="first_frame must be a string or an object with file_id/url")

    if len(sources) > 1:
        raise HTTPException(status_code=400, detail="Only one of file_id, image_url, first_frame can be provided")
    return sources[0] if sources else None


def _decode_first_frame_data_uri(data_uri: str) -> tuple[str, bytes]:
    """解析 data:image/...;base64,... 首帧图片。"""
    if not data_uri.startswith("data:") or "," not in data_uri:
        raise HTTPException(status_code=400, detail="Invalid data URI for first frame")
    header, encoded = data_uri.split(",", 1)
    content_type = header[5:].split(";", 1)[0].strip().lower()
    if ";base64" not in header.lower():
        raise HTTPException(status_code=400, detail="first frame data URI must be base64 encoded")
    if not _is_image_content_type(content_type):
        raise HTTPException(status_code=400, detail=f"first frame must be an image MIME type, got {content_type or 'unknown'}")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid first frame base64 data: {exc}") from exc
    if not raw:
        raise HTTPException(status_code=400, detail="first frame image is empty")
    if len(raw) > MAX_FIRST_FRAME_BYTES:
        raise HTTPException(status_code=400, detail=f"first frame image exceeds {MAX_FIRST_FRAME_BYTES} bytes")
    return content_type, raw


def _is_blocked_ip(ip_text: str) -> bool:
    """判断 URL 解析出的 IP 是否属于 SSRF 高风险地址。"""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    return not ip.is_global


async def _assert_public_http_url(url: str) -> None:
    """校验远程图片 URL，拒绝 localhost、私网和保留地址。"""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="image_url only supports http or https")
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="image_url host is required")
    host = parsed.hostname.strip().lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise HTTPException(status_code=400, detail="image_url host is not allowed")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail=f"image_url host cannot be resolved: {host}") from exc
    resolved = {item[4][0] for item in infos if item and item[4]}
    if not resolved or any(_is_blocked_ip(ip) for ip in resolved):
        raise HTTPException(status_code=400, detail="image_url resolves to a non-public address")


async def _download_first_frame_image(url: str) -> tuple[str, str, bytes]:
    """下载远程首帧图片，并在每次跳转前重新做 SSRF 校验。"""
    current_url = url.strip()
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, trust_env=False) as client:
        for redirect_count in range(MAX_FIRST_FRAME_REDIRECTS + 1):
            await _assert_public_http_url(current_url)
            async with client.stream("GET", current_url) as resp:
                if resp.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count >= MAX_FIRST_FRAME_REDIRECTS:
                        raise HTTPException(status_code=400, detail="image_url redirects too many times")
                    location = resp.headers.get("location")
                    if not location:
                        raise HTTPException(status_code=400, detail="image_url redirect missing Location")
                    current_url = urljoin(current_url, location)
                    continue
                if resp.status_code < 200 or resp.status_code >= 300:
                    raise HTTPException(status_code=400, detail=f"image_url download failed with HTTP {resp.status_code}")

                content_length = resp.headers.get("content-length")
                try:
                    declared_size = int(content_length) if content_length else 0
                except ValueError:
                    declared_size = 0
                if declared_size > MAX_FIRST_FRAME_BYTES:
                    raise HTTPException(status_code=400, detail=f"first frame image exceeds {MAX_FIRST_FRAME_BYTES} bytes")
                content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if not _is_image_content_type(content_type):
                    raise HTTPException(status_code=400, detail=f"first frame must be an image MIME type, got {content_type or 'unknown'}")

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_FIRST_FRAME_BYTES:
                        raise HTTPException(status_code=400, detail=f"first frame image exceeds {MAX_FIRST_FRAME_BYTES} bytes")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                if not raw:
                    raise HTTPException(status_code=400, detail="first frame image is empty")
                filename = Path(urlsplit(current_url).path).name or f"first-frame{_image_extension(content_type)}"
                if not Path(filename).suffix:
                    filename = f"{filename}{_image_extension(content_type)}"
                return filename, content_type, raw
    raise HTTPException(status_code=400, detail="image_url download failed")


async def _prepare_first_frame_file(app: Any, body: dict[str, Any], token: str) -> dict[str, Any] | None:
    """将 file_id、data URI 或远程 URL 统一转换成本地 file_store 元数据。"""
    source = _normalize_first_frame_source(body)
    if source is None:
        return None

    file_store = app.state.file_store
    kind, value = source
    if kind == "file_id":
        meta = await file_store.get(value)
        if meta is None:
            raise HTTPException(status_code=404, detail="first frame file_id not found")
        if meta.get("owner_token") and meta.get("owner_token") != token:
            raise HTTPException(status_code=403, detail="Forbidden first frame file_id")
        _assert_first_frame_meta(meta)
        return meta

    if value.startswith("data:"):
        content_type, raw = _decode_first_frame_data_uri(value)
        filename = f"first-frame{_image_extension(content_type)}"
        meta = await file_store.save_bytes(filename, content_type, raw, "vision", owner_token=token)
        _assert_first_frame_meta(meta)
        return meta

    filename, content_type, raw = await _download_first_frame_image(value)
    meta = await file_store.save_bytes(filename, content_type, raw, "vision", owner_token=token)
    _assert_first_frame_meta(meta)
    return meta


def _coerce_duration(value: Any) -> int:
    try:
        duration = int(value)
    except (TypeError, ValueError):
        duration = 5
    return min(max(duration, 1), 10)


def _build_video_prompt(prompt: str, *, size: str, ratio: str, duration: int) -> str:
    return (
        f"{prompt}\n\n"
        f"视频要求：生成 {duration} 秒视频，宽高比 {ratio}，参考画面尺寸 {size}。"
    )


def _is_async_video_requested(value: Any) -> bool:
    """解析视频接口 async 参数，默认保持同步行为不变。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _format_video_task_response(task: dict[str, Any]) -> dict[str, Any]:
    """将内部任务记录转换为公网查询响应。"""
    task_id = str(task.get("id") or "")
    response = {
        "id": task_id,
        "object": "video.generation.task",
        "status": task.get("status") or "queued",
        "created": task.get("created"),
        "updated": task.get("updated"),
        "model": task.get("model") or "",
        "mode": task.get("mode") or "t2v",
    }
    if task.get("started") is not None:
        response["started"] = task.get("started")
    if task.get("finished") is not None:
        response["finished"] = task.get("finished")
    if task.get("status") in {"queued", "running"}:
        response["poll_url"] = f"/v1/videos/tasks/{task_id}"
    if task.get("status") == "succeeded" and isinstance(task.get("result"), dict):
        response["data"] = task["result"].get("data") or []
    if task.get("error"):
        response["error"] = task.get("error")
    return response


async def _prepare_video_generation_params(app: Any, body: dict[str, Any], token: str) -> dict[str, Any]:
    """校验并归一化视频生成请求，异步任务只持久化安全字段。"""
    prompt: str = str(body.get("prompt", "")).strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")

    first_frame_meta = await _prepare_first_frame_file(app, body, token)
    generation_chat_type = "i2v" if first_frame_meta else "t2v"

    n = min(max(int(body.get("n", 1)), 1), 2)
    model = _resolve_video_model(body.get("model"), generation_chat_type=generation_chat_type)
    duration = _coerce_duration(body.get("duration"))
    size, ratio, width, height = _normalize_image_size(
        body.get("size") or body.get("ratio") or body.get("aspect_ratio")
    )

    return {
        "prompt": prompt,
        "n": n,
        "model": model,
        "duration": duration,
        "size": size,
        "ratio": ratio,
        "width": width,
        "height": height,
        "generation_chat_type": generation_chat_type,
        "first_frame_file_id": str(first_frame_meta.get("id") or "") if first_frame_meta else "",
    }


async def _poll_video_task(client: QwenClient, token: str, task_id: str, *, timeout_seconds: int = 420, log_prefix: str = "[T2V]") -> str:
    started = time.monotonic()
    interval = 10.0
    snapshots: list[str] = []
    last_status = ""

    while time.monotonic() - started < timeout_seconds:
        res = await client.get_vision_task_status(token, task_id, timeout=30.0)
        body_text = str(res.get("body") or "")
        snapshots.append(body_text)

        if int(res.get("status") or 0) != 200:
            log.warning("%s 任务状态查询 HTTP %s task_id=%s body=%r", log_prefix, res.get("status"), task_id, body_text[:300])
            await asyncio.sleep(interval)
            continue

        try:
            obj = json.loads(body_text)
        except Exception:
            obj = {}
        data = obj.get("data") if isinstance(obj, dict) and isinstance(obj.get("data"), dict) else {}
        status = str(
            (obj.get("task_status") if isinstance(obj, dict) else None)
            or (obj.get("status") if isinstance(obj, dict) else None)
            or data.get("task_status")
            or data.get("status")
            or ""
        ).lower()
        last_status = status or last_status

        if status in VIDEO_SUCCESS_STATUSES:
            log.info("%s 视频任务完成 task_id=%s elapsed=%.1fs", log_prefix, task_id, time.monotonic() - started)
            return "\n".join(snapshots)
        if status and status not in VIDEO_RUNNING_STATUSES:
            raise RuntimeError(f"Video task failed status={status} body={body_text[:500]}")
        if not status:
            log.info("%s 任务状态未识别 task_id=%s body=%r", log_prefix, task_id, body_text[:300])

        await asyncio.sleep(interval)

    raise RuntimeError(f"Video task timed out task_id={task_id} last_status={last_status or '-'}")


async def _collect_chat_detail_text(client: QwenClient, token: str, chat_id: str) -> str:
    res = await client.get_chat_detail(token, chat_id, timeout=30.0)
    if int(res.get("status") or 0) != 200:
        return ""
    return str(res.get("body") or "")


async def _create_video_with_account(
    client: QwenClient,
    token: str,
    *,
    model: str,
    prompt_text: str,
    video_options: dict,
    generation_chat_type: str = "t2v",
    files: list[dict] | None = None,
) -> tuple[str, list[str], str]:
    # Qwen Web 抓包显示 I2V 新建会话仍使用 t2v，真正生成请求再切到 i2v。
    create_chat_type = "t2v" if generation_chat_type == "i2v" else generation_chat_type
    chat_id = await client.create_chat(token, model, chat_type=create_chat_type, use_prewarmed=False)
    payload = client._build_payload(
        chat_id,
        model,
        prompt_text,
        has_custom_tools=False,
        files=files,
        chat_type=generation_chat_type,
        image_options=video_options,
    )
    payload["stream"] = False

    res = await client.post_chat_completion_once(token, chat_id, payload, timeout=90.0)
    body_text = str(res.get("body") or "")
    if int(res.get("status") or 0) != 200:
        raise RuntimeError(f"video completion HTTP {res.get('status')}: {body_text[:500]}")

    answer_text = body_text
    upstream_failure = _extract_upstream_failure(answer_text)
    if upstream_failure:
        raise RuntimeError(upstream_failure)

    video_urls = _extract_video_urls(answer_text)
    task_ids = _extract_task_ids(answer_text)
    log_prefix = "[I2V]" if generation_chat_type == "i2v" else "[T2V]"
    log.info("%s 非流式响应 chat_id=%s task_ids=%s video_urls=%s body_tail=%r", log_prefix, chat_id, task_ids, len(video_urls), body_text[-500:])

    if not video_urls and task_ids:
        answer_text += "\n" + await _poll_video_task(client, token, task_ids[0], log_prefix=log_prefix)
        video_urls = _extract_video_urls(answer_text)

    if not video_urls:
        detail_text = await _collect_chat_detail_text(client, token, chat_id)
        if detail_text:
            answer_text += "\n" + detail_text
            video_urls = _extract_video_urls(answer_text)

    return chat_id, video_urls, answer_text


async def _generate_video_data(app: Any, params: dict[str, Any]) -> dict[str, Any]:
    """执行一次同步视频生成，供直接请求和后台任务共同复用。"""
    from backend.core.config import settings

    client: QwenClient = app.state.qwen_client

    prompt = str(params.get("prompt") or "").strip()
    n = min(max(int(params.get("n") or 1), 1), 2)
    model = str(params.get("model") or DEFAULT_VIDEO_MODEL)
    duration = _coerce_duration(params.get("duration"))
    size = str(params.get("size") or "1328x1328")
    ratio = str(params.get("ratio") or "1:1")
    width = int(params.get("width") or 1328)
    height = int(params.get("height") or 1328)
    generation_chat_type = str(params.get("generation_chat_type") or "t2v")
    log_prefix = "[I2V]" if generation_chat_type == "i2v" else "[T2V]"
    video_options = {"size": size, "ratio": ratio, "width": width, "height": height, "duration": duration}

    first_frame_meta = None
    first_frame_file_id = str(params.get("first_frame_file_id") or "")
    if first_frame_file_id:
        first_frame_meta = await app.state.file_store.get(first_frame_file_id)
        if first_frame_meta is None:
            raise RuntimeError("first frame file_id is no longer available")
        _assert_first_frame_meta(first_frame_meta)

    log.info(
        "%s model=%s n=%s size=%s ratio=%s duration=%ss has_first_frame=%s prompt=%r",
        log_prefix,
        model,
        n,
        size,
        ratio,
        duration,
        bool(first_frame_meta),
        prompt[:80],
    )

    prompt_text = _build_video_prompt(prompt, size=size, ratio=ratio, duration=duration)
    exclude: set[str] = set()
    last_error: str | None = None

    for attempt in range(max(1, int(settings.MAX_RETRIES))):
        acc = None
        chat_id = None
        try:
            acc = await client.account_pool.acquire_wait(timeout=60, exclude=exclude)
            if acc is None:
                raise RuntimeError("No available accounts in pool (all busy or rate limited)")

            upstream_files: list[dict] = []
            if first_frame_meta:
                uploader = getattr(app.state, "upstream_file_uploader", None)
                if uploader is None:
                    raise RuntimeError("upstream_file_uploader is not initialized")
                uploaded = await uploader.upload_image_file(acc, first_frame_meta)
                upstream_files = [uploaded["remote_ref"]]
                log.info("[I2V] 首帧图片已上传 账号=%s file_id=%s size=%s", acc.email, uploaded.get("remote_file_id"), first_frame_meta.get("size"))

            log.info("%s 使用账号=%s 第%s/%s次", log_prefix, acc.email, attempt + 1, settings.MAX_RETRIES)
            chat_id, video_urls, answer_text = await _create_video_with_account(
                client,
                acc.token,
                model=model,
                prompt_text=prompt_text,
                video_options=video_options,
                generation_chat_type=generation_chat_type,
                files=upstream_files,
            )

            log.info("%s 提取到 %s 个视频 URL chat_id=%s answer_tail=%r", log_prefix, len(video_urls), chat_id, answer_text[-500:])
            if not video_urls:
                raise RuntimeError(f"Video generation produced no video URL (chat_id={chat_id})")

            data = [
                {
                    "url": url,
                    "revised_prompt": prompt,
                    "size": size,
                    "ratio": ratio,
                    "width": width,
                    "height": height,
                    "duration": duration,
                }
                for url in video_urls[:n]
            ]
            return {"created": int(time.time()), "data": data}

        except Exception as e:
            last_error = str(e)
            if acc is not None:
                exclude.add(acc.email)
            log.warning("%s 尝试失败 第%s/%s次 账号=%s 错误=%s", log_prefix, attempt + 1, settings.MAX_RETRIES, getattr(acc, "email", "-"), last_error)
        finally:
            if acc is not None:
                client.account_pool.release(acc)
                if chat_id:
                    client.delete_chat_background(acc.token, chat_id, source="video_cleanup")

    detail = f"All {settings.MAX_RETRIES} attempts failed. Last error: {last_error or 'unknown'}"
    log.error("%s 生成失败: %s", log_prefix, detail)
    raise RuntimeError(detail)


@router.post("/v1/videos/generations")
@router.post("/videos/generations")
async def create_video(request: Request):
    from backend.core.config import API_KEYS, settings

    token = _get_token(request)
    if API_KEYS:
        if token != settings.ADMIN_KEY and token not in API_KEYS:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    params = await _prepare_video_generation_params(request.app, body, token)
    if _is_async_video_requested(body.get("async")):
        store = getattr(request.app.state, "video_task_store", None)
        runner = getattr(request.app.state, "video_task_runner", None)
        if store is None or runner is None:
            raise HTTPException(status_code=500, detail="video task runner is not initialized")
        task = await store.create(hash_task_owner(token), params)
        await runner.enqueue(task["id"])
        return JSONResponse(_format_video_task_response(task))

    try:
        return JSONResponse(await _generate_video_data(request.app, params))
    except Exception as e:
        detail = str(e)
        if "Qwen upstream error" in detail:
            raise HTTPException(status_code=502, detail=detail)
        raise HTTPException(status_code=500, detail=detail)


@router.get("/v1/videos/tasks/{task_id}")
@router.get("/videos/tasks/{task_id}")
async def get_video_task(task_id: str, request: Request):
    from backend.core.config import API_KEYS, settings

    token = _get_token(request)
    if API_KEYS:
        if token != settings.ADMIN_KEY and token not in API_KEYS:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    store = getattr(request.app.state, "video_task_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="video task store is not initialized")

    task = await store.get_visible(task_id, hash_task_owner(token), is_admin=(token == settings.ADMIN_KEY))
    if not task:
        raise HTTPException(status_code=404, detail="video task not found")
    return JSONResponse(_format_video_task_response(task))
