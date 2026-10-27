import asyncio
import tempfile
import unittest
from unittest.mock import patch

from dmr_bot.archive import Archive
from dmr_bot.discovery import SourceUnavailable, discover, parse_search, search_url


class DiscoveryTests(unittest.TestCase):
    def test_marketplace_links_and_filter(self):
        paths = {'avito': '/moskva/ratsii/radiostantsiya_12345',
                 'ozon': '/product/radio-dmr-123/',
                 'wb': '/catalog/123/detail.aspx',
                 'market': '/product--dmr/123'}
        for source, path in paths.items():
            with self.subTest(source=source):
                html = f'<a href="{path}?tracking=1">Радиостанция DMR</a><a href="{path}?tracking=2">Радиостанция DMR</a>'
                results = parse_search(html, source, 'DMR')
                self.assertEqual(len(results), 1)
                self.assertNotIn('?', results[0][0])
                self.assertIsNone(results[0][1]['published_at'])
                self.assertIsNone(results[0][1]['seller'])
                self.assertEqual(parse_search(f'<a href="{path}">Антенна для DMR</a>', source, 'DMR'), [])

    def test_unrelated_dmr_brand_is_excluded(self):
        html = '<a href="/card/dropaut-dmr/123">Дропаут DMR Rhythm</a><a href="/card/radio-dmr/456">Портативная DMR радиостанция</a>'
        results = parse_search(html, 'market', 'DMR')
        self.assertEqual(len(results), 1)
        self.assertIn('radio-dmr', results[0][0])

    def test_jsonld_keeps_seller_and_price(self):
        html = '''<script type="application/ld+json">{"@type":"ItemList","itemListElement":[{"@type":"Product","name":"DMR рация","url":"https://www.ozon.ru/product/dmr-123/","offers":{"price":10000,"seller":{"name":"Магазин"}}}]}</script>'''
        item = parse_search(html, 'ozon', 'DMR')[0][1]
        self.assertEqual(item['price'], 10000)
        self.assertEqual(item['seller']['name'], 'Магазин')

    def test_captcha_and_js_shell_not_empty_success(self):
        for html in ('<title>CAPTCHA</title>', '<div id="app"></div>'):
            with self.assertRaises(SourceUnavailable):
                parse_search(html, 'market', 'DMR')

    def test_query_encoded(self):
        self.assertIn('%26', search_url('avito', 'DMR & radio'))

    def test_one_failed_source_does_not_lose_other_results(self):
        async def fake(source, query):
            if source == 'avito':
                raise SourceUnavailable('Blocked')
            return [('https://www.ozon.ru/product/radio-1/', {'title': 'DMR рация'})]
        with patch('dmr_bot.discovery.fetch_search', side_effect=fake):
            reports = asyncio.run(discover('DMR'))
        self.assertEqual(len(reports), 4)
        self.assertEqual(reports[0]['error'], 'Blocked')
        self.assertEqual(len(reports[1]['items']), 1)

    def test_persist_queries_and_protect_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = folder + '/archive.db'
            archive = Archive(path)
            archive.add_query('DMR')
            archive.add_query('DMR')
            archive.db.close()
            archive = Archive(path)
            self.assertEqual(archive.queries(), ['DMR'])
            url = 'https://www.ozon.ru/product/radio-1/'
            archive.save_discovery(url, {'title': 'DMR', 'capture_kind': 'search_result'})
            archive.save(url, {'title': 'DMR', 'description': 'Full description', 'seller': 'Seller'})
            self.assertFalse(archive.save_discovery(url, {'title': 'DMR', 'capture_kind': 'search_result'}))
            self.assertEqual(len(archive.history(url)), 2)
            archive.remove_query('DMR')
            self.assertEqual(archive.queries(), [])
            archive.db.close()


class ScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pipeline_archives_when_enrichment_fails_and_deduplicates(self):
        from dmr_bot.discovery import scan_archive
        url = 'https://www.ozon.ru/product/radio-123/'
        item = {'title': 'DMR рация', 'capture_kind': 'search_result'}
        async def fake_discover(query):
            return [{'source': 'ozon', 'items': [(url, item)], 'error': None}]
        async def fail_card(url):
            raise ValueError('No product metadata')
        with tempfile.TemporaryDirectory() as folder:
            archive = Archive(folder + '/test.db')
            with patch('dmr_bot.discovery.discover', side_effect=fake_discover), patch('dmr_bot.sources.fetch_product', side_effect=fail_card), patch('dmr_bot.discovery.asyncio.sleep'):
                first = await scan_archive(archive, 'DMR')
                second = await scan_archive(archive, 'DMR')
            self.assertEqual(len(first[0]['new']), 1)
            self.assertEqual(len(second[0]['new']), 0)
            self.assertEqual(len(archive.history(url)), 1)
            self.assertEqual(archive.watches(), [])
            archive.db.close()
