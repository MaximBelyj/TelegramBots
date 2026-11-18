import json
import tempfile
from pathlib import Path
import unittest
import httpx
from dmr_bot.supplier_adapters import parse_supplier, fetch_supplier
from dmr_bot.resource_catalog import SearchPolicy
from dmr_bot.search_engine import SearchEngine


class SupplierParsingTests(unittest.TestCase):
    def test_api_and_no_inventory_invention(self):
        rows=parse_supplier('platan',json.dumps({'items':[{'NOM_N':'123','NAME':'LM358','PRICE_1':'9.00','QUANTY':'0'}]}),'LM358')
        self.assertEqual(rows[0][1]['price'],'9.00')
        self.assertEqual(rows[0][1]['availability'],'0')
        self.assertTrue(rows[0][0].endswith('id=123'))

    def test_efo_table_ignores_vue_and_cart(self):
        html='<table class="stock"><tbody><tr class="top"><td>Заказать</td><td>CASE123</td><td>OKW</td><td>Корпус</td><td>шт</td><td>12</td><td>На складе</td><td>5</td><td><form action="/cart/shop"><input name="_token" value="private"></form></td></tr><tr><td>{{item.id}}</td></tr></tbody></table>'
        rows=parse_supplier('efo',html,'CASE123')
        self.assertEqual(len(rows),1);self.assertIn('q=CASE123',rows[0][0]);self.assertNotIn('private',str(rows))

    def test_chipfind_rowspan_supplier_is_preserved(self):
        html='<table class="res"><tr class="cap"><td>Поставщик</td><td>Наименование</td><td>Производитель</td><td>Цена</td><td>Склад</td><td></td></tr><tr class="h"><td rowspan="2">Shop</td><td>LM358</td><td>TI</td><td>10 р.</td><td>2</td><td></td></tr><tr class="h"><td>LM358P</td><td>TI</td><td>15 р.</td><td>3</td><td></td></tr></table>'
        rows=parse_supplier('chipfind',html,'LM358')
        self.assertEqual(len(rows),2);self.assertTrue(all(r[1]['seller']=='Shop' for r in rows));self.assertNotEqual(rows[0][0],rows[1][0])

    def test_classic_chipfind_layout(self):
        html='<table><tr id="SSabc"><td><a href="/company/shop.htm">Shop</a></td><td><table class="res"><tr class="cap">'+('<td>h</td>'*7)+'</tr><tr class="h"><td>TI</td><td>LM358</td><td>12</td><td>10</td><td>8</td><td>5</td><td></td></tr></table></td></tr></table>'
        rows=parse_supplier('chipfind',html,'LM358');self.assertEqual(len(rows),1);self.assertEqual(rows[0][1]['seller'],'Shop')

    def test_radioshop_product_ids_remain_distinct(self):
        html=''.join(f'<div class="product-thumb"><div class="caption"><h4><a href="/index.php?route=product/product&amp;product_id={i}">Радиостанция DMR</a></h4></div></div>' for i in (1,2))
        rows=parse_supplier('radioshop',html,'DMR');self.assertEqual(len(rows),2);self.assertIn('product_id=1',rows[0][0])

    def test_radiodetali_and_radiosila_exclude_categories_articles(self):
        html='<table><tr class="product-row"><td><a class="name-link" href="/product/lm358">LM358</a></td></tr></table><a href="/catalog">Catalog</a>'
        self.assertEqual(len(parse_supplier('radiodetali',html,'LM358')),1)
        html='<div class="search"><ol><li><a href="/katalog/details/1/radio.html">Радиостанция DMR</a></li><li><a href="/news/dmr.html">DMR</a></li></ol></div>'
        self.assertEqual(len(parse_supplier('radiosila',html,'DMR')),1)


class SupplierTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_uses_documented_route_without_credentials_or_cart_calls(self):
        calls=[]
        def handler(request):
            calls.append(request);return httpx.Response(200,json={'items':[{'NOM_N':'123','NAME':'LM358'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            rows=await fetch_supplier(client,'platan','LM358',[],SearchPolicy(lambda:0))
        self.assertEqual(len(rows),1);self.assertEqual(len(calls),1);self.assertEqual(calls[0].url.path,'/export/price.json');self.assertEqual(calls[0].method,'GET')

    async def test_chipfind_uses_normal_automated_http_client_and_search_only(self):
        calls=[]
        def handler(request):
            calls.append(request)
            if request.url.path=='/robots.txt':return httpx.Response(200,text='User-agent: *\nDisallow: /cart')
            self.assertTrue(request.headers.get('user-agent','').startswith('python-httpx/'))
            return httpx.Response(200,text='<table class="res"><tr class="cap"><td>Поставщик</td><td>Наименование</td><td>Производитель</td><td>Цена</td><td>Склад</td><td></td></tr><tr class="h"><td>Shop</td><td>LM358</td><td>TI</td><td>10</td><td>3</td><td></td></tr></table>')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result=await fetch_supplier(client,'chipfind','LM358',[],SearchPolicy(lambda:0))
        self.assertEqual(len(result),1)
        self.assertEqual([r.url.path for r in calls],['/robots.txt','/search/index.htm'])
