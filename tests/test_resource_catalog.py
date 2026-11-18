import json
import tempfile
from pathlib import Path
import unittest
import httpx
from dmr_bot.resource_catalog import CATALOG, NEW_ADAPTERS, SearchPolicy, SearchPolicyUnavailable
from dmr_bot.discovery import SOURCES, parse_search, search_url, SourceUnavailable
from dmr_bot.search_engine import SearchEngine


class CatalogTests(unittest.TestCase):
    def test_inventory_unique_and_only_verified_adapters_in_search(self):
        rows = CATALOG['resources']
        self.assertEqual(len(rows), 36)
        self.assertEqual(len({r['id'] for r in rows}), len(rows))
        self.assertEqual(len({r['domain'] for r in rows}), len(rows))
        self.assertEqual(len(SOURCES), 14)
        self.assertEqual(len(NEW_ADAPTERS), 5)
        self.assertEqual(len([r for r in rows if r['status'] == 'candidate']), 22)
        for row in rows:
            if row['status'] == 'candidate':
                self.assertNotIn(row['id'], SOURCES)
        for row in NEW_ADAPTERS.values():
            self.assertTrue(row['robots']['search_allowed'])
            self.assertEqual(row['search_http'], 200)

    def test_new_card_layouts_avoid_navigation_categories(self):
        fixtures = {
            'symmetron': '<a class="product-table-item-name" href="/catalog/active/chips/amplifiers/lm358/">LM358</a>',
            'dessy': '<a class="search" href="/catalog-pdc123.html#MainProduct">LM358</a>',
            'chipster': '<ul id="catalogList"><li><div class="item-info-wrap"><h3><a href="/catalog/chips/123.html">LM358</a></h3></div></li></ul>',
            'radioexpert': '<div class="thumbnail-caption"><h3><a href="/product/dmr-radio/">Радиостанция DMR</a></h3></div>',
            'rct': '<td class="tdName"><a class="tdIndex" href="/warehouse/723/UT-123.html">LM358</a></td>',
        }
        for source, html in fixtures.items():
            with self.subTest(source=source):
                items = parse_search(html + '<a href="/catalog/">LM358</a>', source, 'DMR' if source == 'radioexpert' else 'LM358')
                self.assertEqual(len(items), 1)
                self.assertNotIn('#', items[0][0])

    def test_cp1251_query_and_known_empty_layouts(self):
        self.assertIn('%F0%E5%E7%E8%F1%F2%EE%F0', search_url('dessy', 'резистор'))
        self.assertEqual(parse_search('<div id="center"><div class="catalog-bar"><h2>Результаты поиска</h2></div><ul id="catalogList"></ul></div>', 'chipster', 'LM358'), [])
        html = '<div id="main"><form id="finderFormF"><input name="search"></form><div id="MainSearch"></div>отображаются результаты поиска по слову</div>'
        self.assertEqual(parse_search(html, 'dessy', 'LM358'), [])
        with self.assertRaises(SourceUnavailable):
            parse_search('<div id="app"></div>', 'dessy', 'LM358')


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_respected_and_cached_per_provider(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, text='User-agent: *\nDisallow: /search/\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = SearchPolicy(lambda: 1000)
            for _ in range(2):
                with self.assertRaises(SearchPolicyUnavailable):
                    await policy.check('symmetron', search_url('symmetron', 'LM358'), client)
        self.assertEqual(len(seen), 1)

    async def test_denied_policy_prevents_product_search_requests(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, text='User-agent: *\nDisallow: /search/\n')
        with tempfile.TemporaryDirectory() as folder:
            factory = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
            e = SearchEngine(Path(folder)/'health.json', index_key='', client_factory=factory)
            try:
                with self.assertRaises(SourceUnavailable) as error:
                    await e.fetch('symmetron', 'LM358', probe=True)
                self.assertEqual(error.exception.code, 'robots')
                self.assertEqual(e.state('symmetron')['status'], 'manual')
            finally:
                await e.close()
        self.assertEqual([r.url.path for r in calls], ['/robots.txt'])
