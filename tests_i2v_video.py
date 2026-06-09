import unittest
import base64
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.core.config as config
from backend.api import videos
from backend.api.videos import _normalize_first_frame_source, _resolve_video_model
from backend.core.database import AsyncJsonDB
from backend.services.file_store import LocalFileStore
from backend.services.upstream_file_uploader import UpstreamFileUploader
from backend.services.video_task_store import VideoTaskStore, hash_task_owner
from backend.upstream.payload_builder import build_chat_payload


class I2VPayloadBuilderTest(unittest.TestCase):
    """校验视频 payload 在 T2V 兼容和 I2V 新链路下的关键字段。"""

    def test_t2v_payload_keeps_existing_shape(self):
        payload = build_chat_payload(
            "chat-id",
            "qwen3.6-plus",
            "生成一个视频",
            chat_type="t2v",
            image_options={"ratio": "16:9"},
        )

        message = payload["messages"][0]
        self.assertEqual("t2v", message["chat_type"])
        self.assertEqual("t2v", message["sub_chat_type"])
        self.assertEqual("video_generation", message["extra"]["meta"]["mode"])
        self.assertEqual("16:9", message["extra"]["meta"]["size"])
        self.assertTrue(message["feature_config"]["video_generation"])
        self.assertEqual("16:9", payload["size"])

    def test_i2v_payload_matches_captured_web_shape(self):
        files = [{"type": "image", "id": "first-frame-id", "file_class": "vision"}]
        payload = build_chat_payload(
            "chat-id",
            "qwen3.7-plus",
            "让首帧动起来",
            files=files,
            chat_type="i2v",
            image_options={"ratio": "16:9"},
        )

        message = payload["messages"][0]
        self.assertEqual("i2v", message["chat_type"])
        self.assertEqual("i2v", message["sub_chat_type"])
        self.assertEqual(files, message["files"])
        self.assertEqual({"subChatType": "i2v", "size": "16:9"}, message["extra"]["meta"])
        self.assertFalse(message["feature_config"]["thinking_enabled"])
        self.assertEqual("Fast", message["feature_config"]["thinking_mode"])
        self.assertTrue(message["feature_config"]["auto_search"])
        self.assertEqual("16:9", payload["size"])


class FirstFrameSourceTest(unittest.TestCase):
    """校验首帧入口参数的互斥和兼容解析规则。"""

    def test_no_first_frame_uses_t2v(self):
        self.assertIsNone(_normalize_first_frame_source({}))

    def test_file_id_source(self):
        self.assertEqual(("file_id", "abc"), _normalize_first_frame_source({"file_id": "abc"}))

    def test_image_url_source(self):
        self.assertEqual(("url", "https://example.com/a.png"), _normalize_first_frame_source({"image_url": "https://example.com/a.png"}))

    def test_first_frame_object_file_id(self):
        self.assertEqual(("file_id", "abc"), _normalize_first_frame_source({"first_frame": {"file_id": "abc"}}))

    def test_multiple_sources_are_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            _normalize_first_frame_source({"file_id": "abc", "image_url": "https://example.com/a.png"})
        self.assertEqual(400, ctx.exception.status_code)


class I2VModelResolutionTest(unittest.TestCase):
    """校验实测可用的 I2V 视频模型能解析为上游基础模型。"""

    def test_verified_i2v_models_resolve_to_base_models(self):
        cases = {
            "qwen-i2v": "qwen3.7-plus",
            "qwen3.7-plus-video": "qwen3.7-plus",
            "qwen3.7-max-video": "qwen3.7-max",
            "qwen3.6-plus-video": "qwen3.6-plus",
            "qwen3.6-27b-video": "qwen3.6-27b",
            "qwen3.5-plus-video": "qwen3.5-plus",
            "qwen3.6-35b-a3b-video": "qwen3.6-35b-a3b",
            "qwen3.5-flash-video": "qwen3.5-flash",
            "qwen3.5-397b-a17b-video": "qwen3.5-397b-a17b",
            "qwen3.5-122b-a10b-video": "qwen3.5-122b-a10b",
            "qwen3.5-27b-video": "qwen3.5-27b",
            "qwen3.5-35b-a3b-video": "qwen3.5-35b-a3b",
            "qwen3-max-2026-01-23-video": "qwen3-max-2026-01-23",
            "qwen-plus-2025-07-28-video": "qwen-plus-2025-07-28",
            "qwen3-coder-plus-video": "qwen3-coder-plus",
            "qwen3-vl-plus-video": "qwen3-vl-plus",
            "qwen3-omni-flash-2025-12-01-video": "qwen3-omni-flash-2025-12-01",
        }

        for requested, expected in cases.items():
            with self.subTest(requested=requested):
                self.assertEqual(expected, _resolve_video_model(requested, generation_chat_type="i2v"))


