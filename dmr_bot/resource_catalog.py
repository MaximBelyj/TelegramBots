"""Audited candidate inventory; only rows with tested adapters enter live search."""
from importlib.resources import files
import json

CATALOG = json.loads(files('dmr_bot').joinpath('resource_catalog.json').read_text())
CATALOG_ADAPTERS = {row["id"]: row for row in CATALOG["resources"] if row["status"] == "catalog_adapter"}
SUPPLIER_ADAPTERS = {row["id"]: row for row in CATALOG["resources"] if row["status"] == "supplier_adapter"}
NEW_ADAPTERS = {row['id']: row for row in CATALOG['resources'] if row['status'] == 'adapter_added'}


class SearchPolicyUnavailable(ValueError):
    pass


class SearchPolicy:
    """Cache public robots policy with wildcard support for every HTML route."""
    def __init__(self, clock):
        self.clock = clock
        self.cache = {}

    async def check(self, source, search_url, client):
        from urllib.parse import urlsplit
        from protego import Protego
        p = urlsplit(search_url)
        key = (source, p.netloc)
        stored = self.cache.get(key)
        if stored is None or self.clock() - stored[0] > 86400:
            response = await client.get(f'https://{p.netloc}/robots.txt', follow_redirects=False,
                                        headers={'User-Agent': 'CivilianProductSearchBot/0.2'})
            if response.status_code == 404:
                parser = None
            elif response.status_code == 200 and '<html' not in response.text[:1000].lower():
                parser = Protego.parse(response.text)
            else:
                raise SearchPolicyUnavailable('Не удалось подтвердить robots.txt; автоматический поиск приостановлен')
            stored = (self.clock(), parser)
            self.cache[key] = stored
        if stored[1] is not None and not stored[1].can_fetch(search_url, 'CivilianProductSearchBot/0.2'):
            raise SearchPolicyUnavailable('robots.txt запрещает путь поиска; нужен фид или согласованный доступ')

    def delay(self, source, url):
        from urllib.parse import urlsplit
        cached=self.cache.get((source,urlsplit(url).netloc))
        value=cached[1].crawl_delay('CivilianProductSearchBot/0.2') if cached and cached[1] is not None else None
        return float(value or 0)
