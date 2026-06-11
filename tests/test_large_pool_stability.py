import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path

from backend.core.config import settings
from backend.core.database import AsyncJsonDB
from backend.core.account_pool import Account, AccountPool
from backend.services.auth_resolver import AuthResolver
from backend.services.chat_id_pool import ChatIdPool


class BrowserLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_browser_respects_browser_pool_size(self):
        from backend.core import browser_engine

        class FakeAsyncCamoufox:
            active = 0
            max_active = 0

            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                type(self).active += 1
                type(self).max_active = max(type(self).max_active, type(self).active)
                await asyncio.sleep(0.01)
                return object()

            async def __aexit__(self, *_exc_info):
                type(self).active -= 1
                return False

        fake_api = types.ModuleType("camoufox.async_api")
        fake_api.AsyncCamoufox = FakeAsyncCamoufox
        fake_pkg = types.ModuleType("camoufox")
        sys.modules["camoufox"] = fake_pkg
        sys.modules["camoufox.async_api"] = fake_api

        old_size = settings.BROWSER_POOL_SIZE
        settings.BROWSER_POOL_SIZE = 3
        browser_engine._browser_semaphore = None
        browser_engine._browser_semaphore_limit = 0
        browser_engine._browser_active = 0
        browser_engine._browser_waiting = 0
        browser_engine._browser_launched_total = 0
        browser_engine._browser_failed_total = 0
        try:
            async def run_once():
                async with browser_engine._new_browser():
                    await asyncio.sleep(0.02)

            await asyncio.gather(*(run_once() for _ in range(50)))
            self.assertLessEqual(FakeAsyncCamoufox.max_active, 3)
            self.assertEqual(browser_engine.get_browser_metrics()["active"], 0)
            self.assertEqual(browser_engine.get_browser_metrics()["waiting"], 0)
        finally:
            settings.BROWSER_POOL_SIZE = old_size
            browser_engine._browser_semaphore = None


class AutoHealTests(unittest.TestCase):
    def test_auto_heal_disabled_by_default(self):
        pool = AccountPool(AsyncJsonDB(Path(tempfile.mkdtemp()) / "accounts.json", default_data=[]))
        resolver = AuthResolver(pool)
        acc = Account(email="a@example.com", token="tok", password="pwd")
        old_enabled = settings.AUTO_HEAL_ON_AUTH_FAILURE
        settings.AUTO_HEAL_ON_AUTH_FAILURE = False
        try:
            self.assertFalse(resolver.schedule_auto_heal(acc))
            self.assertFalse(acc.healing)
        finally:
            settings.AUTO_HEAL_ON_AUTH_FAILURE = old_enabled

    def test_auto_heal_skips_cooldown_and_missing_repair_material(self):
        pool = AccountPool(AsyncJsonDB(Path(tempfile.mkdtemp()) / "accounts.json", default_data=[]))
        resolver = AuthResolver(pool)
        old_enabled = settings.AUTO_HEAL_ON_AUTH_FAILURE
        settings.AUTO_HEAL_ON_AUTH_FAILURE = True
        try:
            no_password = Account(email="a@example.com", token="tok")
            self.assertFalse(resolver.schedule_auto_heal(no_password))

            cooled = Account(email="b@example.com", token="tok", password="pwd", heal_cooldown_until=9999999999)
            self.assertFalse(resolver.schedule_auto_heal(cooled))
        finally:
            settings.AUTO_HEAL_ON_AUTH_FAILURE = old_enabled


class LargePoolPrewarmTests(unittest.TestCase):
    def test_large_pool_disables_effective_prewarm_target(self):
        class FakeClient:
            pass

        pool = AccountPool(AsyncJsonDB(Path(tempfile.mkdtemp()) / "accounts.json", default_data=[]))
        pool.accounts = [Account(email=f"u{i}@example.com", token=f"tok{i}") for i in range(250)]
        client = FakeClient()
        client.account_pool = pool
        chat_pool = ChatIdPool(client, target_per_account=5)

        old_enabled = settings.CHAT_ID_PREWARM_LARGE_POOL_ENABLED
        old_threshold = settings.CHAT_ID_PREWARM_LARGE_POOL_THRESHOLD
        settings.CHAT_ID_PREWARM_LARGE_POOL_ENABLED = False
        settings.CHAT_ID_PREWARM_LARGE_POOL_THRESHOLD = 200
        try:
            self.assertTrue(chat_pool.is_large_pool_prewarm_suppressed())
            self.assertEqual(chat_pool.effective_target(), 0)
            settings.CHAT_ID_PREWARM_LARGE_POOL_ENABLED = True
            self.assertFalse(chat_pool.is_large_pool_prewarm_suppressed())
            self.assertEqual(chat_pool.effective_target(), 5)
        finally:
            settings.CHAT_ID_PREWARM_LARGE_POOL_ENABLED = old_enabled
            settings.CHAT_ID_PREWARM_LARGE_POOL_THRESHOLD = old_threshold


class StorageAndIndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_save_is_readable_and_token_index_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = AsyncJsonDB(Path(tmp) / "accounts.json", default_data=[])
            await db.save([{"email": "a@example.com", "token": "old"}])
            data = await db.load()
            self.assertEqual(data[0]["token"], "old")

            pool = AccountPool(db)
            await pool.load()
            acc = pool.get_by_token("old")
            self.assertIsNotNone(acc)
            acc.token = "new"
            pool.reindex_account(acc)
            self.assertIs(pool.get_by_token("new"), acc)
            self.assertIsNone(pool.get_by_token("old"))


if __name__ == "__main__":
    unittest.main()
