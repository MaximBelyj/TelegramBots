"""One-shot, ordinary visible-browser diagnostics. No Telegram, stealth or CAPTCHA solving."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from bs4 import BeautifulSoup
from .discovery import SOURCES, QUERY_PARAMS, SourceUnavailable, parse_search, search_url

CHECKS = {'radiokot': 'LM358', 'ozon': 'DMR', 'wb': 'DMR'}
CHALLENGE = ('captcha', 'капч', 'ddos-guard', 'just a moment', 'access denied',
             'доступ ограничен', 'доступ запрещ', 'проверка браузера', 'подтвердите, что вы не робот')


def inspect_document(source, html, url):
    """Return structural evidence only, never page text, form values or full URLs."""
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    path = urlsplit(url).path.lower()
    challenge = any(w in title for w in CHALLENGE) or 'captcha' in path or soup.select_one('form[action*="captcha"], iframe[src*="captcha"]') is not None
    login = any(w in title for w in ('вход в систему', 'авторизация', 'sign in', 'log in')) or any(w in path for w in ('/login', '/signin'))
    forms = []
    for form in soup.select('form')[:8]:
        fields = sorted({f.get('name') for f in form.select('input[name], select[name]') if f.get('name')})
        # Keep names to identify the actual forum search protocol, omit all values and tokens.
        forms.append({'action_path': urlsplit(form.get('action') or '').path,
                      'method': (form.get('method') or 'get').lower(), 'fields': fields})
    topic_links = soup.select('a[href*="viewtopic.php"]')
    topic_classes = sorted({c for a in topic_links for c in a.get('class', [])})
    result = {'classification': 'unknown_layout', 'candidate_count': None,
              'forms': forms, 'topic_link_count': len(topic_links), 'topic_link_classes': topic_classes,
              'topic_link_structures': [{'parent_tag': a.parent.name,
                 'parent_classes': a.parent.get('class', []),
                 'link_classes': a.get('class', [])} for a in topic_links[:8]]}
    if challenge:
        result['classification'] = 'challenge'
        return result
    if login:
        result['classification'] = 'login'
        return result
    # A category/homepage must not be called a successful search.
    values = parse_qs(urlsplit(url).query).get(QUERY_PARAMS[source], [])
    if not any(value.casefold().strip() == CHECKS[source].casefold() for value in values):
        has_search_form = source == 'radiokot' and any(set(f['fields']) & {'keywords', 'search_keywords'} for f in forms)
        result['classification'] = 'search_form_instead_of_results' if has_search_form else 'search_context_lost'
        return result
    try:
        items = parse_search(html, source, CHECKS[source], filter_results=False)
        result.update(classification='results' if items else 'explicit_empty', candidate_count=len(items))
    except SourceUnavailable as exc:
        if exc.code == 'challenge':
            result['classification'] = 'challenge'
        elif source == 'radiokot' and topic_links:
            result['classification'] = 'forum_layout_unrecognized'
        elif source == 'radiokot' and any(set(f['fields']) & {'keywords', 'search_keywords'} for f in forms):
            result['classification'] = 'search_form_instead_of_results'
    return result


def summarize_navigation(source, html, url, statuses, navigation_error=None):
    result = inspect_document(source, html, url)
    # A rendered blocked document is not a successful JS-rendered result.
    last = statuses[-1]['status'] if statuses else None
    if last in (401, 403, 429, 498):
        result['classification'] = f'http_{last}'
    if navigation_error:
        result['navigation_error'] = navigation_error
        if result['classification'] not in ('challenge', 'login', 'http_401', 'http_403', 'http_429', 'http_498'):
            result['classification'] = 'navigation_incomplete'
    result['document_responses'] = statuses
    return result


async def check_source(browser, source, interactive=True, wait_seconds=8):
    context = await browser.new_context()  # No personal/persistent profile or stored credentials.
    page = await context.new_page()
    statuses = []
    denied = False

    async def guard(route):
        if denied and route.request.resource_type == 'document':
            await route.abort()
        else:
            await route.continue_()

    await context.route('**/*', guard)

    def response_seen(response):
        nonlocal denied
        req = response.request
        if req.resource_type == 'document' and req.frame == page.main_frame:
            p = urlsplit(response.url)
            statuses.append({'status': response.status, 'host': p.hostname, 'path': p.path})
            if response.status in (401, 403, 429, 498):
                denied = True

    page.on('response', response_seen)
    try:
        error = None
        try:
            await page.goto(search_url(source, CHECKS[source]), wait_until='domcontentloaded', timeout=35000)
        except Exception as exc:
            error = type(exc).__name__  # Exception messages may include sensitive navigation details.
        await page.wait_for_timeout(wait_seconds * 1000)
        html = await page.content()
        first = summarize_navigation(source, html, page.url, statuses, error)
        if interactive:
            print(f'\n{SOURCES[source][0]}: страница открыта. Не входите в аккаунт и не решайте CAPTCHA.', flush=True)
            # Never prompt to clear/solve the challenge, nor automatically retry it.
            await asyncio.to_thread(input, 'Посмотрите страницу и нажмите Enter в Терминале, чтобы продолжить: ')
        if first['classification'] in ('challenge', 'login', 'http_401', 'http_403', 'http_429', 'http_498'):
            return first
        # A second DOM snapshot is observation of the same navigation, not a new request/retry.
        return summarize_navigation(source, await page.content(), page.url, statuses, error)
    finally:
        await context.close()


async def run(args):
    from playwright.async_api import async_playwright
    report = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'mode': 'headless_test' if args.headless else 'visible_browser',
              'note': 'Browser diagnostic only; not proof of permitted or stable automatic access.', 'sources': {}}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless)
        try:
            for source in args.sources:
                try:
                    row = await check_source(browser, source, interactive=not args.headless, wait_seconds=args.wait)
                except Exception as exc:
                    row = {'classification': 'diagnostic_error', 'error_type': type(exc).__name__}
                report['sources'][source] = row
                print(SOURCES[source][0] + ': ' + row['classification'], flush=True)
        finally:
            await browser.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f'Отчёт: {args.output}', flush=True)


def main():
    parser = argparse.ArgumentParser(description='Browser-only diagnostics, no Telegram credentials needed.')
    parser.add_argument('--sources', nargs='+', choices=CHECKS, default=list(CHECKS))
    parser.add_argument('--output', type=Path, default=Path('data/macos-browser-report.json'))
    parser.add_argument('--headless', action='store_true', help='For isolated tests; does not represent the visible Mac browser.')
    parser.add_argument('--wait', type=int, default=8, choices=range(0, 31), metavar='0..30')
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print('Проверка остановлена.')
    except Exception as exc:
        print(f'Не удалось запустить браузер: {type(exc).__name__}. Выполните помощник macos-check.sh browser для установки компонентов.')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
