"""Per-site queues, persistent access status, no persistent product/query results."""
import asyncio
import json
import math
import os
from pathlib import Path
import time

import httpx
from .discovery import SOURCES, SourceUnavailable, discover, fetch_search
from .search_index import SearchIndex, INDEX_SOURCES

INTERVALS = {'market': 15, 'qrz': 15, 'platan': 10, 'promelec': 15}
PROBE_QUERIES = {key: ('трансивер' if key == 'qrz' else 'DMR' if key in ('avito', 'ozon', 'wb', 'market') else 'LM358') for key in SOURCES}
MANUAL_CODES = {'proxy', 'http_401', 'http_403', 'http_498', 'challenge', 'layout', 'redirect', 'index_auth', 'index_layout'}
PAUSES = {'proxy': 21600, 'http_401': 21600, 'http_403': 21600, 'http_498': 21600,
          'challenge': 21600, 'http_429': 1800, 'redirect': 3600, 'layout': 3600, 'network': 300, 'index_auth': 21600, 'index_layout': 3600, 'index_budget': 0}


class SearchEngine:
    def __init__(self, path, *, clock=time.time, sleeper=asyncio.sleep, client_factory=httpx.AsyncClient, index_key=None):
        self.path = Path(path)
        key = os.getenv('BRAVE_SEARCH_API_KEY') if index_key is None else index_key
        self.index = SearchIndex(key, clock=clock, sleeper=sleeper, usage_path=self.path.with_name('brave_usage.json'),
                                 daily_limit=int(os.getenv('BRAVE_SEARCH_DAILY_LIMIT', '100'))) if key else None
        self.clock, self.sleep, self.client_factory = clock, sleeper, client_factory
        self.locks = {source: asyncio.Lock() for source in SOURCES}
        self.clients = {}
        self.last_request = {}
        self.states = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, dict):
                    self.states = {s: row for s, row in data.items() if (s in SOURCES or s.removesuffix(':brave') in INDEX_SOURCES) and isinstance(row, dict)}
            except (OSError, ValueError):
                pass
        self.last_request = {source: row['checked_at'] for source, row in self.states.items()
                             if isinstance(row.get('checked_at'), (int, float))}

    def state_key(self, source):
        return source + ':brave' if self.index and source in INDEX_SOURCES else source

    def state(self, source):
        row = self.states.get(self.state_key(source), {})
        if self.index and source in INDEX_SOURCES:
            for key, global_row in self.states.items():
                if key.endswith(':brave') and global_row.get('code') in ('http_429', 'index_auth', 'index_budget') and global_row.get('retry_at', 0) > self.clock():
                    row = global_row
                    break
        # A stale check is not a promise of readiness in a restored/new environment.
        if row.get('status') == 'ready' and self.clock() - row.get('checked_at', 0) > 86400:
            return {**row, 'status': 'unknown', 'reason': 'Проверка устарела; проверьте доступность в разделе «Площадки»'}
        return {'status': 'unknown', 'reason': 'Доступ ещё не проверен', **row}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.states, ensure_ascii=False, indent=2))
        temp.replace(self.path)

    def status_text(self, source):
        row = self.state(source)
        if row['status'] == 'ready':
            return '✅ Поиск через индекс Brave, не весь каталог' if self.index and source in INDEX_SOURCES else '✅ Автопоиск: последняя проверка успешна'
        label = '🔗 Только ручная ссылка' if row['status'] in ('manual', 'unknown') else '⏳ Автопоиск на паузе'
        text = label + ': ' + row['reason']
        remaining = row.get('retry_at', 0) - self.clock()
        if remaining > 0:
            text += f'. Повторная проверка не раньше чем через {math.ceil(remaining / 60)} мин.'
        return text

    def _success(self, source, trace):
        self.states[self.state_key(source)] = {'status': 'ready', 'reason': 'Выдача распознана', 'checked_at': self.clock(),
                               'retry_at': 0, 'failures': 0, 'trace': trace}
        self.save()

    def _failure(self, source, exc, trace):
        old = self.states.get(self.state_key(source), {})
        count = min(int(old.get('failures', 0)) + 1, 6)
        delay = min(86400, PAUSES.get(exc.code, 3600) * 2 ** (count - 1))
        if exc.retry_after is not None:
            # Retry-After is a lower bound; do not retry before the server permits it.
            delay = max(delay, exc.retry_after)
        self.states[self.state_key(source)] = {'status': 'manual' if exc.code in MANUAL_CODES else 'cooldown',
                               'reason': str(exc), 'code': exc.code, 'checked_at': self.clock(),
                               'retry_at': self.clock() + delay, 'failures': count, 'trace': trace}
        self.save()

    async def fetch(self, source, query, *, candidates=False, probe=False):
        async with self.locks[source]:
            row = self.state(source)
            if row.get('retry_at', 0) > self.clock():
                raise SourceUnavailable(self.status_text(source), 'paused')
            if not probe and row['status'] in ('manual', 'unknown'):
                raise SourceUnavailable(self.status_text(source), 'manual')
            wait = INTERVALS.get(source, 15) - (self.clock() - self.last_request.get(self.state_key(source), -1e20))
            if wait > 0:
                await self.sleep(wait)
            if source not in self.clients:
                self.clients[source] = self.client_factory(timeout=20, follow_redirects=False)
            trace = []
            try:
                if self.index and source in INDEX_SOURCES:
                    items = await self.index.fetch(self.clients[source], source, query, trace=trace)
                else:
                    items = await fetch_search(source, query, candidates=candidates,
                                               client=self.clients[source], trace=trace)
            except httpx.ProxyError:
                exc = SourceUnavailable('Запрос отклонён сетевым прокси; это не ответ магазина', 'proxy')
                self._failure(source, exc, trace)
                raise exc from None
            except SourceUnavailable as exc:
                self._failure(source, exc, trace)
                raise
            except (httpx.HTTPError, ValueError):
                exc = SourceUnavailable('Сбой сети или неподдерживаемое перенаправление', 'network')
                self._failure(source, exc, trace)
                raise exc from None
            finally:
                self.last_request[self.state_key(source)] = self.clock()
            self._success(source, trace)
            return items

    async def discover(self, query, sources=None):
        reports = await discover(query, sources, fetcher=self.fetch)
        for report in reports:
            report['access_status'] = self.state(report['source'])['status']
            if self.index and report['source'] in INDEX_SOURCES:
                report['retrieval'] = 'brave_index'
        return reports

    async def probe(self):
        async def one(source):
            try:
                await self.fetch(source, PROBE_QUERIES[source], candidates=True, probe=True)
            except SourceUnavailable:
                pass
            return source, self.state(source)
        return dict(await asyncio.gather(*(one(source) for source in SOURCES)))

    async def close(self):
        await asyncio.gather(*(c.aclose() for c in self.clients.values()))
        self.clients.clear()


def main():
    import os
    async def diagnose():
        engine = SearchEngine(os.getenv('DMR_HEALTH_PATH', 'data/source_health.json'))
        try:
            for source, row in (await engine.probe()).items():
                print(SOURCES[source][0] + ': ' + engine.status_text(source))
                for hop in row.get('trace', []):
                    print(f"  HTTP {hop['status']} {hop['host']}{hop['path']}")
        finally:
            await engine.close()
    asyncio.run(diagnose())


if __name__ == '__main__':
    main()
