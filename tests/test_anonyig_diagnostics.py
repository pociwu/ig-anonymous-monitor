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

from ig_monitor import anonyig as anonyig_module
from ig_monitor.anonyig import AnonyIGScraper, SourceBlocked, _Session
from ig_monitor.config import BrowserConfig
from ig_monitor.models import ScrapeFailure


MARKER = "[ANONYIG-DIAG] "
SENSITIVE_VALUES = (
    "RAW_QUERY_TOKEN", "PRIVATE_TARGET", "RAW_COOKIE_VALUE", "RAW_HEADER_VALUE",
    "RAW_BODY_VALUE", "RAW_BODY_EXCEPTION", "RAW_UNKNOWN_ENDPOINT", "RAW_SITE_KEY",
)
DEFAULT = object()


class FakeResponse:
    def __init__(self, endpoint="stories", status=429, *, fail_json=False, release=None, payload=DEFAULT):
        self.url = f"https://anonyig.com/api/v1/instagram/{endpoint}?username=PRIVATE_TARGET&token=RAW_QUERY_TOKEN"
        self.status = status
        self.headers = {"Set-Cookie": "RAW_COOKIE_VALUE", "X-Private": "RAW_HEADER_VALUE"}
        self.request = SimpleNamespace(headers={"Cookie": "RAW_COOKIE_VALUE"}, post_data="RAW_BODY_VALUE")
        self.fail_json = fail_json
        self.release = release
        self.payload = {"debug": "RAW_BODY_VALUE"} if payload is DEFAULT else payload

    async def json(self):
        if self.release is not None:
            await self.release.wait()
        if self.fail_json:
            raise ValueError("RAW_BODY_EXCEPTION")
        return self.payload


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


def diagnostic(message, *, endpoint, status, block_type, since,
               verification_required=DEFAULT, error_type=None, classification_basis=None, body_state=None):
    prefix, separator, suffix = message.partition(MARKER)
    assert separator, "SourceBlocked must include the machine-readable diagnostic marker"
    assert "AnonyIG" in prefix
    assert any(phrase in prefix for phrase in ("停止所有來源請求", "不再發送請求", "未嘗試完成"))
    for secret in SENSITIVE_VALUES:
        assert secret not in message
    assert "https://" not in message
    assert "?username=" not in message
    data = json.loads(suffix)
    assert set(data) == {"source", "endpoint", "http_status", "block_type", "observed_at",
                         "verification_required", "error_type", "classification_basis", "body_state"}
    assert data["source"] == "anonyig"
    assert data["endpoint"] == endpoint
    assert data["http_status"] == status
    assert data["block_type"] == block_type
    if status is not None:
        expected_required, expected_error = {
            401: (None, "unauthorized"), 403: (None, "forbidden"),
            422: (True, "captcha_required"), 429: (True, "rate_limited"),
        }[status]
        expected_basis, expected_body = "frontend_http_status", "json"
    elif block_type == "visible_challenge":
        expected_required, expected_error = True, "captcha_required"
        expected_basis, expected_body = "visible_widget", "not_applicable"
    else:
        expected_required, expected_error = None, "unclassified"
        expected_basis, expected_body = "none", "not_applicable"
    assert data["verification_required"] is (expected_required if verification_required is DEFAULT else verification_required)
    assert data["error_type"] == (error_type or expected_error)
    assert data["classification_basis"] == (classification_basis or expected_basis)
    assert data["body_state"] == (body_state or expected_body)
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


