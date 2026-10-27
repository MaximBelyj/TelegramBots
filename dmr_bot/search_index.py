"""Official Brave Web Search API for indexed product links, not live marketplace inventory."""
import asyncio
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import httpx
from .discovery import SourceUnavailable, canonical_url

INDEX_SOURCES = {'ozon': ('ozon.ru', r'^/product/[^/]+'),
                 'wb': ('wildberries.ru', r'^/catalog/[0-9]+/detail\.aspx$')}
API_URL = 'https://api.search.brave.com/res/v1/web/search'


def parse_index_results(source, payload):
    if not isinstance(payload, dict) or payload.get('type') not in ('search', None):
        raise SourceUnavailable('Поисковый API вернул неподдерживаемый ответ', 'index_layout')
    web = payload.get('web')
    if web is None and payload.get('type') == 'search':
        return []
    if not isinstance(web, dict) or not isinstance(web.get('results'), list):
        raise SourceUnavailable('В ответе API нет структуры поисковой выдачи', 'index_layout')
    host, pattern = INDEX_SOURCES[source]
    items = {}
    for row in web['results']:
        if not isinstance(row, dict) or not isinstance(row.get('url'), str) or not isinstance(row.get('title'), str):
            continue
        p = urlsplit(row['url'])
        if (p.hostname or '').removeprefix('www.') != host or not re.search(pattern, p.path):
            continue
        try:
            url = canonical_url(row['url'])
        except ValueError:
            continue
        title = BeautifulSoup(row['title'], 'html.parser').get_text(' ', strip=True)
        if not title:
            continue
        items[url] = {'title': title, 'description': None, 'price': None, 'currency': None,
                      'seller': None, 'published_at': None, 'images': [], 'source': source,
                      'capture_kind': 'search_index', 'retrieval': 'brave_index',
                      'provider': 'Brave Search', 'structured_data': {}}
    return list(items.items())[:20]


class SearchIndex:
    def __init__(self, key, *, clock=time.time, sleeper=asyncio.sleep, usage_path=None, daily_limit=100):
        self.key, self.clock, self.sleep = key, clock, sleeper
        self.lock = asyncio.Lock()
        self.last = -1e20
        self.retry_at = 0
        self.retry_code = 'http_429'
        self.usage_path = Path(usage_path) if usage_path else None
        self.daily_limit = int(daily_limit)
        if self.daily_limit <= 0:
            raise ValueError('Daily API limit must be positive')
        self.usage = {}
        if self.usage_path and self.usage_path.exists():
            self.usage = json.loads(self.usage_path.read_text())
            if not isinstance(self.usage, dict) or type(self.usage.get('count')) is not int or self.usage['count'] < 0:
                raise ValueError('Invalid API usage file; preserve it for diagnosis')

    def reserve_request(self):
        now = datetime.fromtimestamp(self.clock(), timezone(timedelta(hours=3)))
        day = now.date().isoformat()
        count = self.usage.get('count', 0) if self.usage.get('day') == day else 0
        if count >= self.daily_limit:
            midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo)
            raise SourceUnavailable('Достигнут суточный лимит поискового API; сброс в 00:00 по Москве', 'index_budget', int((midnight - now).total_seconds()) + 1)
        self.usage = {'day': day, 'count': count + 1}
        if self.usage_path:
            self.usage_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.usage_path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.usage))
            temp.replace(self.usage_path)

    async def fetch(self, client, source, query, trace=None):
        async with self.lock:
            if self.retry_at > self.clock():
                raise SourceUnavailable('Поисковый API на паузе после отказа; проверьте статус подключения', self.retry_code, int(self.retry_at - self.clock()) + 1)
            wait = 2 - (self.clock() - self.last)
            if wait > 0:
                await self.sleep(wait)
            host, _ = INDEX_SOURCES[source]
            # q is a value in a structured HTTP request, never shell text.
            request_query = f'site:{host} {query}'
            self.reserve_request()
            try:
                response = await client.get(API_URL, params={'q': request_query, 'count': 20,
                                            'country': 'RU', 'search_lang': 'ru', 'text_decorations': 'false'},
                                            headers={'X-Subscription-Token': self.key, 'Accept': 'application/json'},
                                            follow_redirects=False)
                if trace is not None:
                    trace.append({'status': response.status_code, 'host': 'api.search.brave.com', 'path': '/res/v1/web/search'})
                if response.status_code in (401, 402, 403):
                    self.retry_at = self.clock() + 21600
                    self.retry_code = 'index_auth'
                    raise SourceUnavailable('Поисковый API не принял ключ или права тарифа; проверьте настройки Brave Search API', 'index_auth')
                if response.status_code == 429:
                    delay = 1800
                    raw = response.headers.get('retry-after', '')
                    if raw.isdigit():
                        delay = max(delay, int(raw))
                    elif raw:
                        try:
                            delay = max(delay, int((parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()))
                        except (ValueError, TypeError, OverflowError):
                            pass
                    self.retry_at = self.clock() + delay
                    self.retry_code = 'http_429'
                    raise SourceUnavailable('Поисковый API ограничил частоту или квоту запросов', 'http_429', delay)
                if response.is_redirect:
                    # Do not forward the API key to any redirect destination.
                    raise SourceUnavailable('Неожиданное перенаправление поискового API', 'index_layout')
                if response.status_code == 400:
                    raise SourceUnavailable('Поисковый API отклонил параметры запроса', 'index_layout')
                response.raise_for_status()
                if len(response.content) > 5_000_000:
                    raise SourceUnavailable('Слишком большой ответ поискового API', 'index_layout')
                try:
                    return parse_index_results(source, response.json())
                except ValueError as exc:
                    if isinstance(exc, SourceUnavailable):
                        raise
                    raise SourceUnavailable('Поисковый API вернул некорректный JSON', 'index_layout') from None
            finally:
                self.last = self.clock()
