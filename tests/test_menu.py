import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from dmr_bot.bot import create_router, render_report
from dmr_bot.preferences import Preferences
from dmr_bot.discovery import discover, relevant


class SettingsTests(unittest.TestCase):
    def test_only_settings_persist_and_deletion_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = folder + '/prefs.db'
            prefs = Preferences(path)
            self.assertEqual(prefs.get('auto'), 'off')
            self.assertTrue(prefs.add('  Стилус  '))
            self.assertFalse(prefs.add('стилус'))
            prefs.set('source', 'ozon')
            for item in prefs.items():
                prefs.remove(item['id'])
            prefs.db.close()
            prefs = Preferences(path)
            self.assertEqual(prefs.items(), [])
            self.assertEqual(prefs.get('source'), 'ozon')
            tables = {r[0] for r in prefs.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn('snapshots', tables)
            prefs.db.close()

    def test_non_radio_products_can_match(self):
        self.assertTrue(relevant('Стилус универсальный', 'Стилус'))
        self.assertFalse(relevant('Стилус другой', 'Стилус 123'))


class MenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.prefs = Preferences(self.folder.name + '/prefs.db')
        self.bot = Bot('123456:' + 'a' * 35)
        self.bot.send_message = AsyncMock()
        self.bot.session.make_request = AsyncMock(return_value=True)
        self.dp = Dispatcher()
        self.router = create_router(self.bot, 42, self.prefs)
        self.dp.include_router(self.router)
        self.sequence = 0

    async def asyncTearDown(self):
        await self.router.emit_shutdown()
        await self.bot.session.close()
        self.prefs.db.close()
        self.folder.cleanup()

    def message(self, text, owner=42):
        return Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=owner, type='private'), from_user=User(id=owner, is_bot=False, first_name='Owner'), text=text)

    async def text(self, text, owner=42):
        self.sequence += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.sequence, message=self.message(text, owner)))

    async def click(self, data):
        self.sequence += 1
        callback = CallbackQuery(id=str(self.sequence), from_user=User(id=42, is_bot=False, first_name='Owner'), chat_instance='test', message=self.message('Menu'), data=data)
        await self.dp.feed_update(self.bot, Update(update_id=self.sequence, callback_query=callback))

    async def test_add_remove_select_source_and_owner_access(self):
        await self.text('/start', owner=7)
        self.bot.send_message.assert_not_called()
        await self.text('/start')
        self.assertIn('Хранение результатов отключено', self.bot.send_message.call_args.args[1])
        await self.text('➕ Добавить')
        await self.text('Стилус\nСтилус')
        item = next(i for i in self.prefs.items() if i['name'] == 'Стилус')
        await self.click('source:ozon')
        self.assertEqual(self.prefs.get('source'), 'ozon')
        await self.click(f"item:delete:{item['id']}")
        self.assertIsNotNone(self.prefs.item(item['id']))
        await self.click(f"delete:{item['id']}")
        self.assertIsNone(self.prefs.item(item['id']))

    async def test_auto_toggle_and_stop(self):
        with patch('dmr_bot.bot.discover', new=AsyncMock(return_value=[])):
            await self.click('auto:on')
            self.assertEqual(self.prefs.get('auto'), 'on')
            await self.text('⛔ Остановить поиск')
            self.assertEqual(self.prefs.get('auto'), 'off')

    async def test_source_selection_used_and_report_does_not_claim_archival(self):
        fake = AsyncMock(return_value=[])
        with patch('dmr_bot.discovery.fetch_search', fake):
            reports = await discover('Стилус', ['ozon'])
        fake.assert_awaited_once_with('ozon', 'Стилус')
        self.assertEqual([r['source'] for r in reports], ['ozon'])
        self.assertIn('не сохраняются', render_report('Стилус', reports))

    async def test_new_source_in_existing_menu_and_future_item(self):
        import asyncio
        from dmr_bot.discovery import SOURCES
        await self.text('🏪 Площадки')
        markup = self.bot.send_message.call_args.kwargs['reply_markup']
        callbacks = {b.callback_data for row in markup.inline_keyboard for b in row}
        self.assertEqual(callbacks, {f'source:{key}' for key in SOURCES} | {'source:all'})
        await self.click('source:promelec')
        await self.text('➕ Добавить')
        await self.text('LM358')
        item = next(i for i in self.prefs.items() if i['name'] == 'LM358')
        fake = AsyncMock(return_value=[])
        with patch('dmr_bot.bot.discover', fake):
            await self.click(f"item:find:{item['id']}")
            await asyncio.sleep(0)
            fake.assert_awaited_once_with('LM358', ['promelec'])
        await self.text('⛔ Остановить поиск')

    async def test_access_status_and_diagnostics_stay_in_source_menu(self):
        from unittest.mock import Mock
        from dmr_bot.discovery import SOURCES
        engine = Mock()
        engine.state.return_value = {'status': 'manual'}
        engine.status_text.return_value = 'Только ручная ссылка: HTTP 403'
        dispatcher = Dispatcher()
        router = create_router(self.bot, 42, self.prefs, engine)
        dispatcher.include_router(router)
        try:
            await dispatcher.feed_update(self.bot, Update(update_id=100, message=self.message('🏪 Площадки')))
            args = self.bot.send_message.call_args
            self.assertIn('Только ручная ссылка', args.args[1])
            data = {button.callback_data for row in args.kwargs['reply_markup'].inline_keyboard for button in row}
            self.assertEqual(data, {f'source:{key}' for key in SOURCES} | {'source:all', 'health:check'})
        finally:
            await router.emit_shutdown()
