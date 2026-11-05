import asyncio
import time
import unittest
import tempfile
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from telegram.error import BadRequest


class BehaviorTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == 'posix', 'Слоты CGI проверяются на Linux-сервере')
    async def test_download_slot_releases_after_completion(self):
        root = Path(__file__).parent / 'Temp'
        root.mkdir(exist_ok=True)
        entered = asyncio.Event()

        async def second():
            async with bot.download_slot():
                entered.set()

        with tempfile.TemporaryDirectory(dir=root) as folder:
            with patch.dict(os.environ, {'BOT_MODE': 'webhook'}), patch.object(bot, 'BASE_DIR', Path(folder)), \
                    patch.object(bot, 'MAX_CONCURRENT_DOWNLOADS', 1):
                async with bot.download_slot():
                    task = asyncio.create_task(second())
                    await asyncio.sleep(0.03)
                    self.assertFalse(entered.is_set())
                await asyncio.wait_for(task, 1)
                self.assertTrue(entered.is_set())

    async def test_membership_api_failure_denies_access(self):
        context = SimpleNamespace(bot=SimpleNamespace(get_chat_member=AsyncMock(side_effect=BadRequest('chat admin required'))))
        channel = bot.Channel('@test', 'https://t.me/test', '@test')
        self.assertFalse(await bot.is_channel_member(context, channel, 7))

    async def test_both_channels_checked_even_for_admin_with_old_cached_success(self):
        channels = bot.parse_channels('https://t.me/kursoviework, https://t.me/siskysindnysuiny')
        member = AsyncMock(side_effect=[SimpleNamespace(status='member'), SimpleNamespace(status='left')])
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7), effective_message=message)
        context = SimpleNamespace(bot=SimpleNamespace(get_chat_member=member),
                                  user_data={'sub_verified': True, 'sub_verified_until': time.time() + 300})
        with patch.object(bot, 'REQUIRED_CHANNELS', channels), patch.object(bot, 'ADMIN_USER_IDS', {7}), \
                patch.object(bot, 'SUBSCRIPTION_CACHE_SECONDS', 0):
            self.assertFalse(await bot.ensure_subscribed(update, context))
        self.assertEqual(member.await_count, 2)
        message.reply_text.assert_awaited_once()

    async def test_confirmed_subscription_to_both_channels_allows_access(self):
        channels = bot.parse_channels('@kursoviework, @siskysindnysuiny')
        member = AsyncMock(side_effect=[SimpleNamespace(status='member'), SimpleNamespace(status='administrator')])
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=7), effective_message=message)
        context = SimpleNamespace(bot=SimpleNamespace(get_chat_member=member), user_data={})
        with patch.object(bot, 'REQUIRED_CHANNELS', channels), patch.object(bot, 'SUBSCRIPTION_CACHE_SECONDS', 0):
            self.assertTrue(await bot.ensure_subscribed(update, context))
        self.assertEqual(member.await_count, 2)
        message.reply_text.assert_not_awaited()

    async def test_loading_animation_stops_without_overwriting_final_status(self):
        seen = []
        ready = asyncio.Event()

        async def edit(text):
            seen.append(text)
            if len(seen) == 3:
                ready.set()

        status = SimpleNamespace(edit_text=edit)
        async with bot.loading_animation(status, interval=0.001):
            await asyncio.wait_for(ready.wait(), 1)
        count = len(seen)
        await asyncio.sleep(0.01)
        self.assertEqual(len(seen), count)
        self.assertEqual(seen[:3], ['Загрузка.', 'Загрузка..', 'Загрузка...'])


if __name__ == '__main__':
    unittest.main()
