"""Read public product metadata without bypassing access restrictions."""
import json
from urllib.parse import urlsplit, urlunsplit
import httpx
from bs4 import BeautifulSoup

HOSTS = {'avito.ru', 'www.avito.ru', 'ozon.ru', 'www.ozon.ru',
         'wildberries.ru', 'www.wildberries.ru', 'market.yandex.ru',
         'chipdip.ru', 'www.chipdip.ru', 'platan.ru', 'www.platan.ru',
         'promelec.ru', 'www.promelec.ru', 'qrz.ru', 'www.qrz.ru',
         'radiokot.ru', 'www.radiokot.ru'}


def validate_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname not in HOSTS or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Нужна HTTPS-ссылка на одну из подключённых площадок.')
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ''))


def parse_product(html):
    soup = BeautifulSoup(html, 'html.parser')
    products = []

    def walk(node):
        if isinstance(node, list):
            for entry in node:
                walk(entry)
        elif isinstance(node, dict):
            types = node.get('@type', [])
            if isinstance(types, str):
                types = [types]
            if 'Product' in types:
                products.append(node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

    for script in soup.find_all('script', type='application/ld+json'):
        try:
            walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
    if not products:
        raise ValueError('Страница не содержит доступных данных товара; возможна защита площадки. Архив не изменён.')
    p = products[0]
    offers = p.get('offers') or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}
    return {'title': p.get('name'), 'description': p.get('description'),
            'images': p.get('image', []), 'price': offers.get('price', offers.get('lowPrice')),
            'currency': offers.get('priceCurrency'), 'seller': offers.get('seller', p.get('seller')),
            'published_at': p.get('datePublished', offers.get('datePublished')),
            'availability': offers.get('availability'), 'structured_data': p}


async def fetch_product(url):
    url = validate_url(url)
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        # Validate every redirect to avoid requests to arbitrary destinations.
        for _ in range(5):
            async with client.stream('GET', url, headers={'User-Agent': 'DMRArchiveBot/0.1'}) as response:
                if response.is_redirect:
                    url = validate_url(str(response.url.join(response.headers['location'])))
                    continue
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 5_000_000:
                        raise ValueError('Страница слишком большая.')
                return parse_product(data.decode('utf-8', errors='replace'))
        raise ValueError('Слишком много перенаправлений.')
