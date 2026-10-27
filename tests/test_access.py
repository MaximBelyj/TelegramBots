import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from dmr_bot.access import allowed_users, attach_users
from dmr_bot.preferences import Preferences


class AccessConfigTests(unittest.TestCase):
    def test_owner_default_and_strict_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'access.json'
            self.assertEqual(allowed_users(42, path), {42})
            path.write_text(json.dumps({'allowed_user_ids': [77, 77]}))
            self.assertEqual(allowed_users(42, path), {42, 77})
            for extra in ([True], [-1], ['77'], '*'):
                path.write_text(json.dumps({'allowed_user_ids': extra}))
                with self.assertRaises(ValueError):
                    allowed_users(42, path)


class MultiUserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'preferences.sqlite3'
        old = Preferences(self.path)
        old.add('Стилус владельца')
        old.db.close()
        self.bot = Bot('123456:' + 'a' * 35)
        self.bot.send_message = AsyncMock()
        self.bot.session.make_request = AsyncMock(return_value=True)
        self.dp = Dispatcher()
        self.prefs = attach_users(self.dp, self.bot, 42, self.path, {42, 77})
        self.sequence = 0

    async def asyncTearDown(self):
        await self.dp.emit_shutdown()
        for prefs in self.prefs.values():
            prefs.db.close()
        await self.bot.session.close()
        self.folder.cleanup()

    def message(self, user, text, chat=None):
        return Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=chat or user, type='private'),
                       from_user=User(id=user, first_name='Test', is_bot=False), text=text)

    async def text(self, user, text):
        self.sequence += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.sequence, message=self.message(user, text)))

    async def click(self, user, data, chat=None):
        self.sequence += 1
        callback = CallbackQuery(id=str(self.sequence), from_user=User(id=user, first_name='Test', is_bot=False),
                                 chat_instance='test', data=data, message=self.message(user, 'Menu', chat))
        await self.dp.feed_update(self.bot, Update(update_id=self.sequence, callback_query=callback))

    async def test_private_settings_and_unauthorized_account(self):
        await self.text(99, '/start')
        self.bot.send_message.assert_not_called()
        await self.text(77, '/start')
        self.assertEqual(self.bot.send_message.call_args.args[0], 77)
        self.assertNotIn('Стилус владельца', str(self.prefs[77].items()))
        await self.text(77, '➕ Добавить')
        await self.text(77, 'LM358')
        self.assertIn('LM358', [i['name'] for i in self.prefs[77].items()])
        self.assertNotIn('LM358', [i['name'] for i in self.prefs[42].items()])
        await self.click(77, 'source:platan')
        self.assertEqual(self.prefs[77].get('source'), 'platan')
        self.assertEqual(self.prefs[42].get('source'), 'all')

    async def test_callbacks_cannot_change_another_private_chat(self):
        await self.click(77, 'source:platan', chat=42)
        self.assertEqual(self.prefs[77].get('source'), 'all')
        self.assertEqual(self.prefs[42].get('source'), 'all')

    async def test_auto_stop_and_deletion_are_per_user(self):
        with patch('dmr_bot.bot.discover', new=AsyncMock(return_value=[])):
            await self.click(42, 'auto:on')
            await self.click(77, 'auto:on')
            await self.text(77, '⛔ Остановить поиск')
            self.assertEqual(self.prefs[77].get('auto'), 'off')
            self.assertEqual(self.prefs[42].get('auto'), 'on')
        primary_item = self.prefs[42].items()[0]['id']
        await self.click(77, f'delete:{primary_item}')
        self.assertIsNotNone(self.prefs[42].item(primary_item))
