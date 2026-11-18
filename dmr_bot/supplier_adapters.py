"""Read-only, public supplier routes verified in the resource audit."""
import hashlib
import asyncio
import json
import re
from urllib.parse import quote, urljoin, urlsplit, parse_qs, urlencode
from bs4 import BeautifulSoup
from .discovery import SourceUnavailable

SPECS = {
    'platan': ('Платан', 'https://shop.platan.ru/export/price.json?search={query}', 'LM358'),
    'efo': ('ЭФО', 'https://efo.ru/search/stock.html?q={query}', 'OKW'),
    'radiodetali': ('Сеть электронных компонентов', 'https://radiodetali.com/catalog?q={query}', 'LM358'),
    'radioshop': ('Radio Shop', 'https://radio-shop.ru/search/?search={query}', 'DMR'),
    'chipfind': ('ChipFind', 'https://www.chipfind.ru/search/index.htm?part={query}', 'LM358'),
    'radiosila': ('Радиосила', 'https://radiosila.ru/component/search/?searchword={query}&ordering=&searchphrase=all', 'DMR'),
}


def item(source, title, **extra):
    return {'title': title, 'description': None, 'price': None, 'currency': None,
            'seller': SPECS[source][0], 'published_at': None, 'images': [],
            'source': source, 'capture_kind': 'supplier_search', 'structured_data': {}, **extra}


def amount(value):
    text = str(value or '').replace('\xa0', '').replace(' ', '').replace(',', '.')
    return text if re.fullmatch(r'\d+(?:\.\d+)?', text) else None


def parse_supplier(source, body, query):
    base = SPECS[source][1].format(query=quote(query, safe=''))
    if source == 'platan':
        try:
            payload = json.loads(body)
        except ValueError:
            raise SourceUnavailable('API Платана вернул не JSON', 'layout') from None
        rows = payload.get('items') if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise SourceUnavailable('API Платана: неизвестная структура', 'layout')
        return [(f"https://www.platan.ru/cgi-bin/qwery.pl/id={r['NOM_N']}", item(source, str(r['NAME']),
                 price=amount(r.get('PRICE_1')), currency='RUB', manufacturer=r.get('MANUFAC'), availability=r.get('QUANTY')))
                for r in rows if isinstance(r, dict) and str(r.get('NOM_N', '')).isdigit() and r.get('NAME')]
    soup = BeautifulSoup(body, 'html.parser')
    title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    if any(w in title for w in ('captcha', 'access denied', 'проверка безопасности', 'авторизация', 'ddos-guard')):
        raise SourceUnavailable('Поставщик требует проверку доступа', 'challenge')
    result = {}
    if source == 'efo':
        for tr in soup.select('table.stock tbody tr.top'):
            cells = tr.find_all('td', recursive=False)
            if len(cells) < 8:
                continue
            part = cells[1].get_text(' ', strip=True)
            if not part or '{{' in part:
                continue
            url = 'https://efo.ru/search/stock.html?' + urlencode({'q': part})
            result[url] = item(source, ' '.join([part, cells[2].get_text(' ', strip=True), cells[3].get_text(' ', strip=True)]),
                               price=amount(cells[5].get_text()), currency='RUB',
                               availability=cells[6].get_text(' ', strip=True))
        recognized = soup.select_one('table.stock') is not None or re.search(r'найдено\s+0\s+позици', soup.get_text(' ', strip=True), re.I)
    elif source == 'chipfind':
        # Published classic layout groups one seven-column table under each supplier.
        for table in soup.select('table.res'):
            heading=table.select_one('tr.cap')
            if not heading or len(heading.find_all('td',recursive=False))!=7:
                continue
            outer=table.find_parent('tr',id=re.compile(r'^SS'))
            company=outer.select_one('a[href*="/company/"]') if outer else None
            if not company:continue
            supplier_name=company.get_text(' ',strip=True)
            for tr in table.select('tr.h'):
                cells=tr.find_all('td',recursive=False)
                if len(cells)!=7:continue
                name=cells[1].get_text(' ',strip=True)
                identity=hashlib.sha256((supplier_name+'\0'+name).encode()).hexdigest()[:16]
                result[base+'#offer-'+identity]=item(source,name,seller=supplier_name,
                    manufacturer=cells[0].get_text(' ',strip=True),
                    price_text='; '.join(label+': '+cell.get_text(' ',strip=True) for label,cell in zip(('розница','мелкий опт','опт'),cells[2:5]) if cell.get_text(strip=True)),
                    availability=cells[5].get_text(' ',strip=True))
        supplier = None
        tables=[]
        for table in soup.select('table.res'):
            heading=table.select_one('tr.cap')
            cells=heading.find_all('td',recursive=False) if heading else []
            if len(cells)==6 and 'Поставщик' in cells[0].get_text():tables.append(table)
        for tr in [row for table in tables for row in table.select('tr.h')]:
            if 'cap' in tr.get('class', []):
                continue
            cells = tr.find_all('td', recursive=False)
            if len(cells) == 6:
                supplier = cells.pop(0).get_text(' ', strip=True)
            if len(cells) != 5 or not supplier:
                continue
            name = cells[0].get_text(' ', strip=True)
            if not name:
                continue
            # Keep offers from different suppliers distinct; never use RFQ/cart form tokens.
            identity = hashlib.sha256((supplier + '\0' + name).encode()).hexdigest()[:16]
            url = base + '#offer-' + identity
            result[url] = item(source, name, seller=supplier, price_text=cells[2].get_text(' ', strip=True),
                               availability=cells[3].get_text(' ', strip=True), manufacturer=cells[1].get_text(' ', strip=True))
        recognized = bool(tables) or any(len(h.find_all('td',recursive=False))==7 for h in soup.select('table.res tr.cap'))
    else:
        selectors = {'radiodetali': 'tr.product-row a.name-link[href]',
                     'radioshop': '.product-thumb .caption h4 a[href]',
                     'radiosila': '.search ol li > a[href]'}
        for a in soup.select(selectors[source]):
            url = urljoin(base, a['href']);p = urlsplit(url)
            expected = urlsplit(base).hostname.removeprefix('www.')
            if p.scheme != 'https' or (p.hostname or '').removeprefix('www.') != expected:
                continue
            valid = (source == 'radiodetali' and p.path.startswith('/product/')) or (source == 'radiosila' and p.path.startswith('/katalog/details/'))
            if source == 'radioshop':
                params = parse_qs(p.query)
                valid = p.path == '/index.php' and params.get('route') == ['product/product'] and params.get('product_id', [''])[0].isdigit()
                valid = valid or (p.path.startswith(('/racii/', '/aksessuary-dlya-racij/')) and p.path.count('/') >= 3)
                if params.get('product_id'):
                    url = f'https://{p.netloc}/index.php?' + urlencode({'route': 'product/product', 'product_id': params['product_id'][0]})
            if not valid:
                continue
            name = a.get_text(' ', strip=True)
            if name:
                result[url] = item(source, name)
        texts = soup.get_text(' ', strip=True).lower()
        containers = {'radiodetali': 'table.products-table', 'radioshop': '#content', 'radiosila': '.search'}
        recognized = bool(result) or (soup.select_one(containers[source]) is not None and any(w in texts for w in ('ничего не найдено', 'нет товаров', 'не найдено', 'найдено: 0', 'результатов: 0', 'нет результатов')))
    if not result and not recognized:
        raise SourceUnavailable('Не распознана выдача поставщика; это не подтверждённый нулевой результат', 'layout')
    return list(result.items())


