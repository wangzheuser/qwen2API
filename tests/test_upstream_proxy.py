import asyncio
import sys
import types
import unittest

import httpx
from fastapi import HTTPException

from backend.core.account_pool import Account, AccountPool
from backend.core.config import settings
from backend.core.database import AsyncJsonDB
from backend.core.upstream_proxy import (
    clear_proxy_bindings,
    mark_proxy_failure,
    proxy_status,
    resolve_proxy,
    test_proxy_connectivity,
    to_playwright_proxy,
)


class ProxySettingsMixin:
    def setUp(self):
        self.old_values = {
            "QWEN_PROXY_ENABLED": settings.QWEN_PROXY_ENABLED,
            "QWEN_UPSTREAM_PROXY": settings.QWEN_UPSTREAM_PROXY,
        }
        clear_proxy_bindings()

    def tearDown(self):
        for key, value in self.old_values.items():
            setattr(settings, key, value)
        clear_proxy_bindings()


class UpstreamProxyTests(ProxySettingsMixin, unittest.TestCase):
    def test_static_proxy_resolves_without_binding(self):
        settings.QWEN_PROXY_ENABLED = True
        settings.QWEN_UPSTREAM_PROXY = "http://user:secret@127.0.0.1:7890"

        acc_a = Account(email="a@example.com", token="tok-a")
        acc_b = Account(email="b@example.com", token="tok-b")

        self.assertEqual(resolve_proxy(acc_a), "http://user:secret@127.0.0.1:7890")
        self.assertEqual(resolve_proxy(acc_b), "http://user:secret@127.0.0.1:7890")
        self.assertEqual(proxy_status()["bound_accounts"], 0)

    def test_uuid_template_binds_per_account_and_refreshes(self):
        settings.QWEN_PROXY_ENABLED = True
        settings.QWEN_UPSTREAM_PROXY = "http://node.{uuid}:pass@127.0.0.1:9200"

        acc_a = Account(email="a@example.com", token="tok-a")
        acc_b = Account(email="b@example.com", token="tok-b")

        first = resolve_proxy(acc_a)
        self.assertNotIn("{uuid}", first)
        self.assertRegex(first, r"node\.[0-9a-f]{32}:pass@")
        self.assertEqual(resolve_proxy(acc_a), first)

        second = resolve_proxy(acc_b)
        self.assertNotEqual(first, second)
        self.assertEqual(proxy_status()["bound_accounts"], 2)

        refreshed = resolve_proxy(acc_a, force_refresh=True)
        self.assertNotEqual(first, refreshed)

    def test_proxy_failure_clears_binding(self):
        settings.QWEN_PROXY_ENABLED = True
        settings.QWEN_UPSTREAM_PROXY = "http://node.{uuid}:pass@127.0.0.1:9200"

        acc = Account(email="a@example.com", token="tok")
        old_proxy = resolve_proxy(acc)
        before = proxy_status()["failures_total"]
        mark_proxy_failure(acc, old_proxy, RuntimeError("proxy failed"))
        new_proxy = resolve_proxy(acc)

        self.assertNotEqual(old_proxy, new_proxy)
        self.assertEqual(proxy_status()["failures_total"], before + 1)

    def test_proxy_status_drops_removed_fields(self):
        status = proxy_status()

        self.assertNotIn("masked_proxy", status)
        self.assertNotIn("bind_per_account", status)
        self.assertNotIn("failure_cooldown_seconds", status)

    def test_playwright_proxy_conversion(self):
        converted = to_playwright_proxy("http://node.abc:pass@127.0.0.1:9200")
        self.assertEqual(converted["server"], "http://127.0.0.1:9200")
        self.assertEqual(converted["username"], "node.abc")
        self.assertEqual(converted["password"], "pass")


