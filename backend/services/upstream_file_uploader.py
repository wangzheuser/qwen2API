from __future__ import annotations

import json
import mimetypes
import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import oss2


def _file_class_from_content_type(content_type: str) -> str:
    lowered = (content_type or "").lower()
    if lowered.startswith("image/"):
        return "image"
    if lowered.startswith("audio/"):
        return "audio"
    if lowered.startswith("video/"):
        return "video"
    return "document"


def _normalize_sign_region(region: str) -> str:
    region = (region or "").strip()
    if region.startswith("oss-"):
        return region[len("oss-"):]
    return region


class UpstreamFileUploader:
    def __init__(self, client, settings):
        self.client = client
        self.settings = settings

    async def _upload_to_oss(self, acc, *, filename: str, raw: bytes, content_type: str, filetype: str) -> dict[str, Any]:
        """申请 Qwen Web OSS 临时凭证并上传原始文件内容。"""
        sts_resp = await self.client._request_json(
            "POST",
            "/api/v2/files/getstsToken",
            acc.token,
            {
                "filename": filename,
                "filesize": len(raw),
                "filetype": filetype,
            },
            timeout=20.0,
        )
        if sts_resp.get("status") != 200:
            raise RuntimeError(f"getstsToken failed: {sts_resp.get('status')} {sts_resp.get('body', '')[:200]}")
        sts_data = json.loads(sts_resp.get("body", "{}"))
        sts = (sts_data.get("data") or {}) if isinstance(sts_data, dict) else {}
        file_id = sts.get("file_id")
        file_path_remote = sts.get("file_path", "")
        bucketname = sts.get("bucketname", "")
        endpoint = sts.get("endpoint", "")
        region = _normalize_sign_region(sts.get("region", ""))
        access_key_id = sts.get("access_key_id", "")
        access_key_secret = sts.get("access_key_secret", "")
        security_token = sts.get("security_token", "")
        if not file_id or not file_path_remote or not bucketname or not endpoint:
            raise RuntimeError(f"getstsToken missing file data: {sts_data}")

        # OSS Python SDK 是同步接口，放入线程避免阻塞 FastAPI 事件循环。
        auth = oss2.StsAuth(access_key_id, access_key_secret, security_token, auth_version='v4')
        bucket = oss2.Bucket(
            auth,
            f"https://{endpoint}",
            bucketname,
            region=region,
        )

        def _put_object():
            return bucket.put_object(
                file_path_remote,
                raw,
                headers={"Content-Type": content_type},
            )

        put_result = await asyncio.to_thread(_put_object)
        if getattr(put_result, 'status', None) not in (200, 201):
            raise RuntimeError(f"OSS put_object failed: status={getattr(put_result, 'status', None)}")

        user_id = file_path_remote.split('/', 1)[0] if '/' in file_path_remote else ""
        put_url = f"https://{bucketname}.{endpoint}/{file_path_remote.lstrip('/')}"
        return {
            "file_id": file_id,
            "file_path_remote": file_path_remote,
            "bucketname": bucketname,
            "endpoint": endpoint,
            "user_id": user_id,
            "url": put_url,
        }

    @staticmethod
    def _build_remote_ref(
        *,
        file_id: str,
        user_id: str,
        filename: str,
        content_type: str,
        size: int,
        url: str,
        item_type: str,
        show_type: str,
        file_class: str,
        progress: int,
        parse_status: str | None = None,
    ) -> dict[str, Any]:
        """构造 Qwen Web 消息 files[] 所需的远端文件引用结构。"""
        now_ms = int(time.time() * 1000)
        meta = {
            "name": filename,
            "size": size,
            "content_type": content_type,
        }
        if parse_status:
            meta["parse_meta"] = {"parse_status": parse_status}

        return {
            "type": item_type,
            "file": {
                "created_at": now_ms,
                "data": {},
                "filename": filename,
                "hash": None,
                "id": file_id,
                "user_id": user_id,
                "meta": meta,
                "update_at": now_ms,
                "lastModified": now_ms,
                "name": filename,
                "webkitRelativePath": "",
                "size": size,
                "type": content_type,
            },
            "id": file_id,
            "url": url,
            "name": filename,
            "collection_name": "",
            "progress": progress,
            "status": "uploaded",
            "greenNet": "success",
            "size": size,
            "error": "",
            "itemId": str(uuid.uuid4()),
            "file_type": content_type,
            "showType": show_type,
            "file_class": file_class,
            "uploadTaskId": str(uuid.uuid4()),
        }

    async def upload_local_file(self, acc, local_file_meta: dict[str, Any]) -> dict[str, Any]:
        """上传普通上下文文件到 Qwen Web，并等待服务端解析完成。"""
        filename = local_file_meta["filename"]
        file_path = local_file_meta["path"]
        content_type = local_file_meta.get("content_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        raw = Path(file_path).read_bytes()

        uploaded = await self._upload_to_oss(
            acc,
            filename=filename,
            raw=raw,
            content_type=content_type,
            filetype="file",
        )
        file_id = uploaded["file_id"]
        file_path_remote = uploaded["file_path_remote"]

        parse_resp = await self.client._request_json(
            "POST",
            "/api/v2/files/parse",
            acc.token,
            {"file_id": file_id},
            timeout=20.0,
        )
        if parse_resp.get("status") != 200:
            raise RuntimeError(f"files/parse failed: {parse_resp.get('status')} {parse_resp.get('body', '')[:200]}")

        deadline = time.time() + self.settings.CONTEXT_UPLOAD_PARSE_TIMEOUT_SECONDS
        parse_status = "pending"
        while time.time() < deadline:
            status_resp = await self.client._request_json(
                "POST",
                "/api/v2/files/parse/status",
                acc.token,
                {"file_id_list": [file_id]},
                timeout=20.0,
            )
            if status_resp.get("status") != 200:
                raise RuntimeError(f"files/parse/status failed: {status_resp.get('status')} {status_resp.get('body', '')[:200]}")
            status_data = json.loads(status_resp.get("body", "{}"))
            rows = status_data.get("data") or []
            row = rows[0] if isinstance(rows, list) and rows else {}
            parse_status = row.get("status", "pending")
            if parse_status == "success":
                break
            if parse_status in ("failed", "error"):
                raise RuntimeError(f"file parse failed: {row}")
            await asyncio.sleep(1.0)

        if parse_status != "success":
            raise RuntimeError(f"file parse timeout: {file_id}")

        remote_ref = self._build_remote_ref(
            file_id=file_id,
            user_id=uploaded["user_id"],
            filename=filename,
            content_type=content_type,
            size=len(raw),
            url=uploaded["url"],
            item_type="file",
            show_type="file",
            file_class=_file_class_from_content_type(content_type),
            progress=0,
            parse_status=parse_status,
        )
        return {
            "remote_file_id": file_id,
            "remote_object_key": file_path_remote,
            "filename": filename,
            "content_type": content_type,
            "parse_status": parse_status,
            "remote_ref": remote_ref,
        }

    async def upload_image_file(self, acc, local_file_meta: dict[str, Any]) -> dict[str, Any]:
        """上传 I2V 首帧图片到 Qwen Web OSS，跳过文档解析流程。"""
        filename = local_file_meta["filename"]
        file_path = local_file_meta["path"]
        content_type = local_file_meta.get("content_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        raw = Path(file_path).read_bytes()
        if not raw:
            raise RuntimeError("image file is empty")
        if not content_type.lower().startswith("image/"):
            raise RuntimeError(f"image upload requires image MIME type, got {content_type}")

        uploaded = await self._upload_to_oss(
            acc,
            filename=filename,
            raw=raw,
            content_type=content_type,
            filetype="image",
        )
        remote_ref = self._build_remote_ref(
            file_id=uploaded["file_id"],
            user_id=uploaded["user_id"],
            filename=filename,
            content_type=content_type,
            size=len(raw),
            url=uploaded["url"],
            item_type="image",
            show_type="image",
            file_class="vision",
            progress=100,
        )
        return {
            "remote_file_id": uploaded["file_id"],
            "remote_object_key": uploaded["file_path_remote"],
            "filename": filename,
            "content_type": content_type,
            "parse_status": "",
            "remote_ref": remote_ref,
        }

    async def delete_remote_file(self, acc, remote_meta: dict[str, Any]) -> bool:
        # Qwen web upload delete API has not been fully confirmed yet.
        return False
