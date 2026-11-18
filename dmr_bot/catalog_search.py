"""Bounded public catalog sampling. Explicitly partial, with RAM-only product caches."""
import asyncio
import re
import time
from collections import deque
from urllib.parse import urlsplit, urljoin, parse_qs, urlencode, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from .discovery import SourceUnavailable
from .sources import parse_product, validate_url

ROUTES = {
 'radioshop': ('https://www.radio-shop.ru/sitemap-new.xml', r'^/(?:racii|aksessuary-dlya-racij)/.+/$'),
 'promelec': ('https://www.promelec.ru/sitemap/main.xml', r'^/product/\d+/?$'),
 'symmetron': ('https://www.symmetron.ru/sitemap.xml', r'^/catalog/(?:[^/]+/){3,}[^/]+/?$'),
 'chipster': ('https://chipster.ru/catalog/', r'^/catalog/(?:[^/]+/)*\d+\.html$'),
 'radioexpert': ('https://www.radioexpert.ru/', r'^/product/[^/]+/?$'),
 'rct': ('https://www.rct.ru/sitemap.xml', r'^/warehouse/\d+/[^/]+\.html$'),
 'elecomp': ('https://elecomp.ru/sitemap-shop-2.xml', r'^/(?!category/|blog/|search/|page/|o-kompanii/|kontakty/|dostavka-i-oplata/)[^/]+/?$'),
 'zip2002': ('https://www.zip-2002.ru/sitemap.xml', r'^/product/\d+/?$'),
 'roboshop': ('https://roboshop.spb.ru/', r'^/(?:[^/]+/)+[^/]+/?$'),
 'amperka': ('https://amperka.ru/sitemap.xml', r'^/product/[^/]+/?$'),
 'ruselt': ('https://www.ruselt.ru/sitemap.xml', r'^/catalog/(?:[^/]+/)+\d+/?$'),
}


class CatalogBatch(list):
    def __init__(self, rows, warning):
        super().__init__(rows)
        self.warning = warning


def safe_url(source, raw, base):
    p=urlsplit(validate_url(urljoin(base, raw)))
    host=urlsplit(ROUTES[source][0]).hostname.removeprefix('www.')
    if p.hostname.removeprefix('www.')!=host:
        raise ValueError('Different catalog host')
    if any(w in p.path.lower() for w in ('login','captcha','cart','basket','checkout','account','order','search')):
        raise ValueError('Not a public catalog route')
    query=''
    if source in ('roboshop','radioshop'):
        params=parse_qs(p.query)
        if params.get('route') not in (['product/product'],['product/category']):
            if p.query:raise ValueError('Not a read-only catalog route')
        else:
            wanted={'route':params['route'][0]}
            for key in ('product_id','path'):
                if key in params and re.fullmatch(r'[0-9_]+',params[key][0]):wanted[key]=params[key][0]
            query=urlencode(wanted)
    elif p.query:
        raise ValueError('Unexpected catalog parameters')
    return urlunsplit((p.scheme,p.netloc,p.path,query,''))


def is_card(source,url):
    p=urlsplit(url)
    if p.path.endswith(('.xml','.xml.gz')):return False
    if source=='roboshop' and p.query:return parse_qs(p.query).get('route')==['product/product'] and parse_qs(p.query).get('product_id',[''])[0].isdigit()
    if source=='radioshop' and p.query:
        return parse_qs(p.query).get('route')==['product/product'] and parse_qs(p.query).get('product_id',[''])[0].isdigit()
    return re.search(ROUTES[source][1],p.path) is not None


def links(source, text, base, *, partial=False):
    xml=text.lstrip().startswith('<?xml') or '<urlset' in text[:500] or '<sitemapindex' in text[:500]
    if xml:
        try:
            if partial:
                parser=ElementTree.XMLPullParser(events=('end',))
                parser.feed(text)
                raw=[node.text for _,node in parser.read_events() if node.tag.rsplit('}',1)[-1]=='loc' and node.text]
            else:
                root=ElementTree.fromstring(text)
                raw=[node.text for node in root.iter() if node.tag.rsplit('}',1)[-1]=='loc' and node.text]
        except ElementTree.ParseError:
            raise SourceUnavailable('Неполная или неизвестная карта каталога','layout') from None
    else:
        soup=BeautifulSoup(text,'html.parser');raw=[a['href'] for a in soup.select('a[href]')]
    result=[]
    for value in raw:
        try:
            url=safe_url(source,value,base)
        except ValueError:continue
        path=urlsplit(url).path
        if (xml and path.endswith('.xml')) or is_card(source,url) or path.startswith(('/catalog/','/collection/','/warehouse/','/category/')) or (source=='roboshop' and 'product/category' in url):
            result.append(url)
    return list(dict.fromkeys(result))


