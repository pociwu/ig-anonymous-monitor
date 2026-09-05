"""Offline browser-boundary tests for the explicitly isolated comparison probe."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ig_monitor import browser_probe


PROFILE = {"result": [{"user": {
    "id": "100", "username": "nasa", "media_count": 1, "follower_count": 2,
    "following_count": 0, "is_private": False,
    "profile_pic_url": "https://media.example.test/avatar.jpg",
}}]}
POSTS = {"result": {"edges": [], "page_info": {"has_next_page": False, "end_cursor": None}}}
TURNSTILE = {"challenge": {"type": "turnstile", "siteKey": "SECRET_SITE_KEY"}}


class FakeRequest:
    def __init__(self, endpoint):
        self.url = f"https://anonyig.com/api/v1/instagram/{endpoint}?secret=SECRET_QUERY"


class FakeResponse:
    def __init__(self, request, status, payload, *, gate=None, unreadable=False):
        self.request = request
        self.url = request.url
        self.status = status
        self.payload = payload
        self.gate = gate
        self.unreadable = unreadable

    async def json(self):
        if self.gate is not None:
            await self.gate.wait()
        if self.unreadable:
            raise ValueError("SECRET_BODY_EXCEPTION")
        return self.payload


class FakeRoute:
    def __init__(self, request):
        self.request = request
        self.allowed = False
        self.aborted = False

    async def fallback(self):
        self.allowed = True

    async def abort(self, *_args):
        self.aborted = True


class FakePage:
    def __init__(self, script=None, *, widget=False):
        self.script = script
        self.widget = widget
        self.listeners = {}
        self.route_handler = None
        self.routes = []
        self.actions = []
        self.closed = False
        self.first = self
        self.context = self

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        assert self.listeners.pop(event, None) == callback

    async def route(self, _pattern, handler):
        self.route_handler = handler

    async def unroute(self, _pattern, handler):
        assert handler == self.route_handler
        self.route_handler = None

    async def start(self, endpoint):
        request = FakeRequest(endpoint)
        route = FakeRoute(request)
        self.routes.append(route)
        if "request" in self.listeners:
            self.listeners["request"](request)
        await self.route_handler(route, request)
        return request, route

    def respond(self, request, status, payload, **kwargs):
        self.listeners["response"](FakeResponse(request, status, payload, **kwargs))

    async def api(self, endpoint, status=200, payload=None, **kwargs):
        request, route = await self.start(endpoint)
        if route.allowed:
            self.respond(request, status, payload, **kwargs)
        return request, route

    async def goto(self, url, **_kwargs):
        self.actions.append(("goto", url))

    def get_by_placeholder(self, _placeholder, **_kwargs):
        return self

    async def fill(self, value, **_kwargs):
        self.actions.append(("fill", value))

    async def press(self, key, **_kwargs):
        self.actions.append(("press", key))
        if self.script is not None:
            await self.script(self)

    def locator(self, _selector):
        return self

    async def count(self):
        return int(self.widget)

    async def is_visible(self):
        return self.widget

    async def close(self):
        self.closed = True
        self.route_handler = None


def test_initial_data_uses_one_fixed_search_and_does_not_claim_source_validation():
    async def script(page):
        await page.api("userInfo", payload=PROFILE)
        await page.api("postsV2", payload=POSTS)

    async def scenario():
        page = FakePage(script)
        report = await browser_probe.observe_page(page, observe_seconds=1)
        assert report["outcome"] == "initial_data"
        assert report["initial_data"] is True
        assert report["request_count"] == 2
        assert [event["contract_valid"] for event in report["events"]] == [True, True]
        assert page.actions == [("goto", "https://anonyig.com/en/"), ("fill", "nasa"), ("press", "Enter")]
        assert not page.listeners
        assert page.route_handler is None
        assert page.closed
        assert "source_validated" not in report

    asyncio.run(scenario())


@pytest.mark.parametrize("ordering", ["new", "old", "preissued"])
def test_challenge_recovery_requires_same_endpoint_request_started_after_422_header(ordering):
    async def script(page):
        await page.api("userInfo", payload=PROFILE)
        if ordering == "old":
            success, _ = await page.start("postsV2")
            await page.api("postsV2", 422, TURNSTILE)
        elif ordering == "preissued":
            challenge, _ = await page.start("postsV2")
            success, _ = await page.start("postsV2")
            page.respond(challenge, 422, TURNSTILE)
        else:
            await page.api("postsV2", 422, TURNSTILE)
            success, _ = await page.start("postsV2")
        page.respond(success, 200, POSTS)

    report = asyncio.run(browser_probe.observe_page(FakePage(script), observe_seconds=1))
    assert report["outcome"] == ("recovered_after_challenge" if ordering == "new" else "challenge_unresolved")


@pytest.mark.parametrize("endpoint", ["userInfo", "stories", "SECRET_UNKNOWN", "posts"])
def test_other_endpoint_challenge_is_not_hidden_by_valid_profile_and_posts(endpoint):
    async def script(page):
        await page.api(endpoint, 422, TURNSTILE)
        await page.api("userInfo", payload=PROFILE)
        await page.api("postsV2", payload=POSTS)

    report = asyncio.run(browser_probe.observe_page(FakePage(script), observe_seconds=1))
    assert report["outcome"] == ("recovered_after_challenge" if endpoint == "userInfo" else "challenge_unresolved")
    assert "SECRET_" not in json.dumps(report)


def test_late_challenge_is_observed_after_initial_contract_success():
    async def scenario():
        pending = []

        async def script(page):
            await page.api("userInfo", payload=PROFILE)
            await page.api("postsV2", payload=POSTS)

            async def later():
                await asyncio.sleep(0.05)
                await page.api("postsV2", 422, TURNSTILE)

            pending.append(asyncio.create_task(later()))

        report = await browser_probe.observe_page(FakePage(script), observe_seconds=1)
        await asyncio.gather(*pending)
        assert report["outcome"] == "challenge_unresolved"
        assert report["elapsed_ms"] >= 950

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403, 429])
def test_fatal_http_stops_further_api_dispatch_without_waiting_for_body(status):
    async def scenario():
        gate = asyncio.Event()

        async def script(page):
            await page.api("postsV2", status, TURNSTILE, gate=gate)
            await page.api("stories", 200, {})

        page = FakePage(script)
        report = await browser_probe.observe_page(page, observe_seconds=1)
        assert report["outcome"] == "stopped_http"
        assert sum(route.allowed for route in page.routes) == 1
        assert page.routes[-1].aborted
        assert report["events"][0]["body_state"] == "not_applicable"
        assert page.closed

    asyncio.run(scenario())


def test_eighth_request_is_allowed_ninth_is_aborted_and_event_buffer_is_bounded():
    async def script(page):
        for _ in range(25):
            await page.api("postsV2", 422, TURNSTILE)

    page = FakePage(script)
    report = asyncio.run(browser_probe.observe_page(page, observe_seconds=1))
    assert report["outcome"] == "request_budget"
    assert report["request_count"] == 8
    assert sum(route.allowed for route in page.routes) == 8
    assert all(route.aborted for route in page.routes[8:])
    assert len(report["events"]) == 9


@pytest.mark.parametrize("bad_profile,bad_posts", [
    ({"result": []}, POSTS), (PROFILE, {"result": {"edges": []}}),
    ({"result": [{"user": {**PROFILE["result"][0]["user"], "username": "other"}}]}, POSTS),
])
def test_200_without_both_valid_contracts_is_not_initial_data(bad_profile, bad_posts):
    async def script(page):
        await page.api("userInfo", payload=bad_profile)
        await page.api("postsV2", payload=bad_posts)

    report = asyncio.run(browser_probe.observe_page(FakePage(script), observe_seconds=1))
    assert report["outcome"] == "inconclusive"
    assert report["initial_data"] is False


def test_widget_visibility_alone_does_not_claim_manual_verification_is_required():
    report = asyncio.run(browser_probe.observe_page(FakePage(widget=True), observe_seconds=1))
    assert report["visible_challenge"] is True
    assert report["outcome"] == "inconclusive"
    assert "verification_required" not in report


def test_widget_inspection_failure_is_unknown_not_a_runtime_failure():
    page = FakePage()

    async def failed_count():
        raise ValueError("SECRET_LOCATOR_ERROR")

    page.count = failed_count
    report = asyncio.run(browser_probe.observe_page(page, observe_seconds=1))
    assert report["outcome"] == "inconclusive"
    assert report["visible_challenge"] is None
    assert "SECRET_" not in json.dumps(report)


@pytest.mark.parametrize("value", [0, 46, True, 1.5, "30"])
def test_observation_bounds_are_checked_before_any_page_action(value):
    page = FakePage()
    with pytest.raises(ValueError):
        asyncio.run(browser_probe.observe_page(page, observe_seconds=value))
    assert page.actions == []


def test_cancelled_observer_closes_owned_page_and_propagates_cancellation():
    async def scenario():
        submitted = asyncio.Event()

        async def script(_page):
            submitted.set()

        page = FakePage(script)
        observer = asyncio.create_task(browser_probe.observe_page(page, observe_seconds=30))
        await submitted.wait()
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert page.closed
        assert not page.listeners
        assert page.route_handler is None

    asyncio.run(scenario())


def test_navigation_failure_exposes_only_fixed_runtime_stage_and_kind():
    page = FakePage()

    async def failed_navigation(*_args, **_kwargs):
        raise ValueError("SECRET_URL_AND_EXCEPTION")

    page.goto = failed_navigation
    report = asyncio.run(browser_probe.observe_page(page, observe_seconds=1))
    assert report["outcome"] == "runtime_error"
    assert report["runtime_stage"] == "navigation"
    assert report["runtime_kind"] == "browser_error"
    assert "SECRET_" not in json.dumps(report)
    assert page.closed


@pytest.mark.parametrize("unreadable", [False, True])
def test_response_body_read_is_bounded_and_never_retains_payload(unreadable):
    async def scenario():
        gate = asyncio.Event()

        async def script(page):
            await page.api("userInfo", 422, TURNSTILE, unreadable=unreadable,
                           gate=None if unreadable else gate)

        report = await browser_probe.observe_page(FakePage(script), observe_seconds=1)
        assert report["outcome"] == "challenge_unresolved"
        assert report["events"][0]["body_state"] == ("unreadable" if unreadable else "timeout")
        assert report["events"][0]["challenge_kind"] == "unspecified"
        assert "SECRET_" not in json.dumps(report)

    asyncio.run(scenario())


def install_fake_browser(monkeypatch, page, *, version="144.0.1.2"):
    calls = []

    async def new_page():
        calls.append(("new_page",))
        return page

    page.new_page = new_page

    class Browser:
        async def new_context(self, **options):
            calls.append(("new_context", options))
            return page

        async def close(self):
            calls.append(("browser_close",))

    browser = Browser()
    browser.version = version

    async def launch(**options):
        calls.append(("launch", options))
        return browser

    async def manager_stop():
        calls.append(("manager_stop",))

    manager = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=manager_stop)

    async def start():
        calls.append(("start",))
        return manager

    monkeypatch.setattr(browser_probe, "async_playwright", lambda: SimpleNamespace(start=start))
    return calls


@pytest.mark.parametrize("headless", [False, True])
def test_run_probe_creates_one_stock_browser_and_fresh_nonpersistent_context(monkeypatch, headless):
    async def script(page):
        await page.api("userInfo", payload=PROFILE)
        await page.api("postsV2", payload=POSTS)

    calls = install_fake_browser(monkeypatch, FakePage(script))
    report = asyncio.run(browser_probe.run_probe(headless=headless, observe_seconds=1))
    assert report["outcome"] == "initial_data"
    assert [call for call in calls if call[0] == "launch"] == [
        ("launch", {"headless": headless, "channel": "chromium"}),
    ]
    assert [call for call in calls if call[0] == "new_context"] == [
        ("new_context", {"service_workers": "block", "accept_downloads": False,
                         "locale": "en-US", "viewport": {"width": 1440, "height": 1000}}),
    ]
    assert calls.count(("new_page",)) == 1
    assert calls[-2:] == [("browser_close",), ("manager_stop",)]
    assert report["headless"] is headless
    assert report["service_workers"] == "blocked"
    assert report["browser_version"] == "144.0.1.2"
    assert report["platform"] in {"linux", "windows", "macos", "other"}
    assert report["architecture"] in {"arm64", "x64", "other"}


def test_runtime_launch_deadline_is_bounded_and_browser_version_cannot_leak(monkeypatch):
    async def scenario():
        async def never_start():
            await asyncio.Event().wait()

        monkeypatch.setattr(browser_probe, "_RUN_TIMEOUT_SECONDS", 0.08)
        monkeypatch.setattr(browser_probe, "async_playwright", lambda: SimpleNamespace(start=never_start))
        report = await asyncio.wait_for(browser_probe.run_probe(observe_seconds=1), timeout=0.25)
        assert report["outcome"] == "runtime_error"
        assert report["runtime_stage"] == "launch"
        assert report["runtime_kind"] == "timeout"

    asyncio.run(scenario())


def test_cli_emits_one_safe_marker_and_exit_code_without_running_both_modes(monkeypatch, capsys):
    calls = install_fake_browser(monkeypatch, FakePage(), version="SECRET_BROWSER_VERSION")
    exit_code = browser_probe.main(["--observe-seconds", "1"])
    output = capsys.readouterr().out.strip().splitlines()
    assert exit_code == 2
    assert len(output) == 1
    assert output[0].startswith("[ANONYIG-BROWSER-PROBE] ")
    report = json.loads(output[0].removeprefix("[ANONYIG-BROWSER-PROBE] "))
    assert report["browser_version"] is None
    assert "SECRET_" not in output[0]
    assert len([call for call in calls if call[0] == "launch"]) == 1


def test_cli_invalid_observation_never_starts_browser_and_uses_error_exit(monkeypatch, capsys):
    calls = install_fake_browser(monkeypatch, FakePage())
    assert browser_probe.main(["--observe-seconds", "46"]) == 3
    assert calls == []
    output = capsys.readouterr().out.strip()
    report = json.loads(output.removeprefix("[ANONYIG-BROWSER-PROBE] "))
    assert report["outcome"] == "runtime_error"
    assert report["runtime_kind"] == "invalid_arguments"


def test_owned_context_remains_guarded_until_sibling_pages_are_closed():
    async def scenario():
        page = FakePage()

        class FreshContext:
            closed = False
            sibling_leaks = 0
            on = page.on
            remove_listener = page.remove_listener
            route = page.route

            async def unroute(self, pattern, handler):
                await page.unroute(pattern, handler)
                if not self.closed:
                    # An existing sibling has a timer ready when routing ends.
                    self.sibling_leaks += 1

            async def close(self):
                _, route = await page.start("stories")
                assert route.aborted, "Sibling API must stay guarded throughout context shutdown"
                self.closed = True
                await page.close()
                page.route_handler = None

        context = FreshContext()
        page.context = context
        report = await browser_probe.observe_page(page, observe_seconds=1)
        assert report["outcome"] == "inconclusive"
        assert context.closed
        assert context.sibling_leaks == 0

    asyncio.run(scenario())


def test_context_close_timeout_keeps_stop_guard_installed(monkeypatch):
    monkeypatch.setattr(browser_probe, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        page = FakePage()

        async def hanging_close():
            await asyncio.Event().wait()

        page.close = hanging_close
        report = await browser_probe.observe_page(page, observe_seconds=1)
        assert report["outcome"] == "runtime_error"
        assert report["runtime_stage"] == "cleanup"
        assert report["runtime_kind"] == "timeout"
        assert page.route_handler is not None
        _, route = await page.start("postsV2")
        assert route.aborted
        assert report["request_count"] == 0

    asyncio.run(scenario())