class ProxyConnectivityTests(ProxySettingsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_connectivity_accepts_any_non_proxy_auth_status(self):
        from backend.core import upstream_proxy as proxy_module

        class FakeResponse:
            status_code = 403

        class FakeAsyncClient:
            created = []

            def __init__(self, **kwargs):
                self.proxy = kwargs.get("proxy")
                type(self).created.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc_info):
                return False

            async def get(self, url, **kwargs):
                self.url = url
                self.kwargs = kwargs
                return FakeResponse()

        old_async_client = proxy_module.httpx.AsyncClient
        proxy_module.httpx.AsyncClient = FakeAsyncClient
        try:
            ok, message = await test_proxy_connectivity("http://127.0.0.1:7890")
        finally:
            proxy_module.httpx.AsyncClient = old_async_client

        self.assertTrue(ok)
        self.assertIn("HTTP 403", message)
        self.assertEqual(FakeAsyncClient.created[0].proxy, "http://127.0.0.1:7890")

    async def test_connectivity_renders_uuid_template_for_test(self):
        from backend.core import upstream_proxy as proxy_module

        class FakeResponse:
            status_code = 200

        class FakeAsyncClient:
            created = []

            def __init__(self, **kwargs):
                self.proxy = kwargs.get("proxy")
                type(self).created.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc_info):
                return False

            async def get(self, *_args, **_kwargs):
                return FakeResponse()

        old_async_client = proxy_module.httpx.AsyncClient
        proxy_module.httpx.AsyncClient = FakeAsyncClient
        try:
            ok, _message = await test_proxy_connectivity("http://node.{uuid}:pass@127.0.0.1:9200")
        finally:
            proxy_module.httpx.AsyncClient = old_async_client

        self.assertTrue(ok)
        self.assertRegex(FakeAsyncClient.created[0].proxy, r"^http://node\.[0-9a-f]{32}:pass@127\.0\.0\.1:9200$")

    async def test_connectivity_rejects_proxy_auth_status(self):
        from backend.core import upstream_proxy as proxy_module

        class FakeResponse:
            status_code = 407

        class FakeAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc_info):
                return False

            async def get(self, *_args, **_kwargs):
                return FakeResponse()

        old_async_client = proxy_module.httpx.AsyncClient
        proxy_module.httpx.AsyncClient = FakeAsyncClient
        try:
            ok, message = await test_proxy_connectivity("http://127.0.0.1:7890")
        finally:
            proxy_module.httpx.AsyncClient = old_async_client

        self.assertFalse(ok)
        self.assertIn("407", message)

    async def test_connectivity_rejects_network_error(self):
        from backend.core import upstream_proxy as proxy_module

        class FakeAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc_info):
                return False

            async def get(self, *_args, **_kwargs):
                raise httpx.ProxyError("proxy connect failed")

        old_async_client = proxy_module.httpx.AsyncClient
        proxy_module.httpx.AsyncClient = FakeAsyncClient
        try:
            ok, message = await test_proxy_connectivity("http://127.0.0.1:7890")
        finally:
            proxy_module.httpx.AsyncClient = old_async_client

        self.assertFalse(ok)
        self.assertIn("ProxyError", message)