def parse_card(source,text,url):
    soup=BeautifulSoup(text,'html.parser')
    title=soup.title.get_text(' ',strip=True).lower() if soup.title else ''
    if any(w in title for w in ('captcha','проверка безопасности','access denied','ddos-guard')):
        raise SourceUnavailable('Каталог требует проверку доступа','challenge')
    h=soup.select_one('h1')
    if re.search(r'"@type"\s*:\s*"ItemList"',text):return None
    try:row=parse_product(text)
    except ValueError:
        if not h or not is_card(source,url):return None
        # Except strict warehouse/numeric SKU routes, H1 alone may be a category.
        if source not in ('rct','ruselt') and not soup.select_one('[itemtype*="Product"], .product-page, #product, .product-info, #button-cart'):
            return None
        row={'title':h.get_text(' ',strip=True),'price':None,'currency':None,'seller':None,'published_at':None,'images':[],'description':None,'structured_data':{}}
    if h and row.get('title') and ' '.join(str(row['title']).casefold().split())!=' '.join(h.get_text(' ',strip=True).casefold().split()):
        if source not in ('rct','ruselt'):return None
        # Strict SKU detail pages may embed JSON-LD for a recommended product.
        # Keep only the page's main title; never borrow that other item's price.
        row={'title':h.get_text(' ',strip=True),'price':None,'currency':None,'seller':None,'description':None,'images':[],'published_at':None,'structured_data':{}}
    row.update(source=source,capture_kind='catalog_sample')
    if isinstance(row.get('seller'),dict):row['seller']=row['seller'].get('name')
    return row if row.get('title') else None


class CatalogSearch:
    def __init__(self,clock=time.time,sleeper=asyncio.sleep,max_requests=10,max_seconds=55):
        self.clock,self.sleep=clock,sleeper
        self.max_requests,self.max_seconds=max_requests,max_seconds
        self.cache={};self.inventory={};self.last={}

    async def fetch(self,source,query,client,policy,trace):
        now=self.clock();entry=self.cache.get(source)
        records=dict(entry[1]) if entry and now-entry[0]<21600 else {}
        known=self.inventory.get(source,[])
        words=re.findall(r'[a-zа-я0-9]+',query.casefold())
        def score(u):
            path=urlsplit(u).path.lower()
            return (sum(w in u.casefold() for w in words), is_card(source,u),
                    any(w in path for w in ('goods','shop','product','iblock')),path.count('/'))
        todo=deque([u for u in sorted(known,key=score,reverse=True) if not (is_card(source,u) and u in records)][:50] if known else [ROUTES[source][0]])
        seen=set();requests=0;deadline=now+self.max_seconds;errors=[];denied=0
        while todo and requests<self.max_requests and self.clock()<deadline:
            url=todo.popleft()
            if url in seen or (is_card(source,url) and url in records):continue
            seen.add(url)
            try:
                await policy.check(source,url,client)
                delay=max(2,policy.delay(source,url))-(self.clock()-self.last.get(source,-1e20))
                if delay>0:
                    if self.clock()+delay>deadline:break
                    await self.sleep(delay)
                requests+=1
                async with client.stream('GET',url,headers={'User-Agent':'CivilianProductSearchBot/0.2'}) as response:
                    p=urlsplit(str(response.url));trace.append({'status':response.status_code,'host':p.hostname,'path':p.path})
                    if response.is_redirect:
                        todo.appendleft(safe_url(source,response.headers.get('location',''),url));continue
                    if response.status_code in (401,403,429,498):
                        raise SourceUnavailable(f'Каталог ограничил доступ (HTTP {response.status_code})',f'http_{response.status_code}')
                    response.raise_for_status();body=bytearray();truncated=False
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body)>4_000_000:
                            if not urlsplit(url).path.endswith('.xml'):
                                raise SourceUnavailable('Превышен предел страницы каталога','coverage')
                            body=body[:4_000_000];truncated=True
                            errors.append('Большая карта прочитана частично')
                            break
                    text=body.decode(response.encoding or 'utf-8',errors='replace')
                if is_card(source,url):
                    row=parse_card(source,text,url)
                    if row:
                        records[url]=row
                        if len(records)>500:records.pop(next(iter(records)))
                else:
                    found=links(source,text,url,partial=truncated)
                    inventory=sorted(dict.fromkeys(known+found),key=score,reverse=True)[:30000]
                    known=inventory;self.inventory[source]=inventory
                    # Keyword slugs are useful, opaque product IDs remain a bounded sample.
                    todo.extendleft(reversed(sorted(found,key=score,reverse=True)[:100]))
            except SourceUnavailable:
                raise
            except Exception as exc:
                # Do not hide challenge/access denial; transport errors are reported as incomplete work.
                from .resource_catalog import SearchPolicyUnavailable
                if isinstance(exc,SearchPolicyUnavailable):
                    if url==ROUTES[source][0]:raise
                    denied+=1
                    continue
                errors.append(type(exc).__name__)
                break
            finally:self.last[source]=self.clock()
        self.cache[source]=(entry[0] if entry and now-entry[0]<21600 else now,records)
        warning=f'Выборочная проверка каталога: {len(records)} карточек в памяти; {requests} сетевых запросов. Полный ассортимент не проверен; ноль совпадений не означает отсутствие товара.'
        if denied:warning+=f' Пропущено запрещённых путей: {denied}.'
        if errors:warning+=' Ограничения проверки: '+', '.join(errors)
        if not records and errors:raise SourceUnavailable(warning,'coverage')
        return CatalogBatch(list(records.items()),warning)
