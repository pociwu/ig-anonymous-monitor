import asyncio
import base64
from dataclasses import replace
from pathlib import Path

import pytest

from ig_monitor.config import BrowserConfig
from ig_monitor.igwatcher import IGWatcherScraper
from ig_monitor.models import ScrapeFailure


def test_browser_media_is_opt_in_and_preserves_validation(monkeypatch):
    from ig_monitor.browser_media import BrowserMedia
    calls = []

    async def download(self, url):
        calls.append(url)
        return b'\x00\x00\x00\x18ftypisomdata', 'video/mp4'

    monkeypatch.setattr(BrowserMedia, 'download', download)
    cfg = BrowserConfig(True, 30, 0, Path('.'), 'igwatcher')
    assert cfg.igwatcher_media_transport == 'http'
    cfg = replace(cfg, igwatcher_media_transport='browser_proxy')

    async def run():
        async with IGWatcherScraper(cfg) as scraper:
            with pytest.raises(ScrapeFailure):
                await scraper.download('https://127.0.0.1/private', '')
            assert calls == []
            scraper.source_guard = lambda: True
            with pytest.raises(ScrapeFailure):
                await scraper.download('https://s.cdninstagram.com/a', '')
            assert calls == []
            scraper.source_guard = lambda: False
            body, mime = await scraper.download('https://s.cdninstagram.com/a', '')
            assert mime == 'video/mp4' and body[4:8] == b'ftyp'
    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize('status', [401, 403, 422, 429])
def test_proxy_denial_is_global_stop(status):
    from ig_monitor.browser_media import decode_result
    with pytest.raises(ScrapeFailure) as exc:
        decode_result({'status': status}, 100)
    assert exc.value.blocker == 'source_blocked'


@pytest.mark.parametrize('phase', ['homepage', 'media_proxy', 'https://secret.invalid/token'])
def test_denial_diagnostic_identifies_phase_without_reflecting_arbitrary_input(phase):
    from ig_monitor.browser_media import decode_result
    with pytest.raises(ScrapeFailure) as exc:
        decode_result({'status': 403}, 100, phase=phase)
    expected = phase if phase in ('homepage', 'media_proxy') else 'unknown'
    assert f'phase={expected}' in str(exc.value)
    assert 'http_status=403' in str(exc.value)
    assert 'secret.invalid' not in str(exc.value)


@pytest.mark.parametrize('result', [
    {'status': 206}, {'status': 302}, {'status': 404},
    {'status': 200, 'error': 'size'},
    {'status': 200, 'mime': 'text/html', 'body': base64.b64encode(b'<html>').decode()},
    {'status': 200, 'mime': 'video/mp4', 'body': '???'},
])
def test_incomplete_or_invalid_proxy_response_fails(result):
    from ig_monitor.browser_media import decode_result
    with pytest.raises(ScrapeFailure):
        decode_result(result, 100)


def test_proxy_decode_size_and_challenge():
    from ig_monitor.browser_media import decode_result
    data = b'video bytes'
    result = {'status': 200, 'mime': 'video/mp4', 'body': base64.b64encode(data).decode()}
    assert decode_result(result, 100) == (data, 'video/mp4')
    with pytest.raises(ScrapeFailure):
        decode_result(result, 2)
    with pytest.raises(ScrapeFailure) as exc:
        decode_result({'status': 200, 'challenge': True}, 100)
    assert exc.value.blocker == 'source_blocked'


def test_browser_block_stops_subsequent_api(monkeypatch):
    from ig_monitor.browser_media import BrowserMedia
    async def blocked(self, url):
        raise ScrapeFailure('blocked', 'test', blocker='source_blocked')
    monkeypatch.setattr(BrowserMedia, 'download', blocked)
    cfg = BrowserConfig(True, 30, 0, Path('.'), 'igwatcher', igwatcher_media_transport='browser_proxy')
    async def run():
        async with IGWatcherScraper(cfg) as scraper:
            with pytest.raises(ScrapeFailure):
                await scraper.download('https://s.cdninstagram.com/a', '')
            with pytest.raises(ScrapeFailure) as exc:
                scraper._guard()
            assert exc.value.blocker == 'source_cooldown'
    asyncio.run(run())


def test_route_blocks_unrelated_requests_and_redirects():
    from types import SimpleNamespace
    from ig_monitor.browser_media import BrowserMedia
    transport = BrowserMedia(None, lambda: None, 100)
    transport._allowed_media = 'https://igwatcher.com/wp-json/igw/v1/media?u=fixture'
    async def run():
        for url, method, allowed in [
            ('https://igwatcher.com/', 'GET', True),
            (transport._allowed_media, 'GET', True),
            (transport._allowed_media, 'POST', False),
            ('https://igwatcher.com/wp-json/igw/v1/stories', 'GET', False),
            ('https://instagram.com/', 'GET', False),
            ('https://127.0.0.1/', 'GET', False),
        ]:
            actions = []
            async def abort(): actions.append(False)
            async def proceed(): actions.append(True)
            route = SimpleNamespace(request=SimpleNamespace(url=url, method=method),
                                    abort=abort, continue_=proceed)
            await transport._route(route)
            assert actions == [allowed]
    asyncio.run(run())