def test_http_blocked_state_is_synchronous_and_unreadable_body_keeps_status():
    async def scenario():
        page = FakePage()
        session = _Session(page, 1)
        release = asyncio.Event()
        since = datetime.now(UTC)
        try:
            page.emit_response(FakeResponse("postsV2", 429, fail_json=True, release=release))
            assert session.blocked is True
            release.set()
            early = diagnostic(await block_message(session), endpoint="postsV2", status=429,
                               block_type="http_rate_limit", since=since, body_state="unreadable")
            await asyncio.gather(*list(session.tasks))
            after = diagnostic(await block_message(session), endpoint="postsV2", status=429,
                               block_type="http_rate_limit", since=since, body_state="unreadable")
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
                   block_type="http_unprocessable" if mode == "http" else "source_cooldown", since=since,
                   body_state="unreadable" if mode == "http" else "not_applicable")
        if mode == "guard":
            assert page.navigation_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(("status", "block_type", "expected_error", "expected_basis"), [
    (401, "http_unauthorized", "unauthorized", "frontend_http_status"),
    (403, "http_forbidden", "forbidden", "frontend_http_status"),
    (422, "http_unprocessable", "turnstile_required", "response_challenge"),
    (429, "http_rate_limit", "turnstile_required", "response_challenge"),
])
def test_valid_turnstile_object_refines_only_422_and_429_without_retaining_site_key(
    status, block_type, expected_error, expected_basis,
):
    async def scenario():
        page = FakePage()
        session = _Session(page, 1)
        since = datetime.now(UTC)
        payload = {"challenge": {"type": "turnstile", "siteKey": "RAW_SITE_KEY", "details": "RAW_BODY_VALUE"},
                   "message": "RAW_BODY_VALUE"}
        try:
            page.emit_response(FakeResponse("userInfo", status, payload=payload))
            diagnostic(await block_message(session), endpoint="userInfo", status=status, block_type=block_type,
                       since=since, error_type=expected_error, classification_basis=expected_basis)
            await asyncio.gather(*list(session.tasks))
            assert len(session.responses) == 1
            assert session.responses[0].payload is None
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    None, [], "RAW_BODY_VALUE", True,
    {"challenge": None}, {"challenge": []}, {"challenge": "RAW_BODY_VALUE"},
    {"challenge": {"type": "turnstile", "siteKey": ""}},
    {"challenge": {"type": "turnstile", "siteKey": 12}},
    {"challenge": {"type": "RAW_BODY_VALUE", "siteKey": "RAW_SITE_KEY"}},
    {"challenge": {"type": "turnstile", "site_key": "RAW_SITE_KEY"}},
    {"response": {"data": {"challenge": {"type": "turnstile", "siteKey": "RAW_SITE_KEY"}}}},
    {"error_type": "turnstile_required", "verification_required": False, "message": "RAW_BODY_VALUE"},
])
@pytest.mark.parametrize(("status", "block_type"), [(422, "http_unprocessable"), (429, "http_rate_limit")])
def test_nonobject_missing_or_unrecognized_body_values_use_only_fixed_fallback(payload, status, block_type):
    async def scenario():
        page = FakePage()
        session = _Session(page, 1)
        since = datetime.now(UTC)
        try:
            page.emit_response(FakeResponse(status=status, payload=payload))
            diagnostic(await block_message(session), endpoint="stories", status=status,
                       block_type=block_type, since=since)
            await asyncio.gather(*list(session.tasks))
            assert len(session.responses) == 1
            assert session.responses[0].payload is None
        finally:
            await session.close()

    asyncio.run(scenario())


