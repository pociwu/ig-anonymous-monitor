"""Isolated, one-search browser comparison; never a production-source approval.

The supplied page and its fresh context belong to this probe. It observes the
website's own requests, does not solve challenges, and closes the whole context.
No credentials, tokens, HTML, original response bodies, or media are persisted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import re
import sys
import time
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from .anonyig import parse_posts_page, parse_profile


MARKER = "[ANONYIG-BROWSER-PROBE] "
_SOURCE_URL = "https://anonyig.com/en/"
_API_ENDPOINTS = frozenset({"userInfo", "posts", "postsV2", "stories", "highlights", "highlightStories"})
_API_PATTERN = "**/api/v1/instagram/**"
_REQUEST_LIMIT = 8
_BODY_TIMEOUT_SECONDS = 0.5
_CLOSE_TIMEOUT_SECONDS = 2.0
_RUN_TIMEOUT_SECONDS = 75.0
_FATAL_HTTP = frozenset({401, 403, 429})


def _validate_seconds(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 45:
        raise ValueError("observe_seconds must be an integer from 1 to 45")


def _endpoint(url: str) -> str | None:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or not (host == "anonyig.com" or host.endswith(".anonyig.com"))
            or not parsed.path.startswith("/api/v1/instagram/")):
        return None
    endpoint = parsed.path.removeprefix("/api/v1/instagram/")
    return endpoint if endpoint in _API_ENDPOINTS else "unknown_api"


def _consume_task(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


async def _bounded(awaitable, seconds: float):
    """Do not let a slow browser operation extend a diagnostic deadline."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, seconds))
        if done:
            return task.result()
        task.cancel()
        task.add_done_callback(_consume_task)
        raise TimeoutError
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_task)
        raise


def _empty_report(outcome: str, kind: str | None = None, stage: str | None = None) -> dict[str, Any]:
    return {
        "source": "anonyig", "outcome": outcome, "elapsed_ms": 0,
        "request_count": 0, "visible_challenge": None, "initial_data": False,
        "events": [], "runtime_stage": stage, "runtime_kind": kind,
    }


