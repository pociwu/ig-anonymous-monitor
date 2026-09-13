import asyncio
from pathlib import Path

import httpx
import pytest

from ig_monitor.config import BrowserConfig
from ig_monitor.igwatcher import IGWatcherScraper
from ig_monitor.models import ScrapeFailure


START = "https://scontent.cdninstagram.com/start?secret=token"
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"test video"


def run_download(respond):
    async def run():
        config = BrowserConfig(True, 15, 0, Path("unused"), anonymous_source="igwatcher")
        async with IGWatcherScraper(config, transport=httpx.MockTransport(respond)) as scraper:
            return await scraper.download(START, "https://igwatcher.com/")
    return asyncio.run(run())


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", ["/final", "https://video.fbcdn.net/final"])
def test_safe_redirect_downloads_without_cookies(status, location):
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, headers={"Location": location, "Set-Cookie": "session=secret; Path=/"})
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        assert request.headers["referer"] == "https://igwatcher.com/"
        return httpx.Response(200, content=VIDEO, headers={"Content-Type": "video/mp4"})
    assert run_download(respond) == (VIDEO, "video/mp4")
    assert len(requests) == 2


@pytest.mark.parametrize("location", ["http://video.fbcdn.net/x", "https://127.0.0.1/x",
    "https://cdninstagram.com.evil.example/x", "https://user:secret@video.fbcdn.net/x",
    "https://video.fbcdn.net:444/x", "https://example.com/x", "https://[::1]/x", ""])
def test_unsafe_redirect_never_requested(location):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": location})
    with pytest.raises(ScrapeFailure) as error:
        run_download(respond)
    assert len(requests) == 1
    assert "http_status=302" in str(error.value)
    assert "secret" not in str(error.value)


def test_redirect_chain_is_bounded():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": f"/step{len(requests)}"})
    with pytest.raises(ScrapeFailure):
        run_download(respond)
    assert len(requests) == 4


def test_redirect_challenge_still_blocks():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(302, content=b"cf-chl-test", headers={"Location": "/final"})
    with pytest.raises(ScrapeFailure) as error:
        run_download(respond)
    assert error.value.blocker == "source_blocked"
    assert len(requests) == 1


def test_second_redirect_is_validated_before_request():
    requests = []
    def respond(request):
        requests.append(request)
        target = "/next" if len(requests) == 1 else "http://169.254.169.254/secret"
        return httpx.Response(302, headers={"Location": target})
    with pytest.raises(ScrapeFailure):
        run_download(respond)
    assert len(requests) == 2


@pytest.mark.parametrize("status", [401, 403, 422, 429])
def test_redirect_target_block_stops_source(status):
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": "/next"})
        return httpx.Response(status)
    with pytest.raises(ScrapeFailure) as error:
        run_download(respond)
    assert error.value.blocker == "source_blocked"
    assert len(requests) == 2


def test_redirect_final_payload_still_validated():
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": "/next"})
        return httpx.Response(200, content=b"secret html", headers={"Content-Type": "video/mp4"})
    with pytest.raises(ScrapeFailure) as error:
        run_download(respond)
    assert "secret" not in str(error.value)
    assert len(requests) == 2


def test_redirects_share_total_timeout():
    requests = []
    async def respond(request):
        requests.append(request)
        await asyncio.sleep(0.07)
        return httpx.Response(302, headers={"Location": f"/next{len(requests)}"})
    async def run():
        config = BrowserConfig(True, 0.12, 0, Path("unused"), anonymous_source="igwatcher")
        async with IGWatcherScraper(config, transport=httpx.MockTransport(respond)) as scraper:
            await scraper.download(START, "https://igwatcher.com/")
    with pytest.raises(ScrapeFailure, match="逾時"):
        asyncio.run(run())
    assert len(requests) <= 2
