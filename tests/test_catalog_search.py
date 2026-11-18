import tempfile
from pathlib import Path
import unittest
import httpx
from dmr_bot.catalog_search import CatalogSearch, links, safe_url
from dmr_bot.resource_catalog import SearchPolicy


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowed_sitemap_cards_are_partial_and_ram_only(self):
        calls=[]
        def handler(request):
            calls.append(request)
            if request.url.path=='/robots.txt':return httpx.Response(200,text='User-agent: *\nDisallow: /search\n')
            if request.url.path=='/sitemap.xml':return httpx.Response(200,text='<urlset><url><loc>https://amperka.ru/product/arduino-uno</loc></url></urlset>')
            return httpx.Response(200,text='<script type="application/ld+json">{"@type":"Product","name":"Arduino Uno"}</script>')
        clock=[100.]
        async def sleep(delay):clock[0]+=delay
        search=CatalogSearch(clock=lambda:clock[0],sleeper=sleep,max_requests=3)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result=await search.fetch('amperka','Arduino',client,SearchPolicy(lambda:clock[0]),[])
        self.assertEqual(len(result),1);self.assertIn('Полный ассортимент не проверен',result.warning)
        self.assertNotIn('/search',[r.url.path for r in calls])

    def test_catalog_rejects_actions_sessions_and_external_destinations(self):
        for path in ('/cart/','https://evil.org/product/1','/search?q=foo'):
            with self.assertRaises(ValueError):safe_url('amperka',path,'https://amperka.ru/')
        values=links('amperka','<urlset><url><loc>https://amperka.ru/product/one</loc></url><url><loc>https://evil.org/x</loc></url></urlset>','https://amperka.ru/sitemap.xml')
        self.assertEqual(values,['https://amperka.ru/product/one'])

    def test_sitemap_is_not_a_product_and_partial_xml_keeps_only_complete_locations(self):
        from dmr_bot.catalog_search import is_card
        self.assertFalse(is_card('elecomp','https://elecomp.ru/sitemap-shop-1.xml'))
        prefix='<urlset><url><loc>https://amperka.ru/product/uno</loc></url><url><loc>https://amperka.ru/product/unfinished'
        self.assertEqual(links('amperka',prefix,'https://amperka.ru/sitemap.xml',partial=True),['https://amperka.ru/product/uno'])

    def test_category_heading_and_product_list_are_not_products(self):
        from dmr_bot.catalog_search import parse_card
        self.assertIsNone(parse_card('radioshop','<h1>Портативные рации</h1>','https://www.radio-shop.ru/racii/portativnye/'))
        self.assertIsNone(parse_card('amperka','<h1>Каталог</h1><script type="application/ld+json">{"@type":"ItemList","itemListElement":[{"@type":"Product","name":"Uno"}]}</script>','https://amperka.ru/product/uno'))

    async def test_forbidden_card_is_skipped_without_request(self):
        calls=[]
        def handler(request):
            calls.append(request.url.path)
            if request.url.path=='/robots.txt':return httpx.Response(200,text='User-agent: *\nDisallow: /product/closed*\n')
            if request.url.path=='/sitemap.xml':return httpx.Response(200,text='<urlset><url><loc>https://amperka.ru/product/closed-sku</loc></url><url><loc>https://amperka.ru/product/uno</loc></url></urlset>')
            return httpx.Response(200,text='<script type="application/ld+json">{"@type":"Product","name":"Uno"}</script>')
        clock=[100.]
        async def sleep(delay):clock[0]+=delay
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result=await CatalogSearch(clock=lambda:clock[0],sleeper=sleep).fetch('amperka','Uno',client,SearchPolicy(lambda:clock[0]),[])
        self.assertEqual(len(result),1);self.assertNotIn('/product/closed-sku',calls)
        self.assertIn('Пропущено запрещённых путей: 1',result.warning)

    def test_recommended_product_metadata_does_not_supply_the_primary_price(self):
        from dmr_bot.catalog_search import parse_card
        html='<h1>Источник питания SW1000</h1><script type="application/ld+json">{"@type":"Product","name":"Кабель","offers":{"price":"123"}}</script>'
        row=parse_card('ruselt',html,'https://www.ruselt.ru/catalog/ibp/2514/')
        self.assertEqual(row['title'],'Источник питания SW1000');self.assertIsNone(row['price'])
