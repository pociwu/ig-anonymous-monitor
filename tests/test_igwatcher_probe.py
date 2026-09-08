"""Network-free acceptance and safety tests for the isolated IGWatcher probe.

The HTTP boundary is replaced with MockTransport. Fixtures contain synthetic
media, cursors and URLs; no production configuration, browser, media file or
external service is used.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import gzip
import json
from typing import Any

import httpx
import pytest

from ig_monitor import igwatcher_probe as probe


NOW = 1_788_912_000.0
OWNER = "528817151"
PREFIX = "[IGWATCHER-PROBE] "
PATHS = {
    "profile": "/wp-json/igw/v1/search",
    "stories": "/wp-json/igw/v1/stories",
    "posts": "/wp-json/igw/v1/posts",
    "reels": "/api/reels",
    "highlights": "/wp-json/igw/v1/highlights",
    "highlight_items": "/api/highlight-items",
}
ENDPOINTS = {path: endpoint for endpoint, path in PATHS.items()}


def media(identifier: str, *, reel: bool = False, story: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": f"{identifier}_{OWNER}",
        "shortcode": f"fixture{identifier}",
        "taken_at": int(NOW) - 3_600,
        "taken_at_timestamp": int(NOW) - 3_600,
        "product_type": "clips" if reel else "feed",
        "media_type": 2 if reel else 1,
        "is_video": reel,
        "image_url": "https://media.example.test/RAW_MEDIA_URL.jpg",
        "thumbnail_url": "https://media.example.test/RAW_THUMBNAIL_URL.jpg",
        "children": [],
    }
    if reel:
        item["video_url"] = "https://media.example.test/RAW_VIDEO_URL.mp4"
    if story:
        item["expiring_at"] = int(NOW) + 82_800
    return item


def envelope(data: Any, **metadata: Any) -> dict[str, Any]:
    return {"status": "success", "code": 200, "data": data, **metadata}


def baseline() -> dict[str, dict[str, Any]]:
    return {
        "profile": envelope({"user": {
            "username": "nasa", "id": OWNER, "pk": OWNER, "is_private": False,
        }}),
        "stories": envelope([media("3100000000000000001", story=True)]),
        "posts": envelope([media("3100000000000000002")], has_more=False),
        "reels": envelope([media("3100000000000000003", reel=True)], has_more=False),
        "highlights": envelope([{"id": "highlight:3100000000000000004", "media_count": 1}]),
        "highlight_items": envelope([media("3100000000000000005")]),
    }


class Scenario:
    def __init__(self, payloads: dict[str, Any] | None = None):
        self.payloads = deepcopy(baseline() if payloads is None else payloads)
        self.requests: list[httpx.Request] = []
        self.responses: dict[str, httpx.Response | Exception] = {}
        self.pages: dict[str, int] = {}
        self.second_pages: dict[str, Any] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "igwatcher.com"
        assert request.url.port in (None, 443)
        assert not request.url.userinfo
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        assert "proxy-authorization" not in request.headers
        endpoint = ENDPOINTS[request.url.path]
        if endpoint == "highlight_items":
            assert request.url.params["user"] == "nasa"
            assert request.url.params["highlight_id"] == "highlight:3100000000000000004"
        else:
            assert request.url.params["username"] == "nasa"
        self.pages[endpoint] = self.pages.get(endpoint, 0) + 1
        if endpoint in self.responses:
            response = self.responses[endpoint]
            if isinstance(response, Exception):
                raise response
            return response
        payload = self.payloads[endpoint]
        if self.pages[endpoint] == 2:
            assert endpoint in ("posts", "reels")
            cursor = self.payloads[endpoint].get("nextMaxId", self.payloads[endpoint].get("next_max_id"))
            assert request.url.params["maxId"] == cursor
            payload = self.second_pages[endpoint]
        assert self.pages[endpoint] <= 2, "Probe must not retry or fetch a third page"
        return httpx.Response(
            200, json=payload,
            headers={"Set-Cookie": "session=RAW_COOKIE_VALUE; Path=/; Secure"},
        )

    def run(self) -> dict[str, Any]:
        return asyncio.run(probe.run_probe(
            transport=httpx.MockTransport(self.handle), now=lambda: NOW,
        ))


def assert_safe_report(report: dict[str, Any]) -> None:
    encoded = json.dumps(report, allow_nan=False)
    assert report["source"] == "igwatcher"
    assert report["target"] == "nasa"
    assert report["production_ready"] is False
    assert report["schema_version"] >= 1
    assert report["platform"]
    assert report["architecture"]
    assert report["observed_at"]
    assert isinstance(report["elapsed_ms"], int)
    assert report["elapsed_ms"] >= 0
    assert 0 <= report["request_count"] <= 8
    assert len(report["events"]) <= 8
    for event in report["events"]:
        assert event["endpoint"] in PATHS
        assert event["page"] in (1, 2)
        assert "body_state" in event
        assert "state" in event
    for marker in (
        OWNER, "310000000000000000", "RAW_", "fixture-cursor", "fixture3100",
        "https://", "http://", "Set-Cookie", "Authorization",
    ):
        assert marker not in encoded


def test_complete_nonempty_sample_uses_six_cookie_free_requests():
    scenario = Scenario()
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_observed"
    assert probe.exit_code(report) == 0
    assert report["request_count"] == len(scenario.requests) == 6
    assert {event["endpoint"] for event in report["events"]} == set(PATHS)
    assert all(event["http_status"] == 200 for event in report["events"])
    assert all(event["page"] == 1 for event in report["events"])


def test_http_client_disables_environment_and_redirects(monkeypatch):
    constructor = httpx.AsyncClient
    observed: list[dict[str, Any]] = []

    def recording_client(*args, **kwargs):
        observed.append(dict(kwargs))
        return constructor(*args, **kwargs)

    monkeypatch.setattr(probe.httpx, "AsyncClient", recording_client)
    report = Scenario().run()
    assert report["outcome"] == "sample_observed"
    assert len(observed) == 1
    assert observed[0]["trust_env"] is False
    assert observed[0].get("follow_redirects", False) is False
    assert not observed[0].get("proxy")
    assert not observed[0].get("auth")


@pytest.mark.parametrize("status", [201, 204, 301, 302, 307, 308, 400, 401, 403, 404, 422, 429, 500, 503])
def test_every_non200_status_stops_without_retry_or_redirect(status):
    scenario = Scenario()
    scenario.responses["profile"] = httpx.Response(
        status,
        content=b"RAW_ERROR_BODY",
        headers={"Location": "https://other.example.test/RAW_REDIRECT", "Set-Cookie": "RAW_COOKIE_VALUE"},
    )
    report = scenario.run()
    assert_safe_report(report)
    assert report["request_count"] == len(scenario.requests) == 1
    assert report["events"][0]["http_status"] == status
    assert report["outcome"] != "sample_observed"
    assert probe.exit_code(report) == 2


@pytest.mark.parametrize("content", [b"", b"not-json RAW_BODY", b"<html>RAW_HTML_BODY</html>", b"{", b"null", b"[]", b"true"])
def test_unusable_body_is_fail_closed(content):
    scenario = Scenario()
    scenario.responses["profile"] = httpx.Response(200, content=content)
    report = scenario.run()
    assert_safe_report(report)
    assert len(scenario.requests) == 1
    assert probe.exit_code(report) == 2


@pytest.mark.parametrize("payload", [
    {"status": "error", "code": 200, "data": {"user": {"username": "nasa"}}, "message": "RAW_MESSAGE"},
    {"status": "success", "code": 403, "data": {"user": {"username": "nasa"}}},
    {"status": "success", "code": 200, "data": None},
    {"status": "success", "code": 200, "data": {}},
    {"status": "success", "code": 200, "data": [], "error": "turnstile_required RAW_TOKEN"},
    {"success": False, "error": "RAW_FALSE_ERROR"},
])
def test_invalid_envelope_or_challenge_stops(payload):
    scenario = Scenario()
    scenario.payloads["profile"] = payload
    report = scenario.run()
    assert_safe_report(report)
    assert len(scenario.requests) == 1
    assert probe.exit_code(report) == 2


@pytest.mark.parametrize(("field", "value"), [
    ("username", "RAW_OTHER_ACCOUNT"), ("id", "999"), ("pk", "999"),
    ("id", 528817151), ("is_private", True),
])
def test_wrong_or_unsafe_profile_cannot_trigger_content_requests(field, value):
    scenario = Scenario()
    scenario.payloads["profile"]["data"]["user"][field] = value
    report = scenario.run()
    assert_safe_report(report)
    assert len(scenario.requests) == 1
    assert probe.exit_code(report) == 2


@pytest.mark.parametrize("endpoint", ["stories", "posts", "reels", "highlights", "highlight_items"])
def test_valid_empty_list_is_incomplete_not_observed(endpoint):
    scenario = Scenario()
    scenario.payloads[endpoint]["data"] = []
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_incomplete"
    assert probe.exit_code(report) == 2
    if endpoint == "highlights":
        assert not any(request.url.path == PATHS["highlight_items"] for request in scenario.requests)


@pytest.mark.parametrize("endpoint", ["stories", "posts", "reels", "highlights", "highlight_items"])
def test_nonarray_collection_stops_without_later_requests(endpoint):
    scenario = Scenario()
    scenario.payloads[endpoint]["data"] = {"message": "RAW_INVALID_LIST"}
    report = scenario.run()
    assert_safe_report(report)
    assert probe.exit_code(report) == 2
    assert report["events"][-1]["endpoint"] == endpoint


@pytest.mark.parametrize(("endpoint", "field", "value"), [
    ("posts", "id", 3100000000000000002),
    ("posts", "id", "3100000000000000002_999"),
    ("posts", "id", ""),
    ("posts", "taken_at_timestamp", 0),
    ("posts", "taken_at_timestamp", int(NOW) + 86_400),
    ("posts", "image_url", "javascript:RAW_MEDIA_URL"),
    ("posts", "image_url", "http://media.example.test/RAW_MEDIA_URL"),
    ("reels", "product_type", "feed"),
    ("reels", "video_url", ""),
    ("stories", "expiring_at", int(NOW) - 1),
    ("stories", "taken_at", 0),
    ("highlight_items", "id", "3100000000000000005_999"),
    ("highlight_items", "taken_at", 0),
])
def test_quality_gaps_never_become_success(endpoint, field, value):
    scenario = Scenario()
    scenario.payloads[endpoint]["data"][0][field] = value
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_incomplete"
    assert probe.exit_code(report) == 2


@pytest.mark.parametrize("port", ["0", "-1", "65536", "RAW_BAD_PORT"])
def test_invalid_media_url_port_is_a_quality_gap(port):
    scenario = Scenario()
    scenario.payloads["posts"]["data"][0]["image_url"] = f"https://media.example.test:{port}/RAW_MEDIA.jpg"
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_incomplete"
    event = next(event for event in report["events"] if event["endpoint"] == "posts")
    assert event["quality"]["media_url_invalid"] == 1


@pytest.mark.parametrize("kind", [None, True, "1", 0, 3, 9])
def test_missing_or_invalid_required_media_type_is_a_quality_gap(kind):
    scenario = Scenario()
    item = scenario.payloads["posts"]["data"][0]
    if kind is None:
        del item["media_type"]
    else:
        item["media_type"] = kind
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_incomplete"
    event = next(event for event in report["events"] if event["endpoint"] == "posts")
    assert event["quality"]["media_type_unconfirmed"] >= 1


@pytest.mark.parametrize(("kind", "video"), [(1, True), (2, False), (1, "false"), (2, 1)])
def test_contradictory_or_nonboolean_video_flag_is_a_quality_gap(kind, video):
    scenario = Scenario()
    item = scenario.payloads["posts"]["data"][0]
    item.update(media_type=kind, is_video=video, video_url="https://media.example.test/RAW_VIDEO_URL.mp4")
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_incomplete"
    event = next(event for event in report["events"] if event["endpoint"] == "posts")
    assert event["quality"]["media_type_unconfirmed"] >= 1


@pytest.mark.parametrize("identifier", [0, "0", f"0_{OWNER}", f"03100000000000000002_{OWNER}", "3100000000000000002_0", "3100000000000000002_0528817151"])
def test_zero_or_leading_zero_media_identifiers_are_invalid(identifier):
    scenario = Scenario()
    scenario.payloads["posts"]["data"][0]["id"] = identifier
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_incomplete"
    event = next(event for event in report["events"] if event["endpoint"] == "posts")
    assert event["quality"]["invalid_ids"] == 1


@pytest.mark.parametrize("identifier", ["highlight:0", "highlight:03100000000000000004"])
def test_zero_or_leading_zero_album_identifiers_stop_before_item_request(identifier):
    scenario = Scenario()
    scenario.payloads["highlights"]["data"][0]["id"] = identifier
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "invalid_response"
    assert "highlight_items" not in scenario.pages
    assert report["events"][-1]["quality"]["invalid_ids"] == 1


def test_response_over_two_mebibytes_is_rejected_and_not_printed():
    scenario = Scenario()
    scenario.responses["profile"] = httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
    report = scenario.run()
    assert_safe_report(report)
    assert len(scenario.requests) == 1
    assert probe.exit_code(report) == 2


def test_compressed_body_is_rejected_instead_of_expanding_it():
    scenario = Scenario()
    compressed = gzip.compress(b"x" * (2 * 1024 * 1024 + 1))
    assert len(compressed) < 2 * 1024 * 1024
    scenario.responses["profile"] = httpx.Response(
        200, content=compressed,
        headers={"Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
    )
    report = scenario.run()
    assert_safe_report(report)
    assert len(scenario.requests) == 1
    assert report["outcome"] == "invalid_response"
    assert report["events"][0]["body_state"] == "unsupported_encoding"
    assert probe.exit_code(report) == 2


def test_more_than_one_hundred_items_is_fail_closed():
    scenario = Scenario()
    scenario.payloads["stories"]["data"] *= 101
    report = scenario.run()
    assert_safe_report(report)
    assert probe.exit_code(report) == 2
    assert report["events"][-1]["endpoint"] == "stories"


@pytest.mark.parametrize("cursor_field", ["nextMaxId", "next_max_id"])
def test_two_page_posts_and_reels_use_at_most_eight_requests(cursor_field):
    scenario = Scenario()
    for endpoint, identifier in (("posts", "3100000000000000006"), ("reels", "3100000000000000007")):
        scenario.payloads[endpoint].update(has_more=True, **{cursor_field: "fixture-cursor"})
        scenario.second_pages[endpoint] = envelope(
            [media(identifier, reel=endpoint == "reels")], has_more=False,
        )
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_observed"
    assert probe.exit_code(report) == 0
    assert report["request_count"] == len(scenario.requests) == 8
    assert scenario.pages["posts"] == scenario.pages["reels"] == 2
    assert sum(event["page"] == 2 for event in report["events"]) == 2


@pytest.mark.parametrize("cursor", [None, "", 123, False, "x" * 257, "bad\r\nRAW_CURSOR", "bad\x00RAW_CURSOR"])
def test_invalid_or_missing_continuation_is_not_requested(cursor):
    scenario = Scenario()
    scenario.payloads["posts"].update(has_more=True, nextMaxId=cursor)
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "invalid_response"
    assert probe.exit_code(report) == 2
    assert scenario.pages["posts"] == 1
    assert len(scenario.requests) == 3


def test_maximum_cursor_is_only_an_encoded_query_value():
    scenario = Scenario()
    cursor = "RAW_QUERY_CURSOR/https://other.example.test/?token=".ljust(256, "x")
    assert len(cursor) == 256
    scenario.payloads["posts"].update(has_more=True, nextMaxId=cursor)
    scenario.second_pages["posts"] = envelope([media("3100000000000000006")], has_more=False)
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_observed"
    assert scenario.pages["posts"] == 2
    assert len(scenario.requests) == 7


@pytest.mark.parametrize("has_more", [None, "true", 1, {}, []])
def test_missing_or_nonboolean_pagination_metadata_is_incomplete(has_more):
    scenario = Scenario()
    if has_more is None:
        del scenario.payloads["posts"]["has_more"]
    else:
        scenario.payloads["posts"]["has_more"] = has_more
    scenario.payloads["posts"]["nextMaxId"] = "fixture-cursor"
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_incomplete"
    assert scenario.pages["posts"] == 1


def test_repeated_continuation_media_is_not_accepted_as_new_content():
    scenario = Scenario()
    scenario.payloads["posts"].update(has_more=True, nextMaxId="fixture-cursor")
    scenario.second_pages["posts"] = envelope(deepcopy(scenario.payloads["posts"]["data"]), has_more=False)
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_incomplete"
    assert probe.exit_code(report) == 2
    assert scenario.pages["posts"] == 2


def test_remaining_third_page_is_not_requested():
    scenario = Scenario()
    scenario.payloads["posts"].update(has_more=True, nextMaxId="fixture-cursor")
    scenario.second_pages["posts"] = envelope(
        [media("3100000000000000006")], has_more=True, nextMaxId="RAW_THIRD_CURSOR",
    )
    report = scenario.run()
    assert_safe_report(report)
    assert scenario.pages["posts"] == 2
    assert len(scenario.requests) == 7


def test_request_budget_is_enforced_before_starting_another_request(monkeypatch):
    monkeypatch.setattr(probe, "REQUEST_LIMIT", 3)
    scenario = Scenario()
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "request_budget"
    assert report["request_count"] == len(scenario.requests) == 3


@pytest.mark.parametrize(("request_seconds", "total_seconds", "outcome"), [
    (0.01, 10.0, "network_timeout"),
    (10.0, 0.01, "deadline"),
])
def test_request_and_total_deadlines_bound_hanging_transport(monkeypatch, request_seconds, total_seconds, outcome):
    monkeypatch.setattr(probe, "REQUEST_SECONDS", request_seconds)
    monkeypatch.setattr(probe, "TOTAL_SECONDS", total_seconds)
    request_count = 0

    async def hanging_handler(_request):
        nonlocal request_count
        request_count += 1
        await asyncio.sleep(10)
        raise AssertionError("Transport should have been cancelled")

    report = asyncio.run(probe.run_probe(
        transport=httpx.MockTransport(hanging_handler), now=lambda: NOW,
    ))
    assert_safe_report(report)
    assert report["outcome"] == outcome
    assert report["request_count"] == request_count == 1
    assert probe.exit_code(report) == 2


class UnreadBody(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False
        self.read = False

    async def __aiter__(self):
        self.read = True
        raise AssertionError("Rejected responses must not read the body")
        yield b""  # pragma: no cover - defines the asynchronous iterator protocol

    async def aclose(self):
        self.closed = True


class SlowBody(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False
        self.started = False

    async def __aiter__(self):
        self.started = True
        yield b"{"
        await asyncio.sleep(10)
        raise AssertionError("Body reader should have been cancelled")

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(("request_seconds", "total_seconds", "outcome"), [
    (0.01, 10.0, "network_timeout"),
    (10.0, 0.01, "deadline"),
])
def test_deadlines_include_streaming_body_and_close_it(monkeypatch, request_seconds, total_seconds, outcome):
    monkeypatch.setattr(probe, "REQUEST_SECONDS", request_seconds)
    monkeypatch.setattr(probe, "TOTAL_SECONDS", total_seconds)
    scenario = Scenario()
    stream = SlowBody()
    scenario.responses["profile"] = httpx.Response(200, stream=stream)
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == outcome
    assert stream.started and stream.closed
    assert report["request_count"] == len(scenario.requests) == 1


def test_body_limit_stops_stream_at_first_excess_chunk_and_closes_it():
    class LargeBody(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False
            self.chunks = 0

        async def __aiter__(self):
            for _ in range(34):
                self.chunks += 1
                yield b"x" * 65_536
            raise AssertionError("Oversized response must not be fully consumed")

        async def aclose(self):
            self.closed = True

    scenario = Scenario()
    stream = LargeBody()
    scenario.responses["profile"] = httpx.Response(200, stream=stream)
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "response_too_large"
    assert stream.chunks == 33
    assert stream.closed
    assert report["request_count"] == len(scenario.requests) == 1


@pytest.mark.parametrize(("status", "headers", "outcome"), [
    (403, {}, "stopped_http"),
    (302, {"Location": "https://other.example.test/RAW_REDIRECT"}, "stopped_http"),
    (200, {"Content-Encoding": "gzip"}, "invalid_response"),
    (200, {"Content-Encoding": "br"}, "invalid_response"),
])
def test_rejected_response_is_closed_without_reading_body(status, headers, outcome):
    scenario = Scenario()
    stream = UnreadBody()
    scenario.responses["profile"] = httpx.Response(status, headers=headers, stream=stream)
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == outcome
    assert not stream.read
    assert stream.closed
    assert len(scenario.requests) == 1


@pytest.mark.parametrize("content", [
    b'{"status":"success","status":"error","code":200,"data":{}}',
    b'{"status":"success","code":200,"data":{"bad":NaN}}',
    b'{"status":"success","code":200,"data":{"bad":Infinity}}',
])
def test_ambiguous_or_nonstandard_json_is_rejected(content):
    scenario = Scenario()
    scenario.responses["profile"] = httpx.Response(200, content=content)
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "invalid_response"
    assert report["events"][0]["body_state"] == "invalid_json"


@pytest.mark.parametrize("field", ["error", "note", "error_type", "errors", "toolDown"])
def test_nonempty_source_diagnostic_overrides_success_data(field):
    scenario = Scenario()
    scenario.payloads["profile"][field] = "RAW_SOURCE_MESSAGE"
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "stopped_source"
    assert len(scenario.requests) == 1


@pytest.mark.parametrize(("field", "value"), [
    ("errors", ["RAW_SOURCE_ERROR"]),
    ("errors", {"details": "RAW_SOURCE_ERROR"}),
    ("toolDown", True),
])
def test_structured_source_error_overrides_success_envelope(field, value):
    scenario = Scenario()
    scenario.payloads["profile"][field] = value
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "stopped_source"
    assert len(scenario.requests) == 1


@pytest.mark.parametrize("field", ["error", "error_type", "note", "message"])
def test_challenge_marker_overrides_success_envelope(field):
    scenario = Scenario()
    scenario.payloads["profile"][field] = "turnstile_required RAW_CHALLENGE_TOKEN"
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "challenge_required"
    assert len(scenario.requests) == 1


def test_unknown_success_metadata_is_not_serialized():
    scenario = Scenario()
    for payload in scenario.payloads.values():
        payload["debug"] = {"url": "https://RAW_SOURCE_HOST/RAW_SOURCE_PATH", "token": "RAW_SECRET_TOKEN"}
    for endpoint in ("stories", "posts", "reels", "highlight_items"):
        scenario.payloads[endpoint]["data"][0]["caption"] = "RAW_PERSONAL_CAPTION"
        scenario.payloads[endpoint]["data"][0]["owner"] = {"full_name": "RAW_PERSON_NAME"}
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_observed"


def test_valid_carousel_parent_with_ordered_children_is_observed():
    scenario = Scenario()
    post = scenario.payloads["posts"]["data"][0]
    post.update(media_type=8, is_carousel=True, carousel_count=2, children=[
        media("3100000000000000010"), media("3100000000000000011", reel=True),
    ])
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_observed"


@pytest.mark.parametrize("kind", ["missing", "duplicate", "numeric", "wrong_owner", "unknown_owner", "too_many", "count_mismatch", "nonobject"])
def test_invalid_carousel_children_are_incomplete(kind):
    scenario = Scenario()
    post = scenario.payloads["posts"]["data"][0]
    children: list[Any] = [media("3100000000000000010"), media("3100000000000000011")]
    count = 2
    if kind == "missing":
        children = []
    elif kind == "duplicate":
        children[1] = deepcopy(children[0])
    elif kind == "numeric":
        children[0]["id"] = 3100000000000000010
    elif kind == "wrong_owner":
        children[0]["id"] = "3100000000000000010_999"
    elif kind == "unknown_owner":
        children[0]["id"] = "3100000000000000010"
    elif kind == "too_many":
        children *= 11
        count = len(children)
    elif kind == "count_mismatch":
        count = 3
    elif kind == "nonobject":
        children[0] = "RAW_BAD_CHILD"
    post.update(media_type=8, is_carousel=True, carousel_count=count, children=children)
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_incomplete"
    assert probe.exit_code(report) == 2


@pytest.mark.parametrize("exception", [
    httpx.ConnectError("RAW_CONNECT_ERROR"),
    httpx.ReadTimeout("RAW_TIMEOUT"),
    httpx.RemoteProtocolError("RAW_PROTOCOL_ERROR"),
])
def test_transport_error_stops_without_raw_exception(exception):
    scenario = Scenario()
    scenario.responses["profile"] = exception
    report = scenario.run()
    assert_safe_report(report)
    assert len(scenario.requests) == 1
    assert probe.exit_code(report) == 2


def test_unexpected_error_is_sanitized_runtime_error():
    scenario = Scenario()
    scenario.responses["profile"] = RuntimeError("RAW_RUNTIME_TRACE")
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "runtime_error"
    assert probe.exit_code(report) == 3


def test_main_prints_one_sanitized_json_report(monkeypatch, capsys):
    sample = Scenario().run()

    async def fake_run_probe(**_kwargs):
        return deepcopy(sample)

    monkeypatch.setattr(probe, "run_probe", fake_run_probe)
    assert probe.main([]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(PREFIX)
    assert json.loads(lines[0][len(PREFIX):]) == sample


def test_main_converts_unexpected_failure_to_one_safe_runtime_report(monkeypatch, capsys):
    async def broken_run_probe(**_kwargs):
        raise RuntimeError("RAW_RUNTIME_MESSAGE https://RAW_PRIVATE_HOST/RAW_TOKEN")

    monkeypatch.setattr(probe, "run_probe", broken_run_probe)
    assert probe.main([]) == 3
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1 and lines[0].startswith(PREFIX)
    report = json.loads(lines[0][len(PREFIX):])
    assert_safe_report(report)
    assert report["outcome"] == "runtime_error"
    assert report["request_count"] == 0
