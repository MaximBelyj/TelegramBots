"""Best-effort public search pages. Never interpret access failures as empty results."""
import asyncio
import json
import re
from urllib.parse import quote, urljoin, urlsplit, urlunsplit, parse_qs, urlencode

import httpx
from bs4 import BeautifulSoup
from .sources import validate_url
from .matching import evaluate, query_variants

SOURCES = {
    'avito': ('Avito', 'https://www.avito.ru/rossiya?q={query}'),
    'ozon': ('Ozon', 'https://www.ozon.ru/search/?text={query}'),
    'wb': ('Wildberries', 'https://www.wildberries.ru/catalog/0/search.aspx?search={query}'),
    'market': ('Яндекс Маркет', 'https://market.yandex.ru/search?text={query}'),
    'chipdip': ('Чип и Дип', 'https://www.chipdip.ru/search?searchtext={query}'),
    'platan': ('Платан', 'https://www.platan.ru/cgi-bin/qwery_i.pl?code={query}'),
    'promelec': ('Промэлектроника', 'https://www.promelec.ru/search/?query={query}'),
    'qrz': ('QRZ.ru', 'https://www.qrz.ru/classifieds/search?q={query}'),
    'radiokot': ('RadioKot', 'https://radiokot.ru/forum/search.php?keywords={query}&terms=all&sf=titleonly'),
}
PATHS = {'avito': r'_[0-9]+$', 'ozon': r'^/product/[^/]+',
         'wb': r'^/catalog/[0-9]+/detail\.aspx$', 'market': r'^/(?:product--|card/|offer/)',
         'chipdip': r'^/product0?/[^/]+/?$',
         'platan': r'^/cgi-bin/qwery\.pl/id=[0-9]+$',
         'promelec': r'^/product/[0-9]+/?$',
         'qrz': r'^/classifieds/detail/[^/]+/?$',
         'radiokot': r'^/forum/viewtopic\.php$'}
SELECTORS = {'platan': 'a[href*="/cgi-bin/qwery.pl/id="]',
             'promelec': 'a.product-preview__title[href]',
             'qrz': '.classified_list .adv_classified .Title a[href]',
             'radiokot': 'a.topictitle[href]'}
ACCESSORIES = re.compile(r'антенн|аккумулятор|батаре|чехол|кабел|зарядн|гарнитур|клипс|программатор', re.I)


class SourceUnavailable(ValueError):
    pass


def search_url(source, query):
    return SOURCES[source][1].format(query=quote(query, safe=''))


def relevant(title, query):
    return evaluate(title, query)['accepted']


def canonical_url(url):
    p = urlsplit(validate_url(url))
    # phpBB identifies topics by the t parameter, not by their shared path.
    query = ''
    if p.hostname.removeprefix('www.') == 'radiokot.ru' and p.path == '/forum/viewtopic.php':
        topic = parse_qs(p.query).get('t', [''])[0]
        if not topic.isdigit():
            raise ValueError('Нет идентификатора темы')
        query = urlencode({'t': topic})
    return urlunsplit((p.scheme, p.netloc, p.path, query, ''))


