import asyncio
import sys
import types
import unittest

import httpx

from backend.core.account_pool import Account, AccountPool
from backend.core.config import settings
from backend.core.database import AsyncJsonDB
from backend.core.upstream_proxy import (
    clear_proxy_bindings,
    mark_proxy_failure,
    mask_proxy_url,
    proxy_status,
    resolve_proxy,
    to_playwright_proxy,
)


class ProxySettingsMixin:
    def setUp(self):
        self.old_values = {
            "QWEN_PROXY_ENABLED": settings.QWEN_PROXY_ENABLED,
            "QWEN_UPSTREAM_PROXY": settings.QWEN_UPSTREAM_PROXY,
            "QWEN_PROXY_POOL_BIND_PER_ACCOUNT": settings.QWEN_PROXY_POOL_BIND_PER_ACCOUNT,
            "QWEN_PROXY_FAILURE_COOLDOWN_SECONDS": settings.QWEN_PROXY_FAILURE_COOLDOWN_SECONDS,
        }
        clear_proxy_bindings()

    def tearDown(self):
        for key, value in self.old_values.items():
            setattr(settings, key, value)
        clear_proxy_bindings()


class UpstreamProxyTests(ProxySettingsMixin, unittest.TestCase):
    def test_static_proxy_and_mask(self):
        settings.QWEN_PROXY_ENABLED = True
        settings.QWEN_UPSTREAM_PROXY = "http://user:secret@127.0.0.1:7890"

        self.assertEqual(resolve_proxy(), "http://user:secret@127.0.0.1:7890")
        self.assertEqual(mask_proxy_url(settings.QWEN_UPSTREAM_PROXY), "http://us***:***@127.0.0.1:7890")

    def test_uuid_template_binds_per_account_and_refreshes(self):
        settings.QWEN_PROXY_ENABLED = True
        settings.QWEN_UPSTREAM_PROXY = "http://node.{uuid}:pass@127.0.0.1:9200"
        settings.QWEN_PROXY_POOL_BIND_PER_ACCOUNT = True

        acc_a = Account(email="a@example.com", token="tok-a")
        acc_b = Account(email="b@example.com", token="tok-b")

        first = resolve_proxy(acc_a)
        self.assertNotIn("{uuid}", first)
        self.assertRegex(first, r"node\.[0-9a-f]{32}:pass@")
        self.assertEqual(resolve_proxy(acc_a), first)

        second = resolve_proxy(acc_b)
        self.assertNotEqual(first, second)

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

    def test_playwright_proxy_conversion(self):
        converted = to_playwright_proxy("http://node.abc:pass@127.0.0.1:9200")
        self.assertEqual(converted["server"], "http://127.0.0.1:9200")
        self.assertEqual(converted["username"], "node.abc")
        self.assertEqual(converted["password"], "pass")


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