def test_slow_first_body_times_out_once_and_late_body_cannot_change_frozen_diagnostic(monkeypatch):
    # Shorten only intentionally pending bodies, not successful classification:
    # a global 10 ms deadline makes ordinary JSON tests depend on OS scheduling.
    monkeypatch.setattr(anonyig_module, "_BLOCK_BODY_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        page = FakePage()
        session = _Session(page, 1)
        release = asyncio.Event()
        since = datetime.now(UTC)
        try:
            page.emit_response(FakeResponse("userInfo", 422, release=release,
                                           payload={"challenge": {"type": "turnstile", "siteKey": "RAW_SITE_KEY"}}))
            assert session.blocked is True
            first_message = await asyncio.wait_for(block_message(session), timeout=0.5)
            first = diagnostic(first_message, endpoint="userInfo", status=422,
                               block_type="http_unprocessable", since=since, body_state="timeout")
            # A later check must not wait for another body timeout, even if the
            # configured wait becomes much longer while that same body is pending.
            monkeypatch.setattr(anonyig_module, "_BLOCK_BODY_TIMEOUT_SECONDS", 10.0)
            repeated_message = await asyncio.wait_for(block_message(session), timeout=0.25)
            repeated = diagnostic(repeated_message, endpoint="userInfo", status=422,
                                  block_type="http_unprocessable", since=since, body_state="timeout")
            assert repeated == first
            release.set()
            await asyncio.gather(*list(session.tasks), return_exceptions=True)
            late = diagnostic(await block_message(session), endpoint="userInfo", status=422,
                              block_type="http_unprocessable", since=since, body_state="timeout")
            assert late == first
            assert all(response.payload is None for response in session.responses)
        finally:
            release.set()
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("first_has_turnstile", [False, True])
def test_first_header_keeps_its_own_body_when_later_response_body_finishes_first(first_has_turnstile):
    async def scenario():
        page = FakePage()
        session = _Session(page, 1)
        release_first = asyncio.Event()
        since = datetime.now(UTC)
        turnstile = {"challenge": {"type": "turnstile", "siteKey": "RAW_SITE_KEY"}}
        fallback = {"message": "RAW_BODY_VALUE"}
        try:
            page.emit_response(FakeResponse("userInfo", 422, release=release_first,
                                           payload=turnstile if first_has_turnstile else fallback))
            page.emit_response(FakeResponse("stories", 429,
                                           payload=fallback if first_has_turnstile else turnstile))
            await asyncio.sleep(0)  # The later body can finish; the first body cannot.
            release_first.set()
            diagnostic(await block_message(session), endpoint="userInfo", status=422,
                       block_type="http_unprocessable", since=since,
                       error_type="turnstile_required" if first_has_turnstile else "captcha_required",
                       classification_basis="response_challenge" if first_has_turnstile else "frontend_http_status")
            await asyncio.gather(*list(session.tasks))
            assert len(session.responses) == 2
            assert all(response.payload is None for response in session.responses)
        finally:
            release_first.set()
            await session.close()

    asyncio.run(scenario())


def test_successful_json_response_remains_available_to_collection_parser():
    async def scenario():
        page = FakePage()
        session = _Session(page, 1)
        payload = {"result": [{"synthetic": "public fixture"}]}
        try:
            page.emit_response(FakeResponse("stories", 200, payload=payload))
            await asyncio.gather(*list(session.tasks))
            await session.check_blocked()
            assert session.blocked is False
            assert len(session.responses) == 1
            assert session.responses[0].payload == payload
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("arrival", ["scroll", "sleep", "first_wheel"])
@pytest.mark.parametrize("body_ready", [False, True])
def test_next_posts_stops_at_new_block_before_any_later_scroll_action(monkeypatch, arrival, body_ready):
    if not body_ready:
        monkeypatch.setattr(anonyig_module, "_BLOCK_BODY_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        release = asyncio.Event()
        response = FakeResponse("postsV2", 429, release=None if body_ready else release)
        page = FakePage()
        page.wheels = []
        emitted = False
        original_sleep = asyncio.sleep

        def emit_once():
            nonlocal emitted
            if not emitted:
                emitted = True
                page.emit_response(response)

        class Footer:
            async def scroll_into_view_if_needed(self):
                if arrival == "scroll":
                    emit_once()

        regular_locator = page.locator
        page.locator = lambda selector: Footer() if selector == "footer" else regular_locator(selector)

        async def wheel(x, y):
            page.wheels.append((x, y))
            if arrival == "first_wheel" and len(page.wheels) == 1:
                emit_once()

        async def source_sleep(delay):
            if delay == 0.5 and arrival == "sleep":
                emit_once()
            await original_sleep(0)

        page.mouse = SimpleNamespace(wheel=wheel)
        monkeypatch.setattr(anonyig_module.asyncio, "sleep", source_sleep)
        session = _Session(page, 1)
        since = datetime.now(UTC)
        try:
            with pytest.raises(SourceBlocked) as error:
                await session.next_posts()
            assert len(page.wheels) == (1 if arrival == "first_wheel" else 0)
            diagnostic(str(error.value), endpoint="postsV2", status=429,
                       block_type="http_rate_limit", since=since,
                       body_state="json" if body_ready else "timeout")
        finally:
            release.set()
            await session.close()

    asyncio.run(scenario())


def test_guard_activated_during_widget_await_stops_tab_before_click():
    async def scenario():
        page = FakePage()
        guard_active = False
        clicks = []

        class WidgetLocator(FakeLocator):
            async def count(self):
                nonlocal guard_active
                guard_active = True
                return 0

        page.locator = lambda _selector: WidgetLocator(page)

        async def click():
            clicks.append("stories")

        page.get_by_role = lambda *_args, **_kwargs: SimpleNamespace(click=click)
        session = _Session(page, 1, lambda: guard_active)
        since = datetime.now(UTC)
        try:
            with pytest.raises(SourceBlocked) as error:
                await session.tab("stories", "stories")
            assert clicks == []
            diagnostic(str(error.value), endpoint="none", status=None,
                       block_type="source_cooldown", since=since)
        finally:
            await session.close()

    asyncio.run(scenario())


def test_profile_open_freezes_turnstile_diagnostic_before_session_cleanup():
    async def scenario():
        before_close = []

        class InspectClosePage(FakePage):
            def remove_listener(self, event, callback):
                # The callback belongs to the real session created by _open.
                before_close.append(dict(callback.__self__.block_diagnostic))
                super().remove_listener(event, callback)

        response = FakeResponse("userInfo", 422,
                                payload={"challenge": {"type": "turnstile", "siteKey": "RAW_SITE_KEY"}})
        page = InspectClosePage(response_on_search=response)
        adapter = AnonyIGScraper(BrowserConfig(True, 45, 0, Path("unused")))
        adapter._context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        since = datetime.now(UTC)
        with pytest.raises(ScrapeFailure) as error:
            await adapter._open("https://instagram.com/fixture_account/")
        final = diagnostic(str(error.value), endpoint="userInfo", status=422,
                           block_type="http_unprocessable", since=since,
                           error_type="turnstile_required", classification_basis="response_challenge")
        assert before_close == [final]
        assert error.value.blocker
        assert page.closed
        assert page.navigation_count == 1

    asyncio.run(scenario())


def test_cancelling_block_wait_propagates_and_close_cleans_capture_without_navigation(monkeypatch):
    monkeypatch.setattr(anonyig_module, "_BLOCK_BODY_TIMEOUT_SECONDS", 10.0)

    async def scenario():
        release = asyncio.Event()
        body_started = asyncio.Event()

        class PendingResponse(FakeResponse):
            async def json(self):
                body_started.set()
                return await super().json()

        page = FakePage()
        session = _Session(page, 1)
        waiter = None
        try:
            page.emit_response(PendingResponse("userInfo", 422, release=release))
            waiter = asyncio.create_task(session.check_blocked())
            await body_started.wait()
            await asyncio.sleep(0)
            assert not waiter.done()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert session.blocked is True
            assert session.tasks
            assert not any(task.cancelled() for task in session.tasks)
        finally:
            if waiter is not None and not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            release.set()
            await session.close()
        assert not session.tasks
        assert page.closed
        assert page.navigation_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["tab", "next_posts"])
def test_http_block_arriving_during_widget_await_prevents_collection_action(operation):
    async def scenario():
        page = FakePage()
        emitted = False
        actions = []

        class WidgetLocator(FakeLocator):
            async def count(self):
                nonlocal emitted
                if not emitted:
                    emitted = True
                    page.emit_response(FakeResponse("stories", 429))
                await asyncio.sleep(0)
                return 0

            async def scroll_into_view_if_needed(self):
                actions.append("scroll")

        async def click():
            actions.append("click")

        page.locator = lambda _selector: WidgetLocator(page)
        page.get_by_role = lambda *_args, **_kwargs: SimpleNamespace(click=click)
        session = _Session(page, 1)
        since = datetime.now(UTC)
        try:
            with pytest.raises(SourceBlocked) as error:
                if operation == "tab":
                    await session.tab("stories", "stories")
                else:
                    await session.next_posts()
            assert actions == []
            diagnostic(str(error.value), endpoint="stories", status=429,
                       block_type="http_rate_limit", since=since)
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("body_completes_before_check", [False, True])
def test_absolute_body_deadline_never_restarts_or_accepts_late_turnstile(monkeypatch, body_completes_before_check):
    clock = SimpleNamespace(now=100.0)
    # Replacing this module's reference leaves asyncio's real clock untouched.
    monkeypatch.setattr(anonyig_module, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def scenario():
        page = FakePage()
        session = _Session(page, 1)
        release = asyncio.Event()
        since = datetime.now(UTC)
        try:
            page.emit_response(FakeResponse("userInfo", 422, release=release,
                                           payload={"challenge": {"type": "turnstile", "siteKey": "RAW_SITE_KEY"}}))
            await asyncio.sleep(0)
            clock.now = 101.0
            if body_completes_before_check:
                release.set()
                await asyncio.gather(*list(session.tasks))
            # An expired absolute deadline must raise without even yielding.
            check = session.check_blocked()
            try:
                with pytest.raises(SourceBlocked) as error:
                    check.send(None)
            finally:
                check.close()
            first = diagnostic(str(error.value), endpoint="userInfo", status=422,
                               block_type="http_unprocessable", since=since, body_state="timeout")
            release.set()
            await asyncio.gather(*list(session.tasks), return_exceptions=True)
            repeated = diagnostic(await block_message(session), endpoint="userInfo", status=422,
                                  block_type="http_unprocessable", since=since, body_state="timeout")
            assert repeated == first
        finally:
            release.set()
            await session.close()

    asyncio.run(scenario())
