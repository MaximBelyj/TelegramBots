import tempfile
import unittest
from dmr_bot.archive import Archive
from dmr_bot.sources import parse_product, validate_url


class ArchiveTests(unittest.TestCase):
    def test_history_retains_removed_listing(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Archive(folder + '/archive.db')
            item = {'title': 'DMR radio', 'price': 100, 'seller': 'seller', 'published_at': None}
            self.assertTrue(archive.save('https://www.avito.ru/item', item))
            self.assertFalse(archive.save('https://www.avito.ru/item', item))
            self.assertTrue(archive.save('https://www.avito.ru/item', {**item, 'price': 90}))
            archive.watch('https://www.avito.ru/item')
            archive.unwatch('https://www.avito.ru/item')
            self.assertEqual(len(archive.history('https://www.avito.ru/item')), 2)
            self.assertEqual(len(archive.search('dmr')), 1)
            archive.db.close()

    def test_metadata(self):
        html = '''<script type="application/ld+json">{"@graph":[{"@type":"Product","name":"DMR","offers":{"price":"5000","seller":{"name":"Shop"}},"datePublished":"2026-01-01"}]}</script>'''
        item = parse_product(html)
        self.assertEqual(item['seller']['name'], 'Shop')
        self.assertEqual(item['published_at'], '2026-01-01')
        self.assertEqual(item['price'], '5000')

    def test_block_pages_are_not_listings(self):
        with self.assertRaises(ValueError):
            parse_product('<html>Captcha</html>')

    def test_destinations_restricted(self):
        for url in ['http://avito.ru/a', 'https://avito.ru.evil.org/a', 'https://127.0.0.1/', 'https://user:pass@avito.ru/a']:
            with self.assertRaises(ValueError):
                validate_url(url)
