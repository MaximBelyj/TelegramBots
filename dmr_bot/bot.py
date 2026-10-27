import asyncio
import logging
import os
from pathlib import Path

from aiohttp import ClientSession, TCPConnector
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from .discovery import SOURCES, discover, search_url
from .preferences import Preferences

MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text='🔎 Найти сейчас'), KeyboardButton(text='📋 Изделия')],
    [KeyboardButton(text='➕ Добавить'), KeyboardButton(text='➖ Удалить')],
    [KeyboardButton(text='🏪 Площадки'), KeyboardButton(text='⏱ Автопоиск')],
    [KeyboardButton(text='⛔ Остановить поиск')]], resize_keyboard=True)


class EnvironmentSession(AiohttpSession):
    async def create_session(self):
        if self._session is None or self._session.closed:
            self._session = ClientSession(trust_env=True, connector=TCPConnector())
        return self._session


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows])


def render_report(query, reports):
    lines = [f'Поиск: {query}. Вся Россия, без ограничения цены.']
    for report in reports:
        source = report['source']
        lines.append(SOURCES[source][0])
        if report.get('warning'):
            lines.append('Часть запросов недоступна: ' + report['warning'])
        if report['error']:
            lines.extend([report['error'], search_url(source, query)])
        else:
            lines.append(f"Найдено: {len(report['items'])} (первая доступная страница).")
            for url, item in report['items']:
                price = item.get('price')
                price_text = f" — {price} {item.get('currency') or ''}" if price is not None else ''
                match = item.get('match')
                reason = ('\nСовпало: ' + ', '.join(match['matched']) + f". Баллы: {match['score']:g}; покрытие признаков: {match['coverage']}% (не вероятность).") if match else ''
                lines.append(f"{str(item.get('title', 'Товар'))[:200]}{price_text}{reason}\n{url}")
    lines.append('Результаты не сохраняются в базе бота. Алгоритм поиска пока тестируется.')
    return '\n\n'.join(lines)