async def observe_page(page, *, observe_seconds: int = 30) -> dict[str, Any]:
    """Search NASA once, passively observe, then close its probe-owned context.

    The caller must supply a fresh context (service workers blocked). Context-wide
    API interception also covers a website-created popup/worker. If close times
    out, the stop guard remains installed until the caller closes the browser.
    """
    _validate_seconds(observe_seconds)
    started = time.monotonic()
    context = page.context
    events: list[dict[str, Any]] = []
    requests: dict[Any, dict[str, Any]] = {}
    captures: set[asyncio.Task] = set()
    stop = asyncio.Event()
    stop_reason: str | None = None
    runtime_stage: str | None = None
    runtime_kind: str | None = None
    active_stage = "context"
    accepting = True
    frozen = False
    stopping = False
    request_count = 0
    visible_challenge: bool | None = None
    listeners_installed = False

    def elapsed() -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    def stop_as(reason: str) -> None:
        nonlocal stop_reason
        if stop_reason is None:
            stop_reason = reason
        stop.set()

    def request_started(request) -> None:
        if not accepting or stopping or stop.is_set() or request in requests:
            return
        endpoint = _endpoint(request.url)
        if endpoint is None:
            return
        index = len(events) + 1
        event = {
            "request_index": index, "endpoint": endpoint, "started_ms": elapsed(),
            "status": None, "body_state": "not_received", "contract_valid": False,
            "challenge_kind": "none", "recovery_after_index": 0,
        }
        events.append(event)
        requests[request] = event
        if index > _REQUEST_LIMIT:
            event["body_state"] = "not_requested"
            stop_as("request_budget")

    async def route_api(route, request=None) -> None:
        nonlocal request_count
        request = request or route.request
        if _endpoint(request.url) is None:
            await route.fallback()
            return
        # Playwright normally notifies request before routing; this fallback
        # preserves the same identity if an alternative browser boundary differs.
        request_started(request)
        event = requests.get(request)
        if (stopping or event is None or event["request_index"] > _REQUEST_LIMIT
                or stop_reason not in {None, "request_budget"}):
            await route.abort()
            return
        request_count += 1
        # Preserve other registered routes, notably the offline fixture route.
        await route.fallback()

    async def capture(response, event: dict[str, Any]) -> None:
        try:
            payload = await _bounded(response.json(), _BODY_TIMEOUT_SECONDS)
            body_state = "json"
        except TimeoutError:
            payload, body_state = None, "timeout"
        except Exception:
            payload, body_state = None, "unreadable"
        if frozen:
            return
        event["body_state"] = body_state
        if event["status"] == 422:
            challenge = payload.get("challenge") if isinstance(payload, dict) else None
            if (isinstance(challenge, dict) and challenge.get("type") == "turnstile"
                    and isinstance(challenge.get("siteKey"), str) and challenge["siteKey"]):
                event["challenge_kind"] = "turnstile"
        if event["status"] != 200 or body_state != "json":
            return
        try:
            if event["endpoint"] == "userInfo":
                parse_profile(payload, "nasa")
            elif event["endpoint"] in {"posts", "postsV2"}:
                parse_posts_page(payload)
            else:
                return
        except Exception:
            return
        event["contract_valid"] = True

    def response_received(response) -> None:
        if not accepting or frozen:
            return
        event = requests.get(response.request)
        if event is None or event["request_index"] > _REQUEST_LIMIT or event["status"] is not None:
            return
        status = response.status
        event["status"] = status if type(status) is int and 100 <= status <= 599 else None
        if status in _FATAL_HTTP:
            event["body_state"] = "not_applicable"
            stop_as("stopped_http")
            return
        if status == 422:
            event["challenge_kind"] = "unspecified"
            # A newer request already in flight when 422 arrives is not recovery.
            event["recovery_after_index"] = len(events)
        task = asyncio.create_task(capture(response, event))
        captures.add(task)
        task.add_done_callback(captures.discard)
        task.add_done_callback(_consume_task)

    async def inspect_widget(deadline: float) -> None:
        nonlocal visible_challenge
        if visible_challenge is True:
            return
        try:
            found = False
            for selector in ('iframe[src*="challenges.cloudflare.com"]',
                             'iframe[src*="recaptcha"][title*="challenge"]'):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or stop.is_set():
                    return
                locator = page.locator(selector)
                if await _bounded(locator.count(), min(0.2, remaining)):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    found = found or bool(await _bounded(locator.first.is_visible(), min(0.2, remaining)))
            visible_challenge = found
        except Exception:
            # An inaccessible iframe is unknown, not proof of human interaction.
            pass

    try:
        context.on("request", request_started)
        context.on("response", response_received)
        listeners_installed = True
        await _bounded(context.route(_API_PATTERN, route_api), 2.0)
        async with asyncio.timeout(max(0.01, _RUN_TIMEOUT_SECONDS - 5.0 - (time.monotonic() - started))):
            active_stage = "navigation"
            await _bounded(page.goto(_SOURCE_URL, wait_until="domcontentloaded", timeout=15000), 15.0)
            if not stop.is_set():
                active_stage = "search"
                search = page.get_by_placeholder("@username or link", exact=True)
                await _bounded(search.fill("nasa", timeout=5000), 5.0)
                if not stop.is_set():
                    await _bounded(search.press("Enter", timeout=5000), 5.0)
            deadline = time.monotonic() + observe_seconds
            active_stage = "observe"
            # Initial success never shortens this window; late failures matter.
            while not stop.is_set() and time.monotonic() < deadline:
                await inspect_widget(deadline)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or stop.is_set():
                    break
                try:
                    await _bounded(stop.wait(), min(0.1, remaining))
                except TimeoutError:
                    pass
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        runtime_stage, runtime_kind = active_stage, "timeout"
        stop_as("runtime_error")
    except Exception:
        runtime_stage, runtime_kind = active_stage, "browser_error"
        stop_as("runtime_error")
    finally:
        # Freeze new events before cleanup. Existing body reads get one bounded
        # settlement. Keep the route in stop mode until the entire context closes,
        # including any website-created siblings; never unroute a live context.
        stopping = True
        accepting = False
        if captures:
            _, pending = await asyncio.wait(set(captures), timeout=_BODY_TIMEOUT_SECONDS + 0.05)
            for task in pending:
                task.cancel()
        frozen = True
        if listeners_installed:
            context.remove_listener("request", request_started)
            context.remove_listener("response", response_received)
        try:
            await _bounded(context.close(), _CLOSE_TIMEOUT_SECONDS)
        except Exception as exc:
            runtime_stage, runtime_kind = "cleanup", "timeout" if isinstance(exc, TimeoutError) else "browser_error"
            stop_reason = "runtime_error"

    valid = [event for event in events if event["contract_valid"]]
    initial_data = (any(event["endpoint"] == "userInfo" for event in valid)
                    and any(event["endpoint"] in {"posts", "postsV2"} for event in valid))
    challenges = [event for event in events if event["status"] == 422]
    unresolved = any(not any(success["endpoint"] == challenge["endpoint"]
                             and success["request_index"] > challenge["recovery_after_index"]
                             for success in valid) for challenge in challenges)
    if stop_reason is not None:
        outcome = stop_reason
    elif challenges:
        direct_challenge = any(event["challenge_kind"] == "turnstile" for event in challenges)
        outcome = "recovered_after_challenge" if initial_data and direct_challenge and not unresolved else "challenge_unresolved"
    else:
        outcome = "initial_data" if initial_data else "inconclusive"
    return {
        "source": "anonyig", "outcome": outcome, "elapsed_ms": elapsed(),
        "request_count": request_count, "visible_challenge": visible_challenge,
        "initial_data": initial_data, "events": [dict(event) for event in events],
        "runtime_stage": runtime_stage, "runtime_kind": runtime_kind,
    }


