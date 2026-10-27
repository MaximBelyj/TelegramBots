import json
from pathlib import Path
import tempfile
import unittest
import httpx
from dmr_bot.discovery import SourceUnavailable
from dmr_bot.search_index import SearchIndex, parse_index_results
from dmr_bot.search_engine import SearchEngine
from dmr_bot.bot import render_report


def payload(source='ozon'):
    url = 'https://www.ozon.ru/product/radio-123/' if source == 'ozon' else 'https://www.wildberries.ru/catalog/123/detail.aspx'
    return {'type': 'search', 'web': {'results': [
        {'title': 'Радиостанция DMR', 'url': url, 'description': 'Some unverified price'},
        {'title': 'Радиостанция DMR', 'url': 'https://evil.org/product/123'},
        {'title': 'Радиостанция DMR', 'url': 'https://www.ozon.ru/category/radio/'},
    ]}}


class IndexParsingTests(unittest.TestCase):
    def test_only_product_links_not_foreign_sites_and_no_invented_price(self):
        items = parse_index_results('ozon', payload())
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0][1]['price'])
        self.assertEqual(items[0][1]['capture_kind'], 'search_index')
        self.assertEqual(len(parse_index_results('wb', payload('wb'))), 1)

    def test_schema_error_and_genuine_empty_are_distinct(self):
        with self.assertRaises(SourceUnavailable):
            parse_index_results('ozon', {'error': 'Something failed'})
        self.assertEqual(parse_index_results('ozon', {'type': 'search'}), [])
        self.assertEqual(parse_index_results('ozon', {'type': 'search', 'web': {'results': []}}), [])


class IndexTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_supported_api_and_does_not_fetch_marketplace(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=payload())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            items = await SearchIndex('test-only-key').fetch(client, 'ozon', 'DMR')
        self.assertEqual(len(items), 1)
        self.assertEqual(seen[0].url.host, 'api.search.brave.com')
        self.assertIn('site:ozon.ru', seen[0].url.params['q'])
        self.assertEqual(seen[0].headers['X-Subscription-Token'], 'test-only-key')

    async def test_key_is_not_forwarded_on_redirect(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={'location': 'https://evil.org/'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(SourceUnavailable):
                await SearchIndex('test-only-key').fetch(client, 'ozon', 'DMR')
        self.assertEqual(len(seen), 1)

    async def test_quota_pause_applies_to_both_sites(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={'retry-after': '90000'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            index = SearchIndex('test-only-key')
            for source in ('ozon', 'wb'):
                with self.assertRaises(SourceUnavailable):
                    await index.fetch(client, source, 'DMR')
        self.assertEqual(len(calls), 1)

    async def test_index_backend_has_separate_health_and_preserves_direct_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'health.json'
            path.write_text(json.dumps({'ozon': {'status': 'manual', 'reason': 'HTTP 403', 'retry_at': 1e20}}))
            requests = []
            def factory(**kwargs):
                return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
            def handler(request):
                requests.append(request)
                return httpx.Response(200, json=payload())
            e = SearchEngine(path, index_key='test-only-key', client_factory=factory)
            try:
                await e.fetch('ozon', 'DMR', probe=True)
                # Avoid pacing delay; match ranking itself is exercised by the existing discovery suite.
                e.last_request.clear()
                report = (await e.discover('DMR', ['ozon']))[0]
                self.assertEqual(report['retrieval'], 'brave_index')
                self.assertEqual(len(report['items']), 1)
                self.assertIn('не весь каталог', render_report('DMR', [report]))
                self.assertEqual(e.states['ozon']['reason'], 'HTTP 403')
                self.assertEqual(e.states['ozon:brave']['status'], 'ready')
                self.assertTrue(all(r.url.host == 'api.search.brave.com' for r in requests))
                self.assertNotIn('test-only-key', path.read_text())
                self.assertNotIn('DMR', path.read_text())
            finally:
                await e.close()
            direct = SearchEngine(path, index_key='')
            self.assertEqual(direct.state('ozon')['status'], 'manual')
            await direct.close()

    async def test_provider_backoff_survives_restart_and_blocks_other_site(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'health.json'
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(401)
            factory = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
            e = SearchEngine(path, index_key='test-only-key', client_factory=factory)
            with self.assertRaises(SourceUnavailable):
                await e.fetch('ozon', 'DMR', probe=True)
            await e.close()
            fresh = SearchEngine(path, index_key='test-only-key', client_factory=factory)
            with self.assertRaises(SourceUnavailable):
                await fresh.fetch('wb', 'DMR', probe=True)
            self.assertEqual(len(calls), 1)
            await fresh.close()


class IndexBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_limit_is_shared_persistent_and_stops_before_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'usage.json'
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(200, json=payload())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                first = SearchIndex('test-only-key', usage_path=path, daily_limit=1)
                await first.fetch(client, 'ozon', 'DMR')
                fresh = SearchIndex('test-only-key', usage_path=path, daily_limit=1)
                with self.assertRaises(SourceUnavailable) as error:
                    await fresh.fetch(client, 'wb', 'DMR')
                self.assertEqual(error.exception.code, 'index_budget')
            self.assertEqual(len(calls), 1)
            self.assertNotIn('DMR', path.read_text())
            self.assertNotIn('test-only-key', path.read_text())
