import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from dmr_bot.discovery import SourceUnavailable, fetch_search
from dmr_bot.search_engine import SearchEngine

HTML = '<a href="/cgi-bin/qwery.pl/id=123">LM358</a>'


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'health.json'
        self.now = 1000000.
        self.sleeps = []
        self.engines = []

    async def asyncTearDown(self):
        for e in self.engines:
            await e.close()
        self.folder.cleanup()

    def engine(self, handler):
        async def sleep(delay):
            self.sleeps.append(delay)
            self.now += delay
        e = SearchEngine(self.path, clock=lambda: self.now, sleeper=sleep,
                         client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw))
        self.engines.append(e)
        return e

    async def test_blocked_site_not_retried_for_each_item_or_after_restart(self):
        calls = []
        def blocked(request):
            calls.append(request)
            return httpx.Response(403)
        e = self.engine(blocked)
        with self.assertRaises(SourceUnavailable):
            await e.fetch('platan', 'LM358', probe=True)
        self.assertEqual(e.state('platan')['status'], 'manual')
        await e.discover('Стилус', ['platan'])
        fresh = self.engine(blocked)
        await fresh.discover('Клапан', ['platan'])
        self.assertEqual(len(calls), 1)
        self.assertNotIn('LM358', self.path.read_text())

    async def test_rate_limit_respects_retry_after_and_recovers(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={'retry-after': '90000'}) if len(calls) == 1 else httpx.Response(200, text=HTML)
        e = self.engine(handler)
        with self.assertRaises(SourceUnavailable):
            await e.fetch('platan', 'LM358', probe=True)
        self.assertGreaterEqual(e.state('platan')['retry_at'], self.now + 90000)
        with self.assertRaises(SourceUnavailable):
            await e.fetch('platan', 'LM358')
        self.now += 90001
        self.assertEqual(len(await e.fetch('platan', 'LM358')), 1)
        self.assertEqual(e.state('platan')['status'], 'ready')

    async def test_per_source_spacing_and_cookie_session_reuse(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, text=HTML, headers={'set-cookie': 'ordinary=ok; Path=/'})
        e = self.engine(handler)
        await e.fetch('platan', 'LM358', probe=True)
        await e.fetch('platan', 'LM358')
        self.assertEqual(self.sleeps, [10])
        self.assertIn('ordinary=ok', calls[1].headers.get('cookie', ''))
        self.assertNotIn('ordinary', self.path.read_text())

    async def test_spacing_survives_restart(self):
        first = self.engine(lambda request: httpx.Response(200, text=HTML))
        await first.fetch('platan', 'LM358', probe=True)
        fresh = self.engine(lambda request: httpx.Response(200, text=HTML))
        await fresh.fetch('platan', 'LM358')
        self.assertEqual(self.sleeps, [10])

    async def test_cancelled_queue_does_not_block_next_request(self):
        e = self.engine(lambda request: httpx.Response(200, text=HTML))
        await e.fetch('platan', 'LM358', probe=True)
        gate = asyncio.Event()
        entered = asyncio.Event()
        async def waiting(delay):
            entered.set()
            await gate.wait()
        e.sleep = waiting
        task = asyncio.create_task(e.fetch('platan', 'LM358'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.now += 11
        self.assertEqual(len(await e.fetch('platan', 'LM358')), 1)

    async def test_unchecked_site_is_not_silently_requested(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, text=HTML)
        e = self.engine(handler)
        report = (await e.discover('LM358', ['platan']))[0]
        self.assertEqual(report['access_status'], 'unknown')
        self.assertTrue(report['error'])
        self.assertEqual(calls, [])


class RedirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_cookie_redirect_preserves_search(self):
        calls = []
        def handler(request):
            calls.append(request)
            if 'ordinary=ok' not in request.headers.get('cookie', ''):
                return httpx.Response(302, headers={'location': str(request.url), 'set-cookie': 'ordinary=ok; Path=/'})
            return httpx.Response(200, text=HTML)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            items = await fetch_search('platan', 'LM358', client=client)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(calls), 2)

    async def test_redirect_dropping_query_is_not_success(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={'location': '/category/audio/'}))) as client:
            with self.assertRaises(SourceUnavailable) as error:
                await fetch_search('ozon', 'DMR', client=client)
        self.assertEqual(error.exception.code, 'redirect')

    async def test_captcha_and_unsafe_redirect_not_followed(self):
        for destination in ('/showcaptcha?token=private', 'https://attacker.org/search?text=DMR'):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(302, headers={'location': destination})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(ValueError):
                    await fetch_search('market', 'DMR', client=client)
            self.assertEqual(len(calls), 1)

    async def test_redirect_cycle_has_specific_diagnosis(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={'location': str(r.url)}))) as client:
            with self.assertRaises(SourceUnavailable) as error:
                await fetch_search('platan', 'LM358', client=client)
        self.assertEqual(error.exception.code, 'redirect')