async def fetch_supplier(client, source, query, trace, policy, sleeper=asyncio.sleep):
    url = SPECS[source][1].format(query=quote(query, safe=''))
    if source=='chipfind':url='https://www.chipfind.ru/search/index.htm?'+urlencode({'part':query})
    # Platan explicitly publishes this anonymous catalog API; it is not the disallowed HTML search.
    if source != 'platan':
        await policy.check(source, url, client)
    for _ in range(4):
        # ChipFind supports the library's normal, explicitly automated HTTP client.
        headers={} if source=='chipfind' else {'User-Agent':'CivilianProductSearchBot/0.2'}
        async with client.stream('GET', url, headers=headers) as response:
            p = urlsplit(str(response.url));trace.append({'status': response.status_code, 'host': p.hostname, 'path': p.path})
            if response.is_redirect:
                next_url = str(response.url.join(response.headers.get('location', '')));dest = urlsplit(next_url)
                if dest.scheme != 'https' or dest.hostname != p.hostname or any(w in dest.path for w in ('login', 'captcha', 'signin')):
                    raise SourceUnavailable('Неподдерживаемое перенаправление поставщика', 'redirect')
                parameter = {'efo': 'q', 'radiodetali': 'q', 'radioshop': 'search', 'chipfind': 'part', 'radiosila': 'searchword'}.get(source)
                if not parameter or query not in parse_qs(dest.query).get(parameter, []):
                    raise SourceUnavailable('Поставщик потерял поисковый запрос при переходе', 'redirect')
                await policy.check(source, next_url, client)
                await sleeper(max(2, policy.delay(source, next_url)))
                url = next_url;continue
            if response.status_code in (401, 403, 429, 498):
                raw = response.headers.get('retry-after', '')
                raise SourceUnavailable(f'Поставщик ограничил доступ (HTTP {response.status_code})', f'http_{response.status_code}', int(raw) if raw.isdigit() else None)
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 6_000_000:
                    raise SourceUnavailable('Превышен предел ответа поставщика', 'layout')
            return parse_supplier(source, body.decode(response.encoding or 'utf-8', errors='replace'), query)
    raise SourceUnavailable('Цикл перенаправлений поставщика', 'redirect')
