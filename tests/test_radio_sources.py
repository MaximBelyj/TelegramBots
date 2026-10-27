import unittest
from unittest.mock import patch

import httpx
from dmr_bot.discovery import (SOURCES, SourceUnavailable, canonical_url,
                               fetch_search, parse_search, search_url)
from dmr_bot.sources import validate_url


class RadioSourceTests(unittest.TestCase):
    def test_catalog_links_and_title_cleanup(self):
        cases = {
            'chipdip': '<a href="/product/lm358">LM358</a>',
            'platan': '<a href="/cgi-bin/qwery.pl/id=123" title="&lt;b&gt;LM358&lt;/b&gt;">LM358</a>',
            'promelec': '<a class="product-preview__title" href="/product/123/">LM358</a>',
        }
        for source, html in cases.items():
            with self.subTest(source=source):
                items = parse_search(html, source, 'LM358')
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0][1]['title'], 'LM358')
                self.assertEqual(items[0][1]['source'], source)

    def test_qrz_ignores_ads_and_category_links(self):
        html = '''<li class="classifieds_vip_row"><a href="/classifieds/detail/relay_1">Реле давления</a></li>
        <div class="classified_list"><div class="adv_classified"><span class="Title"><a href="/classifieds/detail/relay_2">Реле давления</a></span></div></div>
        <a href="/classifieds/category/electronics">Реле давления</a>'''
        items = parse_search(html, 'qrz', 'Реле давления')
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0][0].endswith('relay_2'))

    def test_qrz_explicit_no_matches_is_successful_zero(self):
        html = '<div>С выбранной фильтрацией ничего не найдено.</div>'
        self.assertEqual(parse_search(html, 'qrz', 'LM358'), [])

    def test_explicit_empty_catalog_is_not_parser_failure(self):
        self.assertEqual(parse_search('<h1>Результат поиска <sub>0</sub></h1>', 'platan', 'LM358'), [])
        self.assertEqual(parse_search('<div>Найдено: 0 товар(ов)</div>', 'promelec', 'LM358'), [])
        with self.assertRaises(SourceUnavailable):
            parse_search('<div id="app"></div>', 'promelec', 'LM358')

    def test_forum_topic_id_survives_dedup_and_sessions_removed(self):
        html = '''<a class="topictitle" href="viewtopic.php?f=51&amp;t=123&amp;sid=private">LM358</a>
        <a class="topictitle" href="viewtopic.php?t=456">LM358</a>'''
        items = parse_search(html, 'radiokot', 'LM358')
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0][0].endswith('viewtopic.php?t=123'))
        self.assertNotIn('sid=', items[0][0])
        with self.assertRaises(ValueError):
            canonical_url('https://radiokot.ru/forum/viewtopic.php?sid=private')

    def test_new_destinations_restricted_and_queries_encoded(self):
        for source in ('chipdip', 'platan', 'promelec', 'qrz', 'radiokot'):
            url = search_url(source, 'LM358 & test/123')
            self.assertIn('%26', url)
            self.assertIn('%2F', url)
            self.assertEqual(validate_url(url), url)
        with self.assertRaises(ValueError):
            validate_url('https://www.platan.ru.attacker.org/')
        with self.assertRaises(SourceUnavailable):
            parse_search('<title>DDoS-Guard</title>', 'chipdip', 'LM358')


class FetchRadioTests(unittest.IsolatedAsyncioTestCase):
    async def test_windows1251_legacy_catalog(self):
        html = '<a href="/cgi-bin/qwery.pl/id=123">Реле давления РД-301</a>'
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=html.encode('cp1251'), headers={'content-type': 'text/html; charset=windows-1251'}))
        real_client = httpx.AsyncClient
        with patch('dmr_bot.discovery.httpx.AsyncClient', side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
            items = await fetch_search('platan', 'Реле давления РД-301')
        self.assertEqual(items[0][1]['title'], 'Реле давления РД-301')