async def run_probe(*, headless: bool = False, observe_seconds: int = 30) -> dict[str, Any]:
    """One stock Chromium run with a new, non-persistent browser context."""
    _validate_seconds(observe_seconds)
    if type(headless) is not bool:
        raise ValueError("headless must be a bool")
    started = time.monotonic()
    deadline = started + _RUN_TIMEOUT_SECONDS
    manager = browser = context = None
    active_stage = "launch"
    browser_version = None
    report = _empty_report("runtime_error", "browser_error", active_stage)
    try:
        # Reserve the final six seconds for bounded browser/context cleanup.
        async with asyncio.timeout(max(0.01, _RUN_TIMEOUT_SECONDS - 6.0)):
            manager = await async_playwright().start()
            browser = await manager.chromium.launch(headless=headless, channel="chromium")
            version = browser.version
            if isinstance(version, str) and len(version) <= 64 and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version):
                browser_version = version
            active_stage = "context"
            context = await browser.new_context(service_workers="block", accept_downloads=False,
                                                locale="en-US", viewport={"width": 1440, "height": 1000})
            page = await context.new_page()
            active_stage = "observe"
            report = await observe_page(page, observe_seconds=observe_seconds)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        report = _empty_report("runtime_error", "timeout", active_stage)
    except Exception:
        report = _empty_report("runtime_error", "browser_error", active_stage)
    finally:
        for resource, method in ((context, "close"), (browser, "close"), (manager, "stop")):
            if resource is None:
                continue
            try:
                await _bounded(getattr(resource, method)(), min(2.0, max(0.0, deadline - time.monotonic())))
            except Exception as exc:
                report["outcome"] = "runtime_error"
                report["runtime_stage"] = "cleanup"
                report["runtime_kind"] = "timeout" if isinstance(exc, TimeoutError) else "browser_error"
    host_platform = {"linux": "linux", "win32": "windows", "darwin": "macos"}.get(sys.platform, "other")
    architecture = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower(), "other")
    report.update(headless=headless, service_workers="blocked", platform=host_platform,
                  architecture=architecture, browser_version=browser_version,
                  elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))
    return report


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid_arguments")


def main(argv=None) -> int:
    parser = _Parser(description="One isolated NASA browser comparison; no source approval.")
    parser.add_argument("--headless", action="store_true", help="Run one headless comparison instead of headed Chromium.")
    parser.add_argument("--observe-seconds", type=int, default=30)
    try:
        args = parser.parse_args(argv)
        _validate_seconds(args.observe_seconds)
        report = asyncio.run(run_probe(headless=args.headless, observe_seconds=args.observe_seconds))
    except (ValueError, argparse.ArgumentError):
        report = _empty_report("runtime_error", "invalid_arguments")
    except KeyboardInterrupt:
        report = _empty_report("runtime_error", "cancelled")
    print(MARKER + json.dumps(report, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0 if report["outcome"] in {"initial_data", "recovered_after_challenge"} else 3 if report["outcome"] == "runtime_error" else 2


if __name__ == "__main__":
    raise SystemExit(main())
