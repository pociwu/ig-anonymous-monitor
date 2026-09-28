"""Run in the OCI browser image with --network none; no provider requests."""
import asyncio
from types import SimpleNamespace

from ig_monitor.browser_media import BrowserMedia
from ig_monitor.models import ScrapeFailure


async def main():
    config = SimpleNamespace(headless=True)
    data = b'\x00\x00\x00\x18ftypisomfixture'
    state = {'status': 200, 'body': data, 'mime': 'video/mp4'}
    guard = {'blocked': False}
    requests = []

    def check():
        if guard['blocked']:
            raise ScrapeFailure('cooldown', 'test', blocker='source_cooldown')

    transport = BrowserMedia(config, check, 1024)
    original_route = transport._route

    async def routed(route):
        async def fulfill():
            requests.append(route.request.url)
            if route.request.url.endswith('/'):
                await route.fulfill(status=200, content_type='text/html', body='<body>Fixture</body>')
            else:
                await route.fulfill(status=state['status'], content_type=state['mime'], body=state['body'])
        await original_route(SimpleNamespace(request=route.request, abort=route.abort, continue_=fulfill))

    transport._route = routed
    try:
        assert await transport.download('https://s.cdninstagram.com/fixture') == (data, 'video/mp4')
        before = len(requests)
        guard['blocked'] = True
        try:
            await transport.download('https://s.cdninstagram.com/fixture')
            raise AssertionError('cooldown allowed request')
        except ScrapeFailure as exc:
            assert exc.blocker == 'source_cooldown'
        assert len(requests) == before
        guard['blocked'] = False
        for status, body, mime in [(206, data, 'video/mp4'), (200, b'x'*1025, 'video/mp4'),
                                   (200, b'verify you are human', 'text/html'), (429, b'', 'text/plain')]:
            state.update(status=status, body=body, mime=mime)
            transport._denied = False  # separate independent fixtures, not production recovery
            try:
                await transport.download('https://s.cdninstagram.com/fixture')
                raise AssertionError('invalid response accepted')
            except ScrapeFailure as exc:
                if status == 429 or mime == 'text/html':
                    assert exc.blocker == 'source_blocked'
                assert 'phase=media_proxy' in str(exc) or status == 200 and mime == 'video/mp4'
        await transport.close()
        async def homepage_denied(route):
            await route.fulfill(status=403, content_type='text/plain', body='denied')
        transport._route = homepage_denied
        transport._denied = False
        try:
            await transport.download('https://s.cdninstagram.com/fixture')
            raise AssertionError('homepage denial accepted')
        except ScrapeFailure as exc:
            assert exc.blocker == 'source_blocked'
            assert 'phase=homepage' in str(exc)
        print('BROWSER_MEDIA_OFFLINE_OK complete_200 cooldown_zero_requests partial_rejected size_bounded challenge_stop rate_limit_stop')
    finally:
        await transport.close()


if __name__ == '__main__':
    asyncio.run(main())