class UpstreamImageRefTest(unittest.TestCase):
    """校验 I2V 首帧 remote_ref 符合 Qwen Web 图片文件形态。"""

    def test_image_remote_ref_shape(self):
        ref = UpstreamFileUploader._build_remote_ref(
            file_id="remote-id",
            user_id="user-id",
            filename="first-frame.png",
            content_type="image/png",
            size=1024,
            url="https://qwen-webui-prod.oss-accelerate.aliyuncs.com/user-id/remote-id_first-frame.png",
            item_type="image",
            show_type="image",
            file_class="vision",
            progress=100,
        )

        self.assertEqual("image", ref["type"])
        self.assertEqual("image", ref["showType"])
        self.assertEqual("vision", ref["file_class"])
        self.assertEqual(100, ref["progress"])
        self.assertEqual("image/png", ref["file_type"])
        self.assertEqual("uploaded", ref["status"])


class VideoAsyncTaskTest(unittest.IsolatedAsyncioTestCase):
    """校验视频异步任务的创建、归属和安全持久化规则。"""

    async def test_store_hides_task_from_other_api_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = VideoTaskStore(AsyncJsonDB(Path(tmpdir) / "video_tasks.json"), ttl_seconds=3600)
            await store.load()
            task = await store.create(hash_task_owner("owner-key"), {"prompt": "生成视频", "model": "qwen3.6-plus", "generation_chat_type": "t2v"})

            self.assertIsNotNone(await store.get_visible(task["id"], hash_task_owner("owner-key")))
            self.assertIsNone(await store.get_visible(task["id"], hash_task_owner("other-key")))
            self.assertIsNotNone(await store.get_visible(task["id"], hash_task_owner("other-key"), is_admin=True))

    async def test_data_uri_first_frame_is_normalized_to_file_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_store = LocalFileStore(tmpdir)
            app = SimpleNamespace(state=SimpleNamespace(file_store=file_store))
            data_uri = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nframe").decode("ascii")

            params = await videos._prepare_video_generation_params(
                app,
                {
                    "model": "qwen-i2v",
                    "prompt": "让首帧动起来",
                    "first_frame": data_uri,
                    "ratio": "16:9",
                    "duration": 5,
                },
                "owner-key",
            )

            self.assertEqual("i2v", params["generation_chat_type"])
            self.assertTrue(params["first_frame_file_id"])
            self.assertNotIn("first_frame", params)
            self.assertNotIn(data_uri, str(params))


class VideoRouteAsyncTest(unittest.TestCase):
    """校验视频接口同步兼容和 async=true 创建任务响应。"""

    def _build_client(self):
        app = FastAPI()
        app.include_router(videos.router)
        tmpdir = tempfile.TemporaryDirectory()
        store = VideoTaskStore(AsyncJsonDB(Path(tmpdir.name) / "video_tasks.json"), ttl_seconds=3600)

        class FakeRunner:
            def __init__(self):
                self.enqueued: list[str] = []

            async def enqueue(self, task_id: str):
                self.enqueued.append(task_id)

        app.state.file_store = LocalFileStore(tmpdir.name)
        app.state.video_task_store = store
        app.state.video_task_runner = FakeRunner()
        return TestClient(app), tmpdir, app

    def test_sync_request_keeps_existing_response_shape(self):
        client, tmpdir, _ = self._build_client()
        try:
            expected = {"created": 1, "data": [{"url": "https://example.com/a.mp4"}]}
            with patch.object(config, "API_KEYS", set()), patch("backend.api.videos._generate_video_data", AsyncMock(return_value=expected)):
                resp = client.post("/v1/videos/generations", json={"prompt": "生成视频"})
            self.assertEqual(200, resp.status_code)
            self.assertEqual(expected, resp.json())
        finally:
            tmpdir.cleanup()

    def test_async_true_returns_task_and_enqueues_runner(self):
        client, tmpdir, app = self._build_client()
        try:
            with patch.object(config, "API_KEYS", set()):
                resp = client.post("/v1/videos/generations", json={"prompt": "生成视频", "async": True})

            body = resp.json()
            self.assertEqual(200, resp.status_code)
            self.assertTrue(body["id"].startswith("video_task_"))
            self.assertEqual("queued", body["status"])
            self.assertEqual(f"/v1/videos/tasks/{body['id']}", body["poll_url"])
            self.assertEqual([body["id"]], app.state.video_task_runner.enqueued)
        finally:
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
