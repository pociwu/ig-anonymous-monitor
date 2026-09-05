"""Real Chromium/DOM coverage with every request intercepted; no live source.

Opt in with IG_MONITOR_BROWSER_TESTS=1. The fixture page imitates only ordinary
search and automatic API responses, never a real verification implementation.
"""
from __future__ import annotations

import asyncio
from contextlib import chdir
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("IG_MONITOR_BROWSER_TESTS") != "1", reason="Opt-in offline browser probe QA",
)

PROFILE = {"result": [{"user": {
    "username": "nasa", "id": "1", "is_private": False,
    "media_count": 1, "follower_count": 1, "following_count": 1,
    "full_name": "RAW_PRIVATE_PROFILE", "biography": "RAW_PRIVATE_BIO",
    "profile_pic_url": "https://media.example.test/avatar.jpg",
}}]}
POSTS = {"result": {"edges": [], "page_info": {"has_next_page": False}}}
CHALLENGE = {"challenge": {"type": "turnstile", "siteKey": "RAW_SITE_KEY"},
             "message": "RAW_PRIVATE_ERROR"}


def fixture_html(scenario):
    # The success or retry comes from this fixture's own JavaScript, not the
    # observer. A second Enter/click is recorded and would fail the assertions.
    actions = {
        "initial": "request('postsV2', 'initial');",
        "recovered": "request('postsV2', 'challenge').then(() => setTimeout(() => request('postsV2', 'retry'), 60));",
        "unresolved": "request('postsV2', 'challenge');",
        "old_success": "request('postsV2', 'old'); request('postsV2', 'challenge');",
        "preissued_success": "request('postsV2', 'delayed_challenge'); request('postsV2', 'old');",
        "late_challenge": "request('postsV2', 'initial').then(() => setTimeout(() => request('postsV2', 'challenge'), 100));",
        "budget": "for (let i = 0; i < 15; i++) request('postsV2', 'burst');",
        "popup": "window.open('/popup/'); request('postsV2', 'initial');",
    }[scenario]
    return """<!doctype html><html><body>
      <form><input placeholder="@username or link"><button>Search</button></form>
      <script>
      window.submissions = 0;
      window.clicks = 0;
      document.addEventListener('click', () => { window.clicks++; window.fixtureObserved('click'); });
      function request(endpoint, kind) {
        return fetch('/api/v1/instagram/' + endpoint + '?kind=' + kind + '&token=RAW_QUERY_TOKEN').catch(() => {});
      }
      document.querySelector('form').addEventListener('submit', event => {
        event.preventDefault();
        window.submissions++;
        window.fixtureObserved('submit');
        request('userInfo', 'profile');
        ACTIONS
      });
      </script></body></html>""".replace("ACTIONS", actions)


@pytest.mark.parametrize(("scenario", "expected"), [
    ("initial", "initial_data"),
    ("recovered", "recovered_after_challenge"),
    ("unresolved", "challenge_unresolved"),
    ("old_success", "challenge_unresolved"),
    ("preissued_success", "challenge_unresolved"),
    ("late_challenge", "challenge_unresolved"),
    ("budget", "request_budget"),
    ("popup", "initial_data"),
])
def test_one_search_passive_observation_against_offline_browser_fixture(scenario, expected):
    from playwright.async_api import async_playwright
    from ig_monitor.browser_probe import observe_page

    async def run():
        async with async_playwright() as playwright:
            # Full Chromium in modern headless mode keeps local QA invisible.
            # The deployed diagnostic uses headed Chromium under Linux Xvfb.
            browser = await playwright.chromium.launch(headless=True, channel="chromium")
            context = await browser.new_context(accept_downloads=False)
            unexpected = []
            served_api = []
            actions = {"submit": 0, "click": 0}
            owned_pages = []
            context.on("page", lambda owned_page: owned_pages.append(owned_page))

            async def fixture_route(route):
                parsed = urlsplit(route.request.url)
                if parsed.hostname != "anonyig.com":
                    unexpected.append("external_request")
                    await route.abort()
                elif parsed.path == "/en/":
                    await route.fulfill(content_type="text/html", body=fixture_html(scenario))
                elif parsed.path == "/popup/":
                    await route.fulfill(content_type="text/html", body="""<script>
                      setInterval(() => fetch('/api/v1/instagram/stories?token=RAW_POPUP_TOKEN').catch(() => {}), 250);
                    </script>""")
                elif parsed.path.startswith("/api/v1/instagram/"):
                    endpoint = parsed.path.rsplit("/", 1)[-1]
                    served_api.append(endpoint)
                    challenge = any(kind in parsed.query for kind in ("kind=challenge", "kind=delayed_challenge", "kind=burst"))
                    if "kind=delayed_challenge" in parsed.query:
                        await asyncio.sleep(0.06)
                    if "kind=old" in parsed.query:
                        await asyncio.sleep(0.15)
                    payload = CHALLENGE if challenge else PROFILE if endpoint == "userInfo" else POSTS
                    await route.fulfill(status=422 if challenge else 200, content_type="application/json",
                                        body=json.dumps(payload))
                else:
                    await route.abort()

            # All origins are intercepted. The probe's page-level budget route
            # must fall back to this fixture, never continue to the Internet.
            await context.route("**/*", fixture_route)
            page = await context.new_page()

            def observed_action(kind):
                actions[kind] += 1

            await page.expose_function("fixtureObserved", observed_action)
            try:
                report = await observe_page(page, observe_seconds=1)
                assert report["outcome"] == expected
                assert report["request_count"] <= 8
                assert len(served_api) <= 8
                assert not unexpected
                assert actions["submit"] == 1
                # Enter may activate a form's submit button in Chromium, but no
                # further click/keypress may be generated by the observer.
                assert actions["click"] <= 1
                assert page.is_closed()
                if scenario == "popup":
                    assert len(owned_pages) == 2
                    assert all(owned_page.is_closed() for owned_page in owned_pages)
                    after_close = len(served_api)
                    await asyncio.sleep(0.3)
                    assert len(served_api) == after_close
                assert "RAW_" not in json.dumps(report)
                if scenario in {"old_success", "preissued_success", "late_challenge"}:
                    assert any(event["status"] == 422 for event in report["events"])
            finally:
                await context.close()
                await browser.close()

    artifact_dir = Path(__file__).resolve().parents[1] / ".pytest-tmp" / "browser-probe-qa"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    # Any Chromium debug file remains in an ignored, synthetic-QA directory.
    with chdir(artifact_dir):
        asyncio.run(run())
