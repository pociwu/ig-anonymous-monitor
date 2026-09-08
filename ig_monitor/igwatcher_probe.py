"""Bounded, credential-free IGWatcher sample probe; never production approval.

Only fixed public NASA API requests are sent. No website scripts, media, redirects,
cookies, environment proxies, retries, production modules, or stored files are used.
The report contains only fixed labels, booleans, counts and runtime metadata.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import platform
import re
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx


MARKER = "[IGWATCHER-PROBE] "
BASE_URL = "https://igwatcher.com"
TARGET = "nasa"
TARGET_ID = "528817151"
REQUEST_LIMIT = 8
REQUEST_SECONDS = 20.0
TOTAL_SECONDS = 160.0
BODY_LIMIT = 2 * 1024 * 1024
ITEM_LIMIT = 100
CHILD_LIMIT = 20
PATHS = {
    "profile": "/wp-json/igw/v1/search",
    "stories": "/wp-json/igw/v1/stories",
    "posts": "/wp-json/igw/v1/posts",
    "reels": "/api/reels",
    "highlights": "/wp-json/igw/v1/highlights",
    "highlight_items": "/api/highlight-items",
}
_MEDIA_ID = re.compile(r"([1-9][0-9]{0,24})(?:_([1-9][0-9]{0,24}))?", re.ASCII)
_ALBUM_ID = re.compile(r"highlight:[1-9][0-9]{0,24}", re.ASCII)
_QUALITY_KEYS = (
    "invalid_ids", "numeric_ids", "duplicate_ids", "owner_suffix_mismatch",
    "owner_suffix_unknown", "timestamp_missing", "timestamp_invalid",
    "timestamp_future", "expiry_missing", "expiry_invalid", "expired_stories",
    "media_url_missing", "media_url_invalid", "reel_type_unconfirmed",
    "media_type_unconfirmed", "carousel_invalid", "child_ids_invalid",
    "child_owner_mismatch", "child_owner_unknown", "album_count_mismatch",
)


class _Stop(Exception):
    def __init__(self, outcome: str):
        self.outcome = outcome


def _new_report(now: float) -> dict:
    return {
        "schema_version": 1, "source": "igwatcher", "target": TARGET,
        "platform": platform.system().lower(), "architecture": platform.machine().lower(),
        "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
        "elapsed_ms": 0, "request_count": 0, "events": [],
        "outcome": "runtime_error", "production_ready": False,
    }


def _number(value) -> bool:
    return type(value) in (int, float) and 0 < value < 100_000_000_000


def _media_url_quality(item: dict, quality: dict) -> None:
    # Thumbnail-only results are not evidence of a downloadable source media.
    video = (item.get("is_video") is True or item.get("media_type") == 2
             or item.get("product_type") == "clips")
    value = item.get("video_url") if video else item.get("image_url") or item.get("video_url")
    if not value:
        quality["media_url_missing"] += 1
        return
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        valid = (
            parsed is not None and parsed.scheme == "https" and parsed.hostname
            and (parsed.port is None or parsed.port > 0)
            and not parsed.username and not parsed.password
            and not any(ord(c) <= 32 or ord(c) == 127 for c in value)
            and "\\" not in value
        )
        if not valid:
            quality["media_url_invalid"] += 1
    except (ValueError, TypeError):
        quality["media_url_invalid"] += 1


def _media_type_quality(item: dict, quality: dict, *, required: bool) -> None:
    kind = item.get("media_type")
    if (kind is None and required) or (kind is not None and (type(kind) is not int or kind not in (1, 2, 8))):
        quality["media_type_unconfirmed"] += 1
    video = item.get("is_video")
    if (("is_video" in item and type(video) is not bool)
            or (kind == 2 and video is False) or (kind == 1 and video is True)):
        quality["media_type_unconfirmed"] += 1


def _timestamp_quality(item: dict, endpoint: str, quality: dict, now: float) -> None:
    value = item.get("taken_at_timestamp" if endpoint in ("posts", "reels") else "taken_at")
    if value is None:
        quality["timestamp_missing"] += 1
    elif not _number(value) or value < 1_262_304_000:
        quality["timestamp_invalid"] += 1
    elif value > now + 300:
        quality["timestamp_future"] += 1
    if endpoint == "stories":
        expiry = item.get("expiring_at")
        if expiry is None:
            quality["expiry_missing"] += 1
        elif not _number(expiry) or not _number(value) or expiry <= value:
            quality["expiry_invalid"] += 1
        elif expiry <= now:
            quality["expired_stories"] += 1
        elif expiry - value > 2 * 86400:
            quality["expiry_invalid"] += 1


def _list_quality(items: list, endpoint: str, seen: set[str], now: float) -> dict:
    quality = dict.fromkeys(_QUALITY_KEYS, 0)
    for item in items:
        value = item.get("id")
        if type(value) in (int, float):
            quality["numeric_ids"] += 1
        match = _MEDIA_ID.fullmatch(value) if isinstance(value, str) else None
        if endpoint == "highlights":
            valid = isinstance(value, str) and _ALBUM_ID.fullmatch(value)
            canonical = value if valid else None
        else:
            valid = match is not None
            canonical = match[1] if match else None
        if not valid:
            quality["invalid_ids"] += 1
        if canonical:
            if canonical in seen:
                quality["duplicate_ids"] += 1
            seen.add(canonical)
        if endpoint == "highlights":
            if type(item.get("media_count")) is not int or item["media_count"] < 0:
                quality["album_count_mismatch"] += 1
            continue
        if not match or not match[2]:
            quality["owner_suffix_unknown"] += 1
        elif match[2] != TARGET_ID:
            # An ID suffix is a consistency check, not proof of collaboration.
            quality["owner_suffix_mismatch"] += 1
        _timestamp_quality(item, endpoint, quality, now)
        _media_type_quality(item, quality, required=endpoint in ("posts", "reels"))
        if endpoint == "reels" and (item.get("product_type") != "clips"
                                    or item.get("media_type") != 2
                                    or item.get("is_video") is not True):
            quality["reel_type_unconfirmed"] += 1
        children = item.get("children", [])
        if not isinstance(children, list) or len(children) > CHILD_LIMIT:
            quality["carousel_invalid"] += 1
            continue
        carousel = item.get("is_carousel") is True or item.get("media_type") == 8
        if carousel:
            count = item.get("carousel_count")
            if not children or type(count) is not int or count != len(children):
                quality["carousel_invalid"] += 1
            child_seen: set[str] = set()
            for child in children:
                if not isinstance(child, dict):
                    quality["carousel_invalid"] += 1
                    continue
                child_id = child.get("id")
                child_match = _MEDIA_ID.fullmatch(child_id) if isinstance(child_id, str) else None
                if not child_match or child_match[1] in child_seen:
                    quality["carousel_invalid"] += 1
                    quality["child_ids_invalid"] += 1
                if child_match:
                    child_seen.add(child_match[1])
                if not child_match or not child_match[2]:
                    quality["child_owner_unknown"] += 1
                elif child_match[2] != TARGET_ID:
                    quality["child_owner_mismatch"] += 1
                _media_type_quality(child, quality, required=False)
                _media_url_quality(child, quality)
        else:
            if children:
                quality["carousel_invalid"] += 1
            _media_url_quality(item, quality)
    return quality


def _cursor(data: dict) -> tuple[str, str | None]:
    has_more = data.get("has_more")
    cursor = data.get("nextMaxId", data.get("next_max_id"))
    if type(has_more) is not bool:
        return "unavailable", None
    if has_more is False:
        if cursor not in (None, ""):
            raise _Stop("invalid_response")
        return "exhausted", None
    if (not isinstance(cursor, str) or not 1 <= len(cursor) <= 256
            or any(ord(c) < 32 or ord(c) == 127 for c in cursor)):
        raise _Stop("invalid_response")
    return "available", cursor


def _json_object(body: bytes) -> dict:
    # Refuse ambiguous duplicate keys and non-JSON NaN/Infinity, rather than
    # accepting a misleading success/error or ID value chosen by the decoder.
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(_):
        raise ValueError

    data = json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(data, dict):
        raise ValueError
    return data


async def run_probe(*, transport: httpx.AsyncBaseTransport | None = None,
                    now: Callable[[], float] = time.time) -> dict:
    started = time.monotonic()
    observed = now()
    report = _new_report(observed)
    events = report["events"]
    seen = {name: set() for name in PATHS}
    incomplete = False

    async def get(client: httpx.AsyncClient, endpoint: str, params: dict, page: int = 1):
        if report["request_count"] >= REQUEST_LIMIT:
            raise _Stop("request_budget")
        event = {
            "endpoint": endpoint, "page": page, "http_status": None,
            "body_state": "not_read", "state": "pending",
        }
        events.append(event)
        report["request_count"] += 1
        # Even provider-set cookies are not reused on the next request.
        client.cookies.clear()
        async with asyncio.timeout(REQUEST_SECONDS):
            async with client.stream("GET", BASE_URL + PATHS[endpoint], params=params) as response:
                event["http_status"] = response.status_code
                if response.status_code != 200:
                    raise _Stop("stopped_http")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    event["body_state"] = "unsupported_encoding"
                    raise _Stop("invalid_response")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > BODY_LIMIT:
                        event["body_state"] = "too_large"
                        raise _Stop("response_too_large")
                    body.extend(chunk)
                try:
                    data = _json_object(bytes(body))
                except (ValueError, UnicodeError, RecursionError):
                    event["body_state"] = "invalid_json"
                    if b"cf-chl-" in body or b"challenges.cloudflare.com/turnstile" in body:
                        raise _Stop("challenge_required")
                    raise _Stop("invalid_response")
                event["body_state"] = "json"
                diagnostic = " ".join(str(data.get(key, ""))[:512]
                                      for key in ("error", "error_type", "note", "message"))
                if data.get("verification_required") is True or re.search(
                    r"captcha|turnstile", diagnostic, re.IGNORECASE
                ):
                    raise _Stop("challenge_required")
                if (any(data.get(key) for key in ("error", "errors", "error_type", "note", "toolDown"))
                        or ("success" in data and data["success"] is not True)):
                    raise _Stop("stopped_source")
                if data.get("status") != "success" or type(data.get("code")) is not int or data["code"] != 200:
                    raise _Stop("invalid_response")
                return data, event

    async def collection(client, endpoint, params, page=1):
        nonlocal incomplete
        data, event = await get(client, endpoint, params, page)
        items = data.get("data")
        if not isinstance(items, list) or len(items) > ITEM_LIMIT or any(not isinstance(x, dict) for x in items):
            raise _Stop("invalid_response")
        quality = _list_quality(items, endpoint, seen[endpoint], now())
        event.update(count=len(items), quality=quality,
                     state="observed" if items else "empty_unverified")
        if not items or any(quality.values()):
            incomplete = True
        cursor = None
        if endpoint in ("posts", "reels"):
            pagination, cursor = _cursor(data)
            event["pagination"] = pagination
            if pagination == "unavailable":
                incomplete = True
        return items, cursor, event

    try:
        async with asyncio.timeout(TOTAL_SECONDS):
            async with httpx.AsyncClient(
                transport=transport, trust_env=False, follow_redirects=False,
                timeout=REQUEST_SECONDS,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            ) as client:
                data, event = await get(client, "profile", {"username": TARGET})
                user = data.get("data", {}).get("user") if isinstance(data.get("data"), dict) else None
                if not isinstance(user, dict) or type(user.get("is_private")) is not bool:
                    raise _Stop("invalid_response")
                if (user.get("username") != TARGET or user.get("id") != TARGET_ID
                        or user.get("pk", TARGET_ID) != TARGET_ID):
                    raise _Stop("identity_mismatch")
                if user["is_private"]:
                    raise _Stop("private_profile")
                event.update(state="observed", identity_match=True)
                await collection(client, "stories", {"username": TARGET})
                _, posts_cursor, _ = await collection(client, "posts", {"username": TARGET, "limit": 24})
                _, reels_cursor, _ = await collection(client, "reels", {"username": TARGET, "limit": 12})
                albums, _, _ = await collection(client, "highlights", {"username": TARGET})
                if albums:
                    album = albums[0]
                    album_id = album.get("id")
                    if not isinstance(album_id, str) or not _ALBUM_ID.fullmatch(album_id):
                        raise _Stop("invalid_response")
                    items, _, event = await collection(client, "highlight_items", {
                        "highlight_id": album_id, "user": TARGET,
                    })
                    count = album.get("media_count")
                    if type(count) is not int or count != len(items):
                        event["quality"]["album_count_mismatch"] += 1
                        incomplete = True
                # A single continuation per feed, not an unbounded history crawl.
                for endpoint, cursor, limit in (("posts", posts_cursor, 24), ("reels", reels_cursor, 12)):
                    if cursor:
                        _, next_cursor, event = await collection(client, endpoint, {
                            "username": TARGET, "limit": limit, "maxId": cursor,
                        }, page=2)
                        event["cursor_advanced"] = next_cursor != cursor
                        if next_cursor == cursor:
                            incomplete = True
                report["outcome"] = "sample_incomplete" if incomplete else "sample_observed"
    except _Stop as exc:
        report["outcome"] = exc.outcome
    except (TimeoutError, httpx.TimeoutException):
        report["outcome"] = "deadline" if time.monotonic() - started >= TOTAL_SECONDS else "network_timeout"
    except httpx.HTTPError:
        report["outcome"] = "network_error"
    except Exception:
        # Exception messages/URLs can include source content: never serialize them.
        report["outcome"] = "runtime_error"
    finally:
        if events and report["outcome"] not in ("sample_observed", "sample_incomplete"):
            events[-1]["state"] = report["outcome"]
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return report


def exit_code(report: dict) -> int:
    return 0 if report["outcome"] == "sample_observed" else 3 if report["outcome"] == "runtime_error" else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="一次性 NASA 匿名 HTTP 探測；不會啟用正式來源。")
    parser.parse_args(argv)
    try:
        report = asyncio.run(run_probe())
    except (Exception, KeyboardInterrupt):
        report = _new_report(time.time())
    print(MARKER + json.dumps(report, ensure_ascii=True, separators=(",", ":")), flush=True)
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
