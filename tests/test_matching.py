import unittest
from unittest.mock import AsyncMock, patch
from dmr_bot.matching import evaluate, identifier_matches, query_variants
from dmr_bot.discovery import discover, parse_search


class MatchingTests(unittest.TestCase):
    def test_shortened_name_without_model_is_accepted(self):
        result = evaluate('Реле давления', 'Реле давления РД-301')
        self.assertTrue(result['accepted'])
        self.assertFalse(result['exact_model'])
        self.assertFalse(evaluate('Реле автомобильное', 'Реле давления РД-301')['accepted'])

    def test_model_has_more_weight_than_description(self):
        model = evaluate('РД 301', 'Реле давления РД-301')
        words = evaluate('Реле давления', 'Реле давления РД-301')
        self.assertTrue(model['accepted'])
        self.assertGreater(model['score'], words['score'])
        self.assertEqual(model['matched'], ['рд-301'])

    def test_no_fuzzy_digit_correction_or_model_suffix(self):
        for name in ('Реле давления РД-302', 'Реле давления РД-301-1', 'Реле давления FB-301', 'Реле давления АРД-2'):
            with self.subTest(name=name):
                result = evaluate(name, 'Реле давления РД-301')
                self.assertFalse(result['accepted'])
                self.assertTrue(result['model_conflict'])
        self.assertFalse(identifier_matches('1300.3737', '1300.3738'))
        self.assertTrue(identifier_matches('ВК-856', 'ВК 856'))
        self.assertTrue(identifier_matches('РД-301', 'рд301'))

    def test_no_repeated_word_bonus_and_stop_words(self):
        query = 'Клапан электромагнитный двухпозиционный'
        once = evaluate('Клапан электромагнитный', query)
        repeated = evaluate('Клапан клапан клапан электромагнитный', query)
        self.assertEqual(once['score'], repeated['score'])
        self.assertFalse(evaluate('Устройство для станции', 'Устройство с блоком станции')['accepted'])

    def test_morphology_and_single_word_query(self):
        self.assertTrue(evaluate('Клапаны электромагнитные', 'Клапан электромагнитный')['accepted'])
        self.assertTrue(evaluate('Стилус универсальный', 'Стилус')['accepted'])

    def test_variants_include_model_and_short_description(self):
        variants = query_variants('Реле давления РД-301')
        self.assertEqual(variants, ['Реле давления РД-301', 'рд-301', 'реле давления'])

    def test_candidates_not_pre_filtered_by_broadened_query(self):
        html = '<a href="/product/relay-123/">РД 301</a>'
        self.assertEqual(len(parse_search(html, 'ozon', 'Реле давления', filter_results=False)), 1)


class DiscoveryRankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_variants_deduplicate_and_score_against_original(self):
        async def fake(source, query, **kwargs):
            return [('https://www.ozon.ru/product/relay-1/', {'title': 'Реле давления'}),
                    ('https://www.ozon.ru/product/relay-2/', {'title': 'РД 301'}),
                    ('https://www.ozon.ru/product/relay-3/', {'title': 'Реле давления РД-302'})]
        with patch('dmr_bot.discovery.fetch_search', side_effect=fake) as fetch, patch('dmr_bot.discovery.asyncio.sleep', new=AsyncMock()):
            report = (await discover('Реле давления РД-301', ['ozon']))[0]
        self.assertEqual(fetch.await_count, 3)
        self.assertEqual(len(report['items']), 2)
        self.assertTrue(report['items'][0][1]['match']['exact_model'])
        self.assertEqual(report['items'][1][1]['match']['matched'], ['реле', 'давления'])

    async def test_partial_failure_warns_and_keeps_results(self):
        from dmr_bot.discovery import SourceUnavailable
        async def fake(source, query, **kwargs):
            if query == 'рд-301':
                raise SourceUnavailable('CAPTCHA')
            return [('https://www.ozon.ru/product/relay-1/', {'title': 'Реле давления'})]
        with patch('dmr_bot.discovery.fetch_search', side_effect=fake), patch('dmr_bot.discovery.asyncio.sleep', new=AsyncMock()):
            report = (await discover('Реле давления РД-301', ['ozon']))[0]
        self.assertIsNone(report['error'])
        self.assertIn('CAPTCHA', report['warning'])
        self.assertEqual(len(report['items']), 1)