def create_router(bot, owner, prefs):
    router = Router()
    router.message.filter(F.from_user.id == owner, F.chat.type == 'private')
    router.callback_query.filter(F.from_user.id == owner, F.message.chat.type == 'private')
    pending = set()
    lock = asyncio.Lock()
    tasks = {'manual': None, 'auto': None}

    async def send(text, markup=None):
        for start in range(0, len(text), 3800):
            await bot.send_message(owner, text[start:start + 3800], reply_markup=markup)

    def source_label():
        source = prefs.get('source')
        return 'Все площадки' if source == 'all' else SOURCES[source][0]

    async def execute(queries, automatic=False):
        async with lock:
            for query in queries:
                # Re-read scope each time so settings changes affect the next item.
                source = prefs.get('source')
                reports = await discover(query, None if source == 'all' else [source])
                await send(('Автопоиск\n' if automatic else '') + render_report(query, reports))
                await asyncio.sleep(3)

    async def manual(queries):
        try:
            await execute(queries)
            await send('Поиск завершён.', MENU)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.warning('Search failed: %s', type(exc).__name__)
            await send('Не удалось завершить поиск. Попробуйте ещё раз.', MENU)

    async def start_search(queries):
        if not queries:
            await send('Список пуст. Нажмите «➕ Добавить».', MENU)
            return
        if lock.locked() or (tasks['manual'] and not tasks['manual'].done()):
            await send('Поиск уже идёт. Можно остановить его кнопкой в меню.', MENU)
            return
        await send(f'Начинаю поиск: {len(queries)} изделий. Площадки: {source_label()}.', MENU)
        tasks['manual'] = asyncio.create_task(manual(queries))

    async def auto_loop():
        while prefs.get('auto') == 'on':
            try:
                queries = [item['name'] for item in prefs.items()]
                if queries:
                    await execute(queries, automatic=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.warning('Auto search failed: %s', type(exc).__name__)
            await asyncio.sleep(1800)

    async def stop_task(key):
        task = tasks[key]
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        tasks[key] = None

    async def set_auto(enabled):
        prefs.set('auto', 'on' if enabled else 'off')
        await stop_task('auto')
        if enabled:
            tasks['auto'] = asyncio.create_task(auto_loop())

    async def item_menu(mode='view', page=0):
        items = prefs.items()
        pages = max(1, (len(items) + 7) // 8)
        page = min(max(page, 0), pages - 1)
        rows = []
        if mode == 'find' and items:
            rows.append([('Искать все изделия', 'findall')])
        for item in items[page * 8:page * 8 + 8]:
            rows.append([(item['name'][:55], f"item:{mode}:{item['id']}")])
        nav = []
        if page:
            nav.append(('← Назад', f'page:{mode}:{page - 1}'))
        if page < pages - 1:
            nav.append(('Далее →', f'page:{mode}:{page + 1}'))
        if nav:
            rows.append(nav)
        title = {'view': 'Список изделий', 'find': 'Выберите изделие для поиска', 'delete': 'Выберите изделие для удаления'}[mode]
        await send(f'{title}: {len(items)}. Страница {page + 1}/{pages}.\n' + ('Список пуст. Нажмите «➕ Добавить».' if not items else ''), keyboard(rows) if rows else MENU)

    @router.message(Command('start', 'help', 'menu'))
    async def home(message: Message):
        pending.discard(owner)
        await send(f'Поиск товаров\nИзделий: {len(prefs.items())}\nПлощадки: {source_label()}\nАвтопоиск: {"включён" if prefs.get("auto") == "on" else "выключен"}\nХранение результатов отключено. Сохраняются только список изделий и настройки.\nИспользуйте кнопки меню.', MENU)

    @router.message(Command('find', 'search'))
    async def command_find(message: Message):
        pending.discard(owner)
        parts = (message.text or '').split(maxsplit=1)
        if len(parts) > 1:
            query = ' '.join(parts[1].split())
            if not 2 <= len(query) <= 200:
                await send('Введите запрос длиной от 2 до 200 символов.')
                return
            await start_search([query])
        else:
            await item_menu('find')

    @router.message(Command('save', 'history', 'track', 'untrack', 'unwatch'))
    async def old_commands(message: Message):
        await send('Прежние команды архива отключены. Используйте /menu для управления новым поиском.', MENU)

    @router.callback_query()
    async def callback(call: CallbackQuery):
        await call.answer()
        pending.discard(owner)
        data = call.data or ''
        if data.startswith('source:'):
            source = data.split(':', 1)[1]
            if source in (*SOURCES, 'all'):
                prefs.set('source', source)
                await send(f'Место поиска: {source_label()}.', MENU)
        elif data in ('auto:on', 'auto:off'):
            if data == 'auto:on' and not prefs.items():
                await send('Сначала добавьте хотя бы одно изделие.', MENU)
                return
            await set_auto(data == 'auto:on')
            await send('Автопоиск включён: первый запуск сейчас, затем через 30 минут после завершения цикла.' if data == 'auto:on' else 'Автопоиск выключен, текущий автоматический поиск остановлен.', MENU)
        elif data == 'findall':
            await start_search([item['name'] for item in prefs.items()])
        elif data.startswith('page:'):
            _, mode, page = data.split(':')
            if mode in ('view', 'find', 'delete') and page.isdigit():
                await item_menu(mode, int(page))
        elif data.startswith('item:'):
            _, mode, item_id = data.split(':')
            item = prefs.item(int(item_id)) if item_id.isdigit() else None
            if not item:
                await send('Изделие уже удалено. Откройте список заново.')
            elif mode == 'delete':
                await send(f"Удалить «{item['name']}»?", keyboard([[('Да, удалить', f"delete:{item['id']}"), ('Отмена', 'page:view:0')]]))
            elif mode == 'find':
                await start_search([item['name']])
            else:
                await send(item['name'], keyboard([[('Найти', f"item:find:{item['id']}"), ('Удалить', f"item:delete:{item['id']}")]]))
        elif data.startswith('delete:'):
            item_id = data.split(':', 1)[1]
            if item_id.isdigit():
                prefs.remove(int(item_id))
                await stop_task('manual')
                # Stop queued automatic work containing the deleted item.
                if prefs.get('auto') == 'on':
                    await set_auto(bool(prefs.items()))
                await send('Изделие удалено.', MENU)
                await item_menu('delete')

    @router.message()
    async def menu_message(message: Message):
        text = message.text or ''
        if text in ('📋 Изделия', '🔎 Найти сейчас', '➖ Удалить'):
            pending.discard(owner)
            await item_menu({'📋 Изделия': 'view', '🔎 Найти сейчас': 'find', '➖ Удалить': 'delete'}[text])
        elif text == '➕ Добавить':
            pending.add(owner)
            await send('Отправьте название изделия. Можно несколько названий, каждое с новой строки. Для отмены: /menu.', MENU)
        elif text == '🏪 Площадки':
            pending.discard(owner)
            rows = [[(name, f'source:{key}')] for key, (name, _) in SOURCES.items()]
            rows.append([('Все сразу', 'source:all')])
            await send(f'Сейчас: {source_label()}. Выберите место поиска:', keyboard(rows))
        elif text == '⏱ Автопоиск':
            pending.discard(owner)
            await send(f'Автопоиск {"включён" if prefs.get("auto") == "on" else "выключен"}. При включении: сразу, затем пауза 30 минут после каждого полного цикла. Повторные результаты могут приходить снова, история не хранится.', keyboard([[('Включить', 'auto:on'), ('Выключить', 'auto:off')]]))
        elif text == '⛔ Остановить поиск':
            pending.discard(owner)
            await set_auto(False)
            await stop_task('manual')
            await send('Все текущие поиски остановлены. Автопоиск выключен.', MENU)
        elif owner in pending and text:
            added = duplicates = 0
            errors = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    if prefs.add(line):
                        added += 1
                    else:
                        duplicates += 1
                except ValueError as exc:
                    errors.append(str(exc))
            pending.discard(owner)
            await send(f'Добавлено: {added}. Уже были в списке: {duplicates}. Ошибок: {len(errors)}.' + ('\n' + errors[0] if errors else ''), MENU)
        else:
            await send('Выберите действие в меню. Добавление изделия: «➕ Добавить».', MENU)

    async def startup():
        if prefs.get('auto') == 'on':
            tasks['auto'] = asyncio.create_task(auto_loop())

    async def shutdown():
        await stop_task('auto')
        await stop_task('manual')

    router.startup.register(startup)
    router.shutdown.register(shutdown)
    return router


async def run():
    owner = int(os.environ['TELEGRAM_OWNER_ID'])
    path = Path(os.getenv('DMR_SETTINGS_PATH', 'data/preferences.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    prefs = Preferences(path)
    bot = Bot(os.environ['TELEGRAM_BOT_TOKEN'], session=EnvironmentSession())
    dp = Dispatcher()
    dp.include_router(create_router(bot, owner, prefs))
    try:
        await dp.start_polling(bot)
    finally:
        prefs.db.close()
        await bot.session.close()


def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == '__main__':
    main()