def parse_search(html, source, query, *, filter_results=True):
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text(' ', strip=True).casefold() if soup.title else ''
    if any(word in title for word in ('captcha', 'капч', 'доступ ограничен', 'доступ запрещ', 'проверка браузера', 'access denied', 'ddos-guard', 'just a moment')) or soup.select_one('form[action*="showcaptcha"]'):
        raise SourceUnavailable('Площадка требует проверку доступа/CAPTCHA')
    page_text = soup.get_text(' ', strip=True).lower()
    count = soup.select_one('h1 sub') if source == 'platan' else None
    if count is not None and count.get_text(strip=True) == '0':
        return []
    if source == 'promelec' and re.search(r'найдено:\s*0\s+товар', page_text):
        return []
    results = {}
    base = search_url(source, query)

    def add(url, name, metadata=None):
        if not isinstance(url, str) or not isinstance(name, str):
            return
        try:
            url = canonical_url(urljoin(base, url))
        except ValueError:
            return
        if urlsplit(url).hostname.removeprefix('www.') != urlsplit(base).hostname.removeprefix('www.') or not re.search(PATHS[source], urlsplit(url).path):
            return
        if filter_results and not relevant(name, query):
            return
        metadata = metadata or {}
        offers = metadata.get('offers', {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if not isinstance(offers, dict):
            offers = {}
        results[url] = {'title': name, 'description': metadata.get('description'),
                        'price': offers.get('price', offers.get('lowPrice')),
                        'currency': offers.get('priceCurrency'),
                        'seller': offers.get('seller', metadata.get('seller')),
                        'published_at': metadata.get('datePublished'),
                        'images': metadata.get('image', []), 'source': source,
                        'capture_kind': 'search_result', 'structured_data': metadata}

    def walk(node):
        if isinstance(node, list):
            for entry in node:
                walk(entry)
        elif isinstance(node, dict):
            kind = node.get('@type', [])
            if isinstance(kind, str):
                kind = [kind]
            if 'Product' in kind:
                add(node.get('url', node.get('@id')), node.get('name'), node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

    for script in soup.find_all('script', type='application/ld+json'):
        try:
            walk(json.loads(script.get_text()))
        except (ValueError, TypeError):
            pass
    product_links = 0
    for anchor in soup.select(SELECTORS.get(source, 'a[href]')):
        href = anchor.get('href', '')
        if not re.search(PATHS[source], urlsplit(urljoin(base, href)).path):
            continue
        product_links += 1
        visible = anchor.get_text(' ', strip=True)
        name = (visible or anchor.get('title')) if source == 'platan' else (anchor.get('title') or visible)
        if name and '<' in name:
            name = BeautifulSoup(name, 'html.parser').get_text(' ', strip=True)
        if not name:
            img = anchor.find('img', alt=True)
            name = img['alt'] if img else ''
        # Structured data has priority over less complete link text.
        try:
            key = canonical_url(urljoin(base, href))
        except ValueError:
            continue
        if key not in results:
            add(href, name)
    if source == 'qrz' and 'с выбранной фильтрацией ничего не найдено' in soup.get_text(' ', strip=True).lower():
        return []
    if not results and not product_links:
        # A JS shell, changed layout, or challenge must never look like a successful zero-result search.
        raise SourceUnavailable('Нет доступной выдачи: защита, JavaScript или неподдерживаемая разметка')
    return list(results.items())[:20]


async def fetch_search(source, query, *, candidates=False):
    url = search_url(source, query)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for _ in range(5):
            async with client.stream('GET', validate_url(url), headers={'User-Agent': 'DMRArchiveBot/0.1'}) as response:
                if 'captcha' in str(response.url).lower():
                    raise SourceUnavailable('Площадка требует CAPTCHA')
                if response.is_redirect:
                    url = str(response.url.join(response.headers['location']))
                    continue
                if response.status_code in (401, 403, 429, 498):
                    raise SourceUnavailable(f'Площадка ограничила доступ (HTTP {response.status_code})')
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 5_000_000:
                        raise SourceUnavailable('Слишком большой ответ площадки')
                return parse_search(body.decode(response.encoding or 'utf-8', errors='replace'), source, query, filter_results=not candidates)
    raise SourceUnavailable('Площадка перенаправляет запросы, выдача недоступна')


async def discover(query, sources=None):
    async def one(source):
        candidates = {}
        errors = []
        successful = 0
        variants = query_variants(query)
        for index, variant in enumerate(variants):
            try:
                if len(variants) == 1:
                    items = await fetch_search(source, variant)
                else:
                    items = await fetch_search(source, variant, candidates=True)
                successful += 1
                for url, item in items:
                    match = evaluate(item['title'], query)
                    if match['accepted']:
                        item = {**item, 'match': match}
                        old = candidates.get(url)
                        if old is None or old['match']['score'] < match['score']:
                            candidates[url] = item
            except SourceUnavailable as exc:
                errors.append(str(exc))
            except httpx.ProxyError:
                errors.append('Сетевой прокси не разрешил запрос')
            except (httpx.HTTPError, ValueError):
                errors.append('Ошибка сети или неподдерживаемый ответ площадки')
            if errors and not successful:
                # Do not repeat a blocked source with more query variants.
                break
            if index + 1 < len(variants):
                await asyncio.sleep(1)
        ranked = sorted(candidates.items(), key=lambda row: (row[1]['match']['exact_model'], row[1]['match']['score']), reverse=True)[:20]
        return {'source': source, 'items': ranked,
                'error': errors[0] if not successful and errors else None,
                'warning': '; '.join(dict.fromkeys(errors)) if successful and errors else None,
                'variants': variants}
    selected = tuple(SOURCES) if sources is None else tuple(dict.fromkeys(sources))
    if not selected or any(source not in SOURCES for source in selected):
        raise ValueError('Неизвестная площадка')
    return await asyncio.gather(*(one(source) for source in selected))


async def scan_archive(archive, query):
    """Persist search evidence, then try full-card capture for up to three new hits/source."""
    from .sources import fetch_product
    reports = await discover(query)
    for report in reports:
        report['new'] = []
        for url, item in report['items']:
            is_new = not archive.history(url)
            archive.save_discovery(url, item)
            if is_new:
                report['new'].append((url, item))
                if len(report['new']) <= 3:
                    try:
                        full = await fetch_product(url)
                        archive.save(url, full)
                        archive.watch(url)
                    except (httpx.HTTPError, ValueError, KeyError):
                        # The saved search snapshot remains available, with its provenance.
                        pass
                    await asyncio.sleep(1)
    return reports
