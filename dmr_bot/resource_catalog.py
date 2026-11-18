"""Audited candidate inventory; only rows with tested adapters enter live search."""
from importlib.resources import files
import json

CATALOG = json.loads(files('dmr_bot').joinpath('resource_catalog.json').read_text())
NEW_ADAPTERS = {row['id']: row for row in CATALOG['resources'] if row['status'] == 'adapter_added'}


class SearchPolicyUnavailable(ValueError):
    pass


class SearchPolicy:
    """Cache new providers' public robots policy; uncertainty prevents automatic search."""
    def __init__(self, clock):
        self.clock = clock
        self.cache = {}

    async def check(self, source, search_url, client):
        if source not in NEW_ADAPTERS:
            return
        from urllib.parse import urlsplit
        from urllib.robotparser import RobotFileParser
        p = urlsplit(search_url)
        key = (source, p.netloc)
        stored = self.cache.get(key)
        if stored is None or self.clock() - stored[0] > 86400:
            response = await client.get(f'https://{p.netloc}/robots.txt', follow_redirects=False,
                                        headers={'User-Agent': 'CivilianProductSearchBot/0.1'})
            if response.status_code == 404:
                parser = None
            elif response.status_code == 200 and '<html' not in response.text[:1000].lower():
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
            else:
                raise SearchPolicyUnavailable('Не удалось подтвердить robots.txt; автоматический поиск приостановлен')
            stored = (self.clock(), parser)
            self.cache[key] = stored
        if stored[1] is not None and not stored[1].can_fetch('CivilianProductSearchBot/0.1', search_url):
            raise SearchPolicyUnavailable('robots.txt запрещает путь поиска; нужен фид или согласованный доступ')
