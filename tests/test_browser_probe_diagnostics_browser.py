"""Real browser telemetry from intercepted synthetic resources, never live sites."""
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


@pytest.mark.parametrize("scenario", ["loaded", "script_error", "network_error", "http_error", "not_requested", "frame_error"])
def test_diagnostic_resources_and_js_errors_do_not_change_challenge_outcome(scenario):
    from playwright.async_api import async_playwright
    from ig_monitor.browser_probe import observe_page

    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, channel="chromium")
            context = await browser.new_context(service_workers="block", accept_downloads=False)
            actions = {"submit": 0, "click": 0, "render": 0}
            api_requests = []
            unexpected = []

            def observed(kind):
                actions[kind] += 1

            await context.expose_function("fixtureObserved", observed)

            async def fixture_route(route):
                parsed = urlsplit(route.request.url)
                if parsed.hostname == "anonyig.com" and parsed.path == "/en/":
                    loader = "" if scenario == "not_requested" else (
                        '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?token=RAW_QUERY"></script>'
                    )
                    frame = "" if scenario != "frame_error" else (
                        '<iframe style="display:none" src="https://challenges.cloudflare.com/'
                        'cdn-cgi/challenge-platform/fixture?token=RAW_FRAME_QUERY"></iframe>'
                    )
                    await route.fulfill(content_type="text/html", body="""<!doctype html>
                      <script src="/fixture.js?token=RAW_SOURCE_QUERY"></script>LOADER
                      <form><input placeholder="@username or link"><button>Search</button></form>
                      <script>
                        document.addEventListener('click', () => fixtureObserved('click'));
                        document.querySelector('form').addEventListener('submit', e => {
                          e.preventDefault(); fixtureObserved('submit');
                          for (const endpoint of ['userInfo', 'postsV2', 'posts']) {
                            fetch('/api/v1/instagram/' + endpoint + '?token=RAW_API_QUERY');
                          }
                        });
                      </script>FRAME""".replace("LOADER", loader).replace("FRAME", frame))
                elif parsed.hostname == "anonyig.com" and parsed.path == "/fixture.js":
                    await route.fulfill(content_type="application/javascript", body="""
                      console.error('RAW_CONSOLE_COOKIE', {token: 'RAW_CONSOLE_TOKEN'});
                      console.warn('RAW_WARNING_URL');
                      setTimeout(() => { throw new TypeError('RAW_PAGE_ERROR_TOKEN'); }, 10);
                    """)
                elif parsed.hostname == "challenges.cloudflare.com" and parsed.path == "/turnstile/v0/api.js":
                    if scenario == "network_error":
                        await route.abort("connectionrefused")
                    elif scenario == "http_error":
                        await route.fulfill(status=403, content_type="text/plain", body="RAW_ERROR_BODY")
                    else:
                        script = (
                            "throw new ReferenceError('RAW_SCRIPT_ERROR');" if scenario == "script_error" else
                            "window.turnstile = {render: () => { fixtureObserved('render'); }};"
                        )
                        await route.fulfill(content_type="application/javascript", body=script)
                elif parsed.hostname == "challenges.cloudflare.com" and parsed.path == "/cdn-cgi/challenge-platform/fixture":
                    await route.fulfill(content_type="text/html", body="""<script>
                      console.error('RAW_FRAME_CONSOLE_TOKEN');
                      throw new RangeError('RAW_FRAME_ERROR_TOKEN');
                    </script>""")
                elif parsed.hostname == "anonyig.com" and parsed.path.startswith("/api/v1/instagram/"):
                    api_requests.append(parsed.path.rsplit("/", 1)[-1])
                    await route.fulfill(status=422, content_type="application/json", body=json.dumps({
                        "challenge": {"type": "turnstile", "siteKey": "RAW_SITE_KEY"},
                    }))
                else:
                    unexpected.append("unexpected")
                    await route.abort()

            # This is the sole network boundary: every origin is fulfilled or aborted.
            await context.route("**/*", fixture_route)
            page = await context.new_page()
            try:
                report = await observe_page(page, observe_seconds=1)
                assert report["outcome"] == "challenge_unresolved"
                assert report["initial_data"] is False
                assert report["request_count"] == 3
                assert api_requests == ["userInfo", "postsV2", "posts"]
                assert actions["submit"] == 1 and actions["click"] <= 1
                assert actions["render"] == 0, "Observer must not invoke the verification API"
                assert not unexpected
                assert page.is_closed()
                diag = report["diagnostics"]
                assert diag["active"] is True and diag["truncated"] is False
                assert diag["turnstile_api_seen"] is (scenario in {"loaded", "frame_error"})
                assert diag["js_errors"]["uncaught"]["TypeError"] == 1
                assert diag["js_errors"]["console_errors"] >= 1
                assert diag["js_errors"]["console_warnings"] == 1
                source = diag["resources"]["source_script"]
                assert source["requested"] == source["finished"] == 1
                assert source["http_statuses"] == [200]
                loader = diag["resources"]["turnstile_script"]
                if scenario == "not_requested":
                    assert loader["requested"] == 0
                elif scenario == "network_error":
                    assert loader["failed"] == 1
                    assert loader["failure_kinds"] == ["connection"]
                elif scenario == "http_error":
                    assert loader["http_statuses"] == [403]
                else:
                    assert loader["requested"] == loader["finished"] == 1
                    assert loader["http_statuses"] == [200]
                    if scenario == "script_error":
                        # Chromium masks the error name for this classic
                        # cross-origin script; never infer it from message text.
                        assert diag["js_errors"]["uncaught"]["other"] == 1
                        assert diag["js_errors"]["uncaught"]["ReferenceError"] == 0
                    elif scenario == "frame_error":
                        assert report["visible_challenge"] is False
                        assert diag["js_errors"]["uncaught"]["RangeError"] == 1
                        resource = diag["resources"]["challenge_resource"]
                        assert resource["requested"] == resource["finished"] == 1
                        assert resource["http_statuses"] == [200]
                serialized = json.dumps(report)
                assert "RAW_" not in serialized
                assert "https://" not in serialized
            finally:
                await context.close()
                await browser.close()

    artifact_dir = Path(__file__).resolve().parents[1] / ".pytest-tmp" / "browser-probe-qa"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with chdir(artifact_dir):
        asyncio.run(run())
