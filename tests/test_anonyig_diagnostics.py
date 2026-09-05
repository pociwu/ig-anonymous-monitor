"""Offline regression contract for safe anonymous-source block diagnostics.

The agreed seams are response notification -> _Session.check_blocked and the
existing _open exception wrapper. Fakes replace only the browser boundary; no
browser, socket, project config, or production state is accessed.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ig_monitor.anonyig import AnonyIGScraper, SourceBlocked, _Session
from ig_monitor.config import BrowserConfig
from ig_monitor.models import ScrapeFailure


MARKER = "[ANONYIG-DIAG] "
SENSITIVE_VALUES = (
    "RAW_QUERY_TOKEN", "PRIVATE_TARGET", "RAW_COOKIE_VALUE", "RAW_HEADER_VALUE",
    "RAW_BODY_VALUE", "RAW_BODY_EXCEPTION", "RAW_UNKNOWN_ENDPOINT",
)


class FakeResponse:
    def __init__(self, endpoint="stories", status=429, *, fail_json=False, release=None):
        self.url = f"https://anonyig.com/api/v1/instagram/{endpoint}?username=PRIVATE_TARGET&token=RAW_QUERY_TOKEN"
        self.status = status
        self.headers = {"Set-Cookie": "RAW_COOKIE_VALUE", "X-Private": "RAW_HEADER_VALUE"}
        self.request = SimpleNamespace(headers={"Cookie": "RAW_COOKIE_VALUE"}, post_data="RAW_BODY_VALUE")
        self.fail_json = fail_json
        self.release = release

    async def json(self):
        if self.release is not None:
            await self.release.wait()
        if self.fail_json:
            raise ValueError("RAW_BODY_EXCEPTION")
        return {"debug": "RAW_BODY_VALUE"}


class FakeLocator:
    def __init__(self, page, visible=False):
        self.page = page
        self.visible = visible
        self.first = self

    async def count(self):
        return int(self.visible)

    async def is_visible(self):
        return self.visible

    async def fill(self, _value):
        pass

    async def press(self, _key):
        if self.page.response_on_search is not None:
            self.page.emit_response(self.page.response_on_search)


class FakePage:
    def __init__(self, *, visible_challenge=False, response_on_search=None):
        self.visible_challenge = visible_challenge
        self.response_on_search = response_on_search
        self.listeners = {}
        self.navigation_count = 0
        self.closed = False

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, _callback):
        self.listeners.pop(event, None)

    def emit_response(self, response):
        self.listeners["response"](response)

    def locator(self, selector):
        return FakeLocator(self, self.visible_challenge and "challenges.cloudflare.com" in selector)

    def set_default_timeout(self, _timeout):
        pass

    async def goto(self, _url, **_kwargs):
        self.navigation_count += 1

    def get_by_placeholder(self, _placeholder, **_kwargs):
        return FakeLocator(self)

    async def close(self):
        self.closed = True


def diagnostic(message, *, endpoint, status, block_type, since):
    prefix, separator, suffix = message.partition(MARKER)
    assert separator, "SourceBlocked must include the machine-readable diagnostic marker"
    assert "AnonyIG" in prefix
    assert any(phrase in prefix for phrase in ("停止所有來源請求", "不再發送請求", "未嘗試完成"))
    for secret in SENSITIVE_VALUES:
        assert secret not in message
    assert "https://" not in message
    assert "?username=" not in message
    data = json.loads(suffix)
    assert set(data) == {"source", "endpoint", "http_status", "block_type", "observed_at"}
    assert data["source"] == "anonyig"
    assert data["endpoint"] == endpoint
    assert data["http_status"] == status
    assert data["block_type"] == block_type
    observed = datetime.fromisoformat(data["observed_at"])
    assert observed.tzinfo is not None
    assert observed.utcoffset() == timedelta(0)
    assert since - timedelta(seconds=1) <= observed <= datetime.now(UTC)
    return data


async def block_message(session):
    with pytest.raises(SourceBlocked) as error:
        await session.check_blocked()
    return str(error.value)


@pytest.mark.parametrize(("status", "block_type"), [
    (401, "http_unauthorized"), (403, "http_forbidden"),
    (422, "http_unprocessable"), (429, "http_rate_limit"),
])
def test_http_block_exposes_safe_status_and_reason(status, block_type):
    async def scenario():
        page = FakePage()
        session = _Session(page, 1)
        since = datetime.now(UTC)
        try:
            page.emit_response(FakeResponse(status=status))
            return diagnostic(await block_message(session), endpoint="stories", status=status,
                              block_type=block_type, since=since)
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("endpoint", ["userInfo", "postsV2", "posts", "stories", "highlights", "highlightStories"])
def test_known_endpoint_names_survive_without_preserving_request_url(endpoint):
    async def scenario():
        page = FakePage()
        session = _Session(page, 1)
        since = datetime.now(UTC)
        try:
            page.emit_response(FakeResponse(endpoint, 403))
            diagnostic(await block_message(session), endpoint=endpoint, status=403,
                       block_type="http_forbidden", since=since)
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("endpoint", ["RAW_UNKNOWN_ENDPOINT", "page", "none", "nested/userInfo", "userInfo/"])
def test_unknown_api_path_is_reduced_to_fixed_safe_label(endpoint):
    async def scenario():
        page = FakePage()
        session = _Session(page, 1)
        since = datetime.now(UTC)
        try:
            page.emit_response(FakeResponse(endpoint, 422))
            diagnostic(await block_message(session), endpoint="unknown_api", status=422,
                       block_type="http_unprocessable", since=since)
        finally:
            await session.close()

    asyncio.run(scenario())


def test_status_diagnostic_is_available_before_body_and_survives_body_failure():
    async def scenario():
        page = FakePage()
        session = _Session(page, 1)
        release = asyncio.Event()
        since = datetime.now(UTC)
        try:
            page.emit_response(FakeResponse("postsV2", 429, fail_json=True, release=release))
            early = diagnostic(await block_message(session), endpoint="postsV2", status=429,
                               block_type="http_rate_limit", since=since)
            release.set()
            await asyncio.gather(*list(session.tasks))
            after = diagnostic(await block_message(session), endpoint="postsV2", status=429,
                               block_type="http_rate_limit", since=since)
            assert after == early
        finally:
            release.set()
            await session.close()

    asyncio.run(scenario())


def test_first_http_block_is_not_replaced_by_later_status_widget_or_cooldown():
    async def scenario():
        page = FakePage()
        session = _Session(page, 1)
        since = datetime.now(UTC)
        try:
            page.emit_response(FakeResponse("userInfo", 401))
            first = diagnostic(await block_message(session), endpoint="userInfo", status=401,
                               block_type="http_unauthorized", since=since)
            page.emit_response(FakeResponse("stories", 429))
            page.visible_challenge = True
            session.guard = lambda: True
            repeated = diagnostic(await block_message(session), endpoint="userInfo", status=401,
                                  block_type="http_unauthorized", since=since)
            assert repeated == first
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(("mode", "endpoint", "block_type"), [
    ("widget", "page", "visible_challenge"), ("guard", "none", "source_cooldown"),
])
def test_local_block_has_no_http_status_and_remains_first_event(mode, endpoint, block_type):
    async def scenario():
        page = FakePage(visible_challenge=mode == "widget")
        session = _Session(page, 1, guard=(lambda: True) if mode == "guard" else None)
        since = datetime.now(UTC)
        try:
            first = diagnostic(await block_message(session), endpoint=endpoint, status=None,
                               block_type=block_type, since=since)
            page.visible_challenge = False
            session.guard = None
            page.emit_response(FakeResponse("highlights", 403))
            later = diagnostic(await block_message(session), endpoint=endpoint, status=None,
                               block_type=block_type, since=since)
            assert later == first
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["http", "guard"])
def test_profile_open_preserves_diagnostic_in_existing_scrape_failure_wrapper(mode):
    async def scenario():
        response = FakeResponse("userInfo", 422, fail_json=True) if mode == "http" else None
        page = FakePage(response_on_search=response)
        adapter = AnonyIGScraper(BrowserConfig(True, 45, 0, Path("unused")))
        adapter._context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        if mode == "guard":
            adapter.source_guard = lambda: True
        since = datetime.now(UTC)
        with pytest.raises(ScrapeFailure) as error:
            await adapter._open("https://instagram.com/fixture_account/")
        assert error.value.blocker
        assert page.closed
        diagnostic(str(error.value), endpoint="userInfo" if mode == "http" else "none",
                   status=422 if mode == "http" else None,
                   block_type="http_unprocessable" if mode == "http" else "source_cooldown", since=since)
        if mode == "guard":
            assert page.navigation_count == 0

    asyncio.run(scenario())