class AdminProxySettingsTests(ProxySettingsMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        from backend.api import admin as admin_module

        self.admin_module = admin_module
        self.old_test_proxy_connectivity = admin_module.test_proxy_connectivity

    def tearDown(self):
        self.admin_module.test_proxy_connectivity = self.old_test_proxy_connectivity
        super().tearDown()

    @staticmethod
    def _request(client=None):
        state = types.SimpleNamespace(qwen_client=client)
        return types.SimpleNamespace(app=types.SimpleNamespace(state=state))

    async def test_disabled_proxy_update_skips_connectivity_test(self):
        calls = []

        async def fake_test(proxy_template):
            calls.append(proxy_template)
            return True, "ok"

        self.admin_module.test_proxy_connectivity = fake_test
        settings.QWEN_PROXY_ENABLED = True
        settings.QWEN_UPSTREAM_PROXY = "http://old:7890"

        await self.admin_module.update_settings(
            {"qwen_proxy_enabled": False, "qwen_upstream_proxy": "http://new:7890"},
            self._request(),
        )

        self.assertEqual(calls, [])
        self.assertFalse(settings.QWEN_PROXY_ENABLED)
        self.assertEqual(settings.QWEN_UPSTREAM_PROXY, "http://new:7890")

    async def test_proxy_update_saves_only_after_successful_test(self):
        calls = []

        async def fake_test(proxy_template):
            calls.append(proxy_template)
            return True, "ok"

        class FakeClient:
            def __init__(self):
                self.reset_count = 0

            async def reset_proxy_runtime(self):
                self.reset_count += 1

        self.admin_module.test_proxy_connectivity = fake_test
        client = FakeClient()

        await self.admin_module.update_settings(
            {"qwen_proxy_enabled": True, "qwen_upstream_proxy": "http://ok:7890"},
            self._request(client),
        )

        self.assertEqual(calls, ["http://ok:7890"])
        self.assertTrue(settings.QWEN_PROXY_ENABLED)
        self.assertEqual(settings.QWEN_UPSTREAM_PROXY, "http://ok:7890")
        self.assertEqual(client.reset_count, 1)

    async def test_proxy_update_failure_keeps_old_config(self):
        async def fake_test(_proxy_template):
            return False, "代理测试失败"

        class FakeClient:
            async def reset_proxy_runtime(self):
                raise AssertionError("不应重置代理运行时")

        self.admin_module.test_proxy_connectivity = fake_test
        settings.QWEN_PROXY_ENABLED = True
        settings.QWEN_UPSTREAM_PROXY = "http://old:7890"

        with self.assertRaises(HTTPException) as ctx:
            await self.admin_module.update_settings(
                {"qwen_proxy_enabled": True, "qwen_upstream_proxy": "http://bad:7890"},
                self._request(FakeClient()),
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertTrue(settings.QWEN_PROXY_ENABLED)
        self.assertEqual(settings.QWEN_UPSTREAM_PROXY, "http://old:7890")

    async def test_settings_returns_plain_proxy_and_removed_fields_absent(self):
        settings.QWEN_PROXY_ENABLED = True
        settings.QWEN_UPSTREAM_PROXY = "http://user:secret@127.0.0.1:7890"
        request = self._request()
        request.app.state.config_db = AsyncJsonDB("/tmp/qwen2api-test-config.json", default_data={})
        request.app.state.chat_id_pool = None
        request.app.state.account_pool = None
        request.app.state.keepalive_service = None

        payload = await self.admin_module.get_settings(request)

        self.assertEqual(payload["qwen_upstream_proxy"], "http://user:secret@127.0.0.1:7890")
        self.assertNotIn("qwen_upstream_proxy_masked", payload)
        self.assertNotIn("qwen_proxy_pool_bind_per_account", payload)
        self.assertNotIn("qwen_proxy_failure_cooldown_seconds", payload)


class QwenClientProxyTests(ProxySettingsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_request_json_uses_resolved_proxy_and_rebinds_after_proxy_error(self):
        from backend.services import qwen_client as qwen_client_module
        from backend.services.qwen_client import QwenClient

        settings.QWEN_PROXY_ENABLED = True
        settings.QWEN_UPSTREAM_PROXY = "http://node.{uuid}:pass@127.0.0.1:9200"

        class FakeResponse:
            status_code = 200
            text = "{}"

        class FakeAsyncClient:
            created = []
            fail_next = False

            def __init__(self, **kwargs):
                self.proxy = kwargs.get("proxy")
                self.requests = []
                type(self).created.append(self)

            async def request(self, method, url, **kwargs):
                self.requests.append((method, url, kwargs))
                if self.proxy and type(self).fail_next:
                    type(self).fail_next = False
                    raise httpx.ProxyError("proxy connect failed")
                return FakeResponse()

            async def aclose(self):
                return None

        old_async_client = qwen_client_module.httpx.AsyncClient
        qwen_client_module.httpx.AsyncClient = FakeAsyncClient
        try:
            pool = AccountPool(AsyncJsonDB("/tmp/qwen2api-test-accounts.json", default_data=[]))
            acc = Account(email="a@example.com", token="tok")
            pool.accounts = [acc]
            pool._reset_concurrency_limits()

            client = QwenClient(pool)
            await client._request_json("GET", "/api/test", "tok")
            proxy_client = next(item for item in FakeAsyncClient.created if item.proxy)
            first_proxy = proxy_client.proxy
            self.assertIn("node.", first_proxy)
            self.assertNotIn("{uuid}", first_proxy)

            FakeAsyncClient.fail_next = True
            with self.assertRaises(httpx.ProxyError):
                await client._request_json("GET", "/api/test", "tok")
            self.assertNotEqual(resolve_proxy(acc), first_proxy)
            await client.aclose()
        finally:
            qwen_client_module.httpx.AsyncClient = old_async_client


class BrowserProxyTests(ProxySettingsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_new_browser_passes_playwright_proxy(self):
        from backend.core import browser_engine

        settings.QWEN_PROXY_ENABLED = True
        settings.QWEN_UPSTREAM_PROXY = "http://node.{uuid}:pass@127.0.0.1:9200"

        class FakeAsyncCamoufox:
            last_kwargs = None

            def __init__(self, **kwargs):
                type(self).last_kwargs = kwargs

            async def __aenter__(self):
                return object()

            async def __aexit__(self, *_exc_info):
                return False

        fake_api = types.ModuleType("camoufox.async_api")
        fake_api.AsyncCamoufox = FakeAsyncCamoufox
        fake_pkg = types.ModuleType("camoufox")
        old_pkg = sys.modules.get("camoufox")
        old_api = sys.modules.get("camoufox.async_api")
        sys.modules["camoufox"] = fake_pkg
        sys.modules["camoufox.async_api"] = fake_api
        try:
            async with browser_engine._new_browser(Account(email="a@example.com", token="tok")):
                await asyncio.sleep(0)
            proxy = FakeAsyncCamoufox.last_kwargs.get("proxy")
            self.assertEqual(proxy["server"], "http://127.0.0.1:9200")
            self.assertRegex(proxy["username"], r"node\.[0-9a-f]{32}")
            self.assertEqual(proxy["password"], "pass")
        finally:
            if old_pkg is None:
                sys.modules.pop("camoufox", None)
            else:
                sys.modules["camoufox"] = old_pkg
            if old_api is None:
                sys.modules.pop("camoufox.async_api", None)
            else:
                sys.modules["camoufox.async_api"] = old_api


if __name__ == "__main__":
    unittest.main()
