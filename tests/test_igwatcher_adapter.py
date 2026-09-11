"""Conservative adapter contracts at the scraper and HTTP transport seams."""
from __future__ import annotations

import asyncio
import copy
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from ig_monitor.config import BrowserConfig
from ig_monitor.igwatcher import IGWatcherScraper
from ig_monitor.models import ScrapeFailure, TerminalState


def config(**changes):
    return BrowserConfig(True, 15, 0, Path("unused"), anonymous_source="igwatcher", **changes)


def profile(**changes):
    return {"status": "success", "code": 200, "data": {"user": {
        "id": "123", "pk": "123", "username": "alice", "is_private": False,
        "media_count": 15, "follower_count": 1200, "following_count": 30,
        "full_name": "Alice", "biography": "Public profile",
        "profile_pic_url": "https://scontent.cdninstagram.com/avatar.jpg", **changes,
    }}}


def test_profile_only_returns_exact_counts_without_browser_or_media_requests():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=profile())

    async def run():
        async with IGWatcherScraper(config(), transport=httpx.MockTransport(respond)) as scraper:
            return await scraper.scrape_profile_only("https://instagram.com/alice/")

    snapshot = asyncio.run(run())
    assert (snapshot.username, snapshot.posts, snapshot.followers, snapshot.following) == ("alice", 15, 1200, 30)
    assert len(requests) == 1
    assert requests[0].url.path == "/wp-json/igw/v1/search"
    assert "cookie" not in requests[0].headers


def test_profile_accepts_observed_igwatcher_graph_counts_without_media_count():
    """2026-09-09 search shape; synthetic identity/counts, no saved source data."""
    payload = profile()
    user = payload["data"]["user"]
    del user["media_count"]
    user.update({
        "edge_owner_to_timeline_media": {"count": 15},
        "edge_followed_by": {"count": 1200},
        "edge_follow": {"count": 30},
    })
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    async def run():
        async with IGWatcherScraper(config(), transport=httpx.MockTransport(respond)) as scraper:
            return await scraper.scrape_profile_only("https://instagram.com/alice/")

    snapshot = asyncio.run(run())
    assert (snapshot.posts, snapshot.followers, snapshot.following) == (15, 1200, 30)
    assert len(requests) == 1


PROFILE_COUNT_FIELDS = (
    ("media_count", "edge_owner_to_timeline_media"),
    ("follower_count", "edge_followed_by"),
    ("following_count", "edge_follow"),
)


def profile_with_both_count_locations():
    payload = profile()
    user = payload["data"]["user"]
    for direct, edge in PROFILE_COUNT_FIELDS:
        user[edge] = {"count": user[direct]}
    return payload


@pytest.mark.parametrize("location", ["direct", "graph", "both"])
@pytest.mark.parametrize("zero", [False, True])
def test_profile_count_locations_agree_and_accept_real_zero(location, zero):
    payload = profile_with_both_count_locations()
    user = payload["data"]["user"]
    for direct, edge in PROFILE_COUNT_FIELDS:
        if zero:
            user[direct] = user[edge]["count"] = 0
        if location == "direct":
            del user[edge]
        elif location == "graph":
            del user[direct]
    base = transport_for()

    def respond(request):
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=payload)
        return base.handler(request)

    snapshot = scrape_with(httpx.MockTransport(respond)).snapshot
    assert (snapshot.posts, snapshot.followers, snapshot.following) == ((0, 0, 0) if zero else (15, 1200, 30))


def assert_bad_profile_stops_before_media(payload):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    with pytest.raises(ScrapeFailure, match="個人檔案計數"):
        scrape_with(httpx.MockTransport(respond))
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/search")


@pytest.mark.parametrize("direct,edge", PROFILE_COUNT_FIELDS)
@pytest.mark.parametrize("location", ["direct", "graph"])
@pytest.mark.parametrize("invalid", [None, True, False, -1, 1.5, "15", "1.2K", [], {}])
def test_invalid_present_profile_count_is_not_hidden_by_valid_alias(direct, edge, location, invalid):
    payload = profile_with_both_count_locations()
    user = payload["data"]["user"]
    if location == "direct":
        user[direct] = invalid
    else:
        user[edge]["count"] = invalid
    assert_bad_profile_stops_before_media(payload)


@pytest.mark.parametrize("direct,edge", PROFILE_COUNT_FIELDS)
@pytest.mark.parametrize("invalid", [None, [], {}, 15, True, "secret provider payload"])
def test_malformed_profile_count_container_is_not_hidden_by_valid_direct_count(direct, edge, invalid):
    payload = profile_with_both_count_locations()
    payload["data"]["user"][edge] = invalid
    assert_bad_profile_stops_before_media(payload)


@pytest.mark.parametrize("direct,edge", PROFILE_COUNT_FIELDS)
def test_missing_all_profile_count_locations_is_not_zero(direct, edge):
    payload = profile_with_both_count_locations()
    user = payload["data"]["user"]
    del user[direct], user[edge]
    assert_bad_profile_stops_before_media(payload)


@pytest.mark.parametrize("direct,edge", PROFILE_COUNT_FIELDS)
def test_disagreeing_profile_counts_do_not_choose_an_arbitrary_alias(direct, edge):
    payload = profile_with_both_count_locations()
    payload["data"]["user"][edge]["count"] += 1
    assert_bad_profile_stops_before_media(payload)


def test_profile_block_stops_before_collection_requests_and_redacts_response_text():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(403, text="secret provider payload")

    async def run():
        async with IGWatcherScraper(config(), transport=httpx.MockTransport(respond)) as scraper:
            return await scraper.scrape("https://instagram.com/alice/")

    with pytest.raises(ScrapeFailure) as error:
        asyncio.run(run())
    assert error.value.blocker
    assert "secret" not in str(error.value)
    assert len(requests) == 1


def envelope(items, **extra):
    return {"status": "success", "code": 200, "data": items, **extra}


def test_failed_highlight_album_does_not_starve_later_batches():
    albums = [{"id": f"highlight:{index}", "title": "Album", "media_count": 1} for index in (400, 401)]
    requests = []

    def respond(request):
        if request.url.path == "/wp-json/igw/v1/search":
            return httpx.Response(200, json=profile())
        if request.url.path == "/wp-json/igw/v1/highlights":
            return httpx.Response(200, json=envelope(albums))
        if request.url.path == "/api/highlight-items":
            album = request.url.params["highlight_id"]
            requests.append(album)
            if album == "highlight:400":
                return httpx.Response(503, text="temporarily unavailable")
            return httpx.Response(200, json=envelope([{
                "id": "900_123", "taken_at": int(time.time()) - 600,
                "image_url": "https://scontent.cdninstagram.com/image.jpg",
            }]))
        return httpx.Response(200, json=envelope([], has_more=False))

    async def run():
        async with IGWatcherScraper(config(max_pages_per_collection=1), transport=httpx.MockTransport(respond)) as scraper:
            first = await scraper.scrape("https://instagram.com/alice/")
            second = await scraper.scrape("https://instagram.com/alice/", cursors={
                "highlights": first.collections["highlights"].cursor,
            })
            return first, second

    first, second = asyncio.run(run())
    assert first.collections["highlights"].error
    assert first.collections["highlights"].cursor == "after:highlight:400"
    assert requests == ["highlight:400", "highlight:401"]
    assert [item.source_media_id for item in second.media] == ["900_123"]
    assert second.collections["highlights"].cursor is None


def photo(number="100_123", **changes):
    return {"id": number, "media_type": 1, "is_video": False, "is_carousel": False,
            "taken_at_timestamp": int(time.time()) - 600,
            "image_url": "https://scontent.cdninstagram.com/photo.jpg", **changes}


def reel(number="101_987", **changes):
    return photo(number, media_type=2, is_video=True, product_type="clips",
                 video_url="https://video.cdninstagram.com/reel.mp4", **changes)


def carousel(children=None, **changes):
    children = children or [
        {"is_video": False, "image_url": "https://scontent.cdninstagram.com/a.jpg"},
        {"is_video": True, "video_url": "https://video.cdninstagram.com/b.mp4"},
    ]
    return photo("200_999", media_type=8, is_carousel=True, children=children,
                 carousel_count=len(children), **changes)


def transport_for(*, posts=None, reels=None, stories=None, albums=None, album_items=None):
    payloads = {
        "/wp-json/igw/v1/search": profile(),
        "/wp-json/igw/v1/stories": envelope(stories or []),
        "/wp-json/igw/v1/posts": envelope(posts or [], has_more=False, nextMaxId=None),
        "/api/reels": envelope(reels or [], has_more=False, nextMaxId=None),
        "/wp-json/igw/v1/highlights": envelope(albums or []),
        "/api/highlight-items": envelope(album_items or []),
    }
    return httpx.MockTransport(lambda request: httpx.Response(200, json=payloads[request.url.path]))


def scrape_with(transport, **options):
    async def run():
        async with IGWatcherScraper(config(**options), transport=transport) as scraper:
            return await scraper.scrape("https://instagram.com/alice/")
    return asyncio.run(run())


@pytest.mark.parametrize("count", [1, 2])
def test_feed_videos_returned_by_reels_are_saved_as_posts_without_collection_failure(count):
    # Observed on 2026-09-11; synthetic IDs/URLs, same explicit feed/video shape.
    items = [photo(f"{400 + index}_123", media_type=2, is_video=True,
                   product_type="feed", children=[],
                   video_url="https://video.cdninstagram.com/feed.mp4") for index in range(count)]
    result = scrape_with(transport_for(reels=items))
    assert result.collections["reels"].error is None
    assert not result.collections["reels"].complete
    assert len(result.media) == count
    assert all(item.category == "posts" and item.kind == "video"
               and item.ownership_status == "pending" for item in result.media)
    assert all(group.category == "posts" for group in result.groups)


@pytest.mark.parametrize("change", [
    {"product_type": None}, {"product_type": "unknown"}, {"product_type": {}},
    {"is_video": False}, {"media_type": 1}, {"video_url": None},
    {"video_url": "https://localhost/private"}, {"taken_at_timestamp": None},
    {"is_carousel": True}, {"children": [{"id": "999"}]},
])
def test_reels_feed_fallback_does_not_hide_invalid_data(change):
    bad = photo("400_123", media_type=2, is_video=True, product_type="feed",
                video_url="https://video.cdninstagram.com/feed.mp4")
    bad.update(change)
    result = scrape_with(transport_for(reels=[bad, reel()]))
    assert result.collections["reels"].error
    assert len(result.media) == 1
    assert result.media[0].category == "reels"


def test_mixed_reels_and_feed_videos_keep_their_actual_categories():
    feed = photo("400_123", media_type=2, is_video=True, product_type="feed",
                 video_url="https://video.cdninstagram.com/feed.mp4")
    result = scrape_with(transport_for(reels=[feed, reel()]))
    assert result.collections["reels"].error is None
    assert [(item.source_media_id, item.category) for item in result.media] == [
        ("400_123", "posts"), ("101_987", "reels")]
    direct = scrape_with(transport_for(posts=[feed]))
    assert direct.media[0].media_key == result.media[0].media_key


def test_feed_fallback_recovers_incident_once_and_repeated_observation_keeps_one_media(tmp_path):
    from ig_monitor.config import AccountConfig
    from ig_monitor.db import Database
    from ig_monitor.models import CollectionObservation

    feed = photo("400_123", media_type=2, is_video=True, product_type="feed",
                 video_url="https://video.cdninstagram.com/feed.mp4")
    result = scrape_with(transport_for(posts=[feed], reels=[feed]))
    db = Database(tmp_path / "state.sqlite3")
    try:
        db.sync_accounts([AccountConfig("https://instagram.com/alice/", True, "Alice")])
        account_id = db.enabled_accounts()[0]["id"]
        for _ in range(3):
            db.record_collection_observations(account_id, "Alice", {
                "reels": CollectionObservation(TerminalState.PARTIAL, error="old parser failure")
            }, source="igwatcher")
        for _ in range(2):
            db.record_success(account_id, result.snapshot, [], result.media)
            db.record_group_observations(account_id, "igwatcher", result.groups)
            db.record_collection_observations(account_id, "Alice", result.collections, source="igwatcher")
        state = db.collection_observations(account_id, "igwatcher")["reels"]
        assert state["fail_count"] == 0 and not state["complete"]
        assert [e["kind"] for e in db.pending_events(20)] == ["failure", "recovery"]
        assert len(db.pending_media(account_id, 20)) == 1
    finally:
        db.close()


def test_all_four_categories_are_saved_as_pending_without_invented_ownership():
    now = int(time.time())
    story = {"id": "301_123", "taken_at": now - 60, "expiring_at": now + 80000,
             "image_url": "https://scontent.cdninstagram.com/story.jpg"}
    highlight = {"id": "302_123", "taken_at": now - 600000,
                 "image_url": "https://scontent.cdninstagram.com/old.jpg"}
    result = scrape_with(transport_for(posts=[photo()], reels=[reel()], stories=[story],
        albums=[{"id": "highlight:400", "media_count": 2, "title": "Album"}], album_items=[highlight]))
    assert {item.category for item in result.media} == {"posts", "stories", "highlights", "reels"}
    assert all(item.ownership_status == "pending" and item.queried_username == "alice" for item in result.media)
    assert all(item.owner_username is None for item in result.media)
    assert {item.kind for item in result.media} == {"image", "video"}
    assert all(value.state == TerminalState.PARTIAL and not value.complete and value.error is None
               for value in result.collections.values())
    album = next(group for group in result.groups if group.category == "highlights")
    assert (album.declared_count, album.received_count, album.complete) == (2, 1, False)


def test_local_carousel_identity_is_repeatable_but_changes_with_order_or_url():
    parent = carousel()
    first = scrape_with(transport_for(posts=[parent]))
    repeated = scrape_with(transport_for(posts=[parent]))
    reordered = copy.deepcopy(parent)
    reordered["children"].reverse()
    second = scrape_with(transport_for(posts=[reordered]))
    replaced = copy.deepcopy(parent)
    replaced["children"][0]["image_url"] = "https://scontent.cdninstagram.com/new.jpg"
    third = scrape_with(transport_for(posts=[replaced]))
    assert len(first.media) == 2
    assert [item.position for item in first.media] == [0, 1]
    assert all(item.identity_kind == "local" and item.source_media_id is None
               and item.media_key.startswith("igwatcher:local:") for item in first.media)
    assert [item.media_key for item in first.media] == [item.media_key for item in repeated.media]
    assert {item.media_key for item in first.media}.isdisjoint(item.media_key for item in second.media)
    assert first.groups[0].revision_id != third.groups[0].revision_id
    assert first.groups[0].declared_count == first.groups[0].received_count == 2


def test_private_profile_never_fetches_media():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=profile(is_private=True))
    result = scrape_with(httpx.MockTransport(respond))
    assert not result.media
    assert len(requests) == 1
    assert len(result.collections) == 4
    assert all(value.state == TerminalState.PRIVATE for value in result.collections.values())


def test_latest_logical_post_window_follows_bounded_pages_and_keeps_all_children():
    requests = []
    base = transport_for()
    def respond(request):
        requests.append(request)
        if request.url.path == "/wp-json/igw/v1/posts":
            if not request.url.params.get("maxId"):
                return httpx.Response(200, json=envelope([photo()], has_more=True, nextMaxId="opaque:one"))
            assert request.url.params["maxId"] == "opaque:one"
            return httpx.Response(200, json=envelope([carousel(), photo("333_123")], has_more=True, nextMaxId="opaque:two"))
        return base.handler(request)
    result = scrape_with(httpx.MockTransport(respond), initial_posts=2, max_pages_per_collection=2)
    assert [item.parent_id for item in result.media] == ["100_123", "200_999", "200_999"]
    assert len([request for request in requests if request.url.path.endswith("/posts")]) == 2
    assert not result.collections["posts"].complete


def test_highlight_batches_resume_and_later_round_revisits_without_claiming_complete():
    albums = [{"id": f"highlight:{400 + number}", "media_count": 0, "title": "Album"} for number in range(3)]
    requested_albums = []
    base = transport_for(albums=albums)
    def respond(request):
        if request.url.path == "/api/highlight-items":
            requested_albums.append(request.url.params["highlight_id"])
        return base.handler(request)
    async def run():
        async with IGWatcherScraper(config(max_pages_per_collection=2), transport=httpx.MockTransport(respond)) as scraper:
            first = await scraper.scrape("https://instagram.com/alice/")
            cursor = first.collections["highlights"].cursor
            assert cursor
            second = await scraper.scrape("https://instagram.com/alice/", cursors={"highlights": cursor})
            third = await scraper.scrape("https://instagram.com/alice/", cursors={})
            return first, second, third
    first, second, third = asyncio.run(run())
    assert requested_albums == ["highlight:400", "highlight:401", "highlight:402", "highlight:400", "highlight:401"]
    assert second.collections["highlights"].cursor is None
    assert all(not result.collections["highlights"].complete for result in (first, second, third))


def test_bad_child_does_not_erase_valid_siblings_or_reassign_their_positions():
    parent = carousel()
    parent["children"].insert(1, {"is_video": False, "image_url": "http://127.0.0.1/private"})
    parent["carousel_count"] = 3
    result = scrape_with(transport_for(posts=[parent]))
    assert [item.position for item in result.media] == [0, 2]
    assert result.collections["posts"].error
    assert (result.groups[0].declared_count, result.groups[0].received_count) == (3, 3)


def test_source_block_during_collection_keeps_prior_observations_and_stops_all_remaining():
    requests = []
    base = transport_for(posts=[photo()])
    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/api/reels":
            return httpx.Response(200, json={"verification_required": True, "message": "secret turnstile token"})
        return base.handler(request)
    result = scrape_with(httpx.MockTransport(respond))
    assert len(result.media) == 1
    assert result.collections["reels"].state == TerminalState.BLOCKED
    assert result.collections["highlights"].state == TerminalState.BLOCKED
    assert "/wp-json/igw/v1/highlights" not in requests
    assert "secret" not in result.collections["reels"].error


def test_download_uses_plain_bounded_http_and_does_not_reuse_provider_cookies():
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"fixture image bytes"
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.host == "igwatcher.com":
            return httpx.Response(200, json=profile(), headers={"Set-Cookie": "secret_session=never-reuse; Domain=.cdninstagram.com"})
        return httpx.Response(200, content=image_bytes, headers={"Content-Type": "image/png"})
    async def run():
        async with IGWatcherScraper(config(), transport=httpx.MockTransport(respond)) as scraper:
            await scraper.scrape_profile_only("https://instagram.com/alice/")
            return await scraper.download("https://scontent.cdninstagram.com/a.png", "https://untrusted.example/")
    assert asyncio.run(run()) == (image_bytes, "image/png")
    assert len(requests) == 2
    assert "cookie" not in requests[-1].headers
    assert requests[-1].headers["referer"] == "https://igwatcher.com/"


@pytest.mark.parametrize("url", [
    "http://scontent.cdninstagram.com/a.jpg", "https://127.0.0.1/private", "https://localhost/a",
    "https://cdninstagram.com.evil.test/a", "https://cdninstagram.com@evil.test/a",
    "https://user:password@cdninstagram.com/a", "https://cdninstagram.com:444/a",
    "https://cdninstagram.com\\@127.0.0.1/a", "https://igwatcher.com/arbitrary-proxy?url=http://localhost/",
])
def test_untrusted_download_urls_never_make_network_requests(url):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=b"unexpected")
    async def run():
        async with IGWatcherScraper(config(), transport=httpx.MockTransport(respond)) as scraper:
            return await scraper.download(url, "https://igwatcher.com/")
    with pytest.raises(ScrapeFailure):
        asyncio.run(run())
    assert not requests


@pytest.mark.parametrize("status,headers,body", [
    (302, {"Location": "http://127.0.0.1/secret"}, b""),
    (200, {"Content-Type": "text/html"}, b"<html>secret payload</html>"),
    (200, {"Content-Type": "image/png"}, b"<html>secret payload</html>"),
    (200, {"Content-Type": "application/json"}, b'{"error":"secret"}'),
])
def test_download_refuses_redirects_and_non_media_payloads_without_leaking_details(status, headers, body):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers=headers, content=body)
    async def run():
        async with IGWatcherScraper(config(), transport=httpx.MockTransport(respond)) as scraper:
            return await scraper.download("https://scontent.cdninstagram.com/a?secret=token", "https://igwatcher.com/")
    with pytest.raises(ScrapeFailure) as error:
        asyncio.run(run())
    assert "secret" not in str(error.value)
    assert "token" not in str(error.value)
    assert len(requests) == 1


def test_source_cooldown_guard_prevents_network_access():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=profile())
    async def run():
        async with IGWatcherScraper(config(), transport=httpx.MockTransport(respond)) as scraper:
            scraper.source_guard = lambda: True
            return await scraper.scrape_profile_only("https://instagram.com/alice/")
    with pytest.raises(ScrapeFailure) as error:
        asyncio.run(run())
    assert error.value.blocker
    assert not requests


@pytest.mark.parametrize("change", [
    {"media_count": None}, {"following_count": True}, {"follower_count": "1.2K"},
    {"is_private": None}, {"id": 123}, {"pk": "999"}, {"username": "other"},
    {"full_name": None}, {"biography": None}, {"profile_pic_url": "http://localhost/avatar"},
])
def test_profile_missing_or_contradictory_fields_never_create_zero_snapshot(change):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=profile(**change))
    with pytest.raises(ScrapeFailure):
        scrape_with(httpx.MockTransport(respond))
    assert len(requests) == 1


def test_changed_declared_carousel_count_keeps_a_distinct_incomplete_observation():
    first = scrape_with(transport_for(posts=[carousel()]))
    changed = carousel()
    changed["carousel_count"] = 3
    second = scrape_with(transport_for(posts=[changed]))
    assert first.groups[0].revision_id != second.groups[0].revision_id
    assert second.groups[0].declared_count == 3
    assert second.groups[0].received_count == 2
    assert second.collections["posts"].error


def test_feed_cursor_loop_retains_first_page_and_stops_at_page_budget():
    paths = []
    base = transport_for()
    def respond(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/posts"):
            return httpx.Response(200, json=envelope([photo()], has_more=True, nextMaxId="same"))
        return base.handler(request)
    result = scrape_with(httpx.MockTransport(respond), initial_posts=3, max_pages_per_collection=4)
    assert len(result.media) == 1
    assert result.collections["posts"].error
    assert paths.count("/wp-json/igw/v1/posts") == 2


def test_source_cookies_are_never_reused_between_collection_requests():
    requests = []
    base = transport_for()
    def respond(request):
        requests.append(request)
        response = base.handler(request)
        response.headers["Set-Cookie"] = "secret_session=no; Path=/"
        return response
    scrape_with(httpx.MockTransport(respond))
    assert len(requests) == 5
    assert all("cookie" not in request.headers for request in requests)


@pytest.mark.parametrize("body", [
    b'{"status":"error","status":"success","code":200,"data":[]}',
    b'{"status":"success","code":200,"data":[],"unknown":NaN}',
    b'[]',
])
def test_ambiguous_json_cannot_be_interpreted_as_empty_collection(body):
    base = transport_for()
    def respond(request):
        if request.url.path.endswith("/stories"):
            return httpx.Response(200, content=body)
        return base.handler(request)
    result = scrape_with(httpx.MockTransport(respond))
    assert result.collections["stories"].state == TerminalState.PARTIAL
    assert result.collections["stories"].error


@pytest.mark.parametrize("invalid", [
    {"id": 100}, {"taken_at_timestamp": None}, {"taken_at_timestamp": 1},
    {"taken_at_timestamp": time.time() + 100000}, {"media_type": True},
    {"is_video": "false"}, {"image_url": None},
])
def test_invalid_post_does_not_drop_its_valid_neighbour(invalid):
    result = scrape_with(transport_for(posts=[photo(**invalid), photo("222_123")]))
    assert [item.source_media_id for item in result.media] == ["222_123"]
    assert result.collections["posts"].error


def test_expired_story_is_not_saved_as_current_but_is_allowed_in_highlight():
    now = int(time.time())
    item = {"id": "333_123", "taken_at": now - 100000, "expiring_at": now - 10000,
            "image_url": "https://scontent.cdninstagram.com/expired.jpg"}
    result = scrape_with(transport_for(stories=[item], albums=[{"id": "highlight:400", "media_count": 1}], album_items=[item]))
    assert [item.category for item in result.media] == ["highlights"]
    assert result.collections["stories"].error


def test_json_body_above_two_mebibytes_is_rejected():
    oversized = b'{"padding":"' + b"x" * (2 * 1024 * 1024) + b'"}'
    with pytest.raises(ScrapeFailure, match="容量上限"):
        scrape_with(httpx.MockTransport(lambda request: httpx.Response(200, content=oversized)))


def test_media_limit_aborts_stream_before_consuming_the_whole_object():
    class LargeImage(httpx.AsyncByteStream):
        consumed = 0
        async def __aiter__(self):
            yield b"\x89PNG\r\n\x1a\n"
            for _ in range(102):
                self.consumed += 1
                yield b"x" * 1024 * 1024
    stream = LargeImage()
    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream, headers={"Content-Type": "image/png"}))
        async with IGWatcherScraper(config(), transport=transport) as scraper:
            return await scraper.download("https://scontent.cdninstagram.com/large.png", "https://igwatcher.com/")
    with pytest.raises(ScrapeFailure, match="容量上限"):
        asyncio.run(run())
    assert stream.consumed <= 101


def test_download_json_verification_blocks_later_requests_in_the_same_session():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"verification_required": True, "error_type": "turnstile_required"})
    async def run():
        async with IGWatcherScraper(config(), transport=httpx.MockTransport(respond)) as scraper:
            for _ in range(2):
                with pytest.raises(ScrapeFailure) as error:
                    await scraper.download("https://scontent.cdninstagram.com/a.png", "https://igwatcher.com/")
                assert error.value.blocker
    asyncio.run(run())
    assert len(requests) == 1


def test_source_timeout_does_not_retry_or_expose_signed_url():
    requests = []
    def respond(request):
        requests.append(request)
        raise httpx.ReadTimeout("https://scontent.cdninstagram.com/?secret=token", request=request)
    async def run():
        async with IGWatcherScraper(config(), transport=httpx.MockTransport(respond)) as scraper:
            return await scraper.download("https://scontent.cdninstagram.com/a.png", "https://igwatcher.com/")
    with pytest.raises(ScrapeFailure) as error:
        asyncio.run(run())
    assert len(requests) == 1
    assert "secret" not in str(error.value)


def test_http_200_rate_limit_stops_other_categories():
    requests = []
    base = transport_for()
    def respond(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/stories"):
            return httpx.Response(200, json={"error": "rate_limit", "message": "too many requests"})
        return base.handler(request)
    result = scrape_with(httpx.MockTransport(respond))
    assert result.collections["stories"].state == TerminalState.BLOCKED
    assert len(requests) == 2


def test_challenge_body_on_http_503_stops_other_categories():
    requests = []
    base = transport_for()
    def respond(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/stories"):
            return httpx.Response(503, text='<html><script src="https://challenges.cloudflare.com/turnstile/api.js"></script></html>')
        return base.handler(request)
    result = scrape_with(httpx.MockTransport(respond))
    assert result.collections["stories"].state == TerminalState.BLOCKED
    assert len(requests) == 2


def test_matching_counts_later_remain_unverified_and_have_a_new_album_observation():
    item = {"id": "500_123", "taken_at": int(time.time()) - 100000,
            "image_url": "https://scontent.cdninstagram.com/highlight.jpg"}
    first = scrape_with(transport_for(albums=[{"id": "highlight:400", "media_count": 2}], album_items=[item]))
    later = scrape_with(transport_for(albums=[{"id": "highlight:400", "media_count": 1}], album_items=[item]))
    assert first.groups[0].revision_id != later.groups[0].revision_id
    assert not first.groups[0].complete and not later.groups[0].complete
    assert not later.collections["highlights"].complete


def test_feed_page_failure_preserves_already_parsed_posts():
    base = transport_for()
    def respond(request):
        if request.url.path.endswith("/posts"):
            if request.url.params.get("maxId"):
                raise httpx.ReadTimeout("secret")
            return httpx.Response(200, json=envelope([photo()], has_more=True, nextMaxId="page-two"))
        return base.handler(request)
    result = scrape_with(httpx.MockTransport(respond), initial_posts=3)
    assert len(result.media) == 1
    assert result.collections["posts"].error
    assert "secret" not in result.collections["posts"].error


def test_configured_page_limit_bounds_requests_even_with_fresh_distinct_cursors():
    pages = []
    base = transport_for()
    def respond(request):
        if request.url.path.endswith("/posts"):
            pages.append(request)
            return httpx.Response(200, json=envelope([photo(f"{100 + len(pages)}_123")], has_more=True,
                nextMaxId=f"page-{len(pages)}"))
        return base.handler(request)
    result = scrape_with(httpx.MockTransport(respond), initial_posts=12, max_pages_per_collection=2)
    assert len(pages) == len(result.media) == 2
    assert not result.collections["posts"].complete


def test_observation_time_keeps_subsecond_order_for_rapid_revisions(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 9, 10, 0, 0, 123456, tzinfo=UTC)
    monkeypatch.setattr("ig_monitor.igwatcher.datetime", Clock)
    async def run():
        async with IGWatcherScraper(config(), transport=transport_for()) as scraper:
            return await scraper.scrape_profile_only("https://instagram.com/alice/")
    assert asyncio.run(run()).observed_at == "2026-09-09T10:00:00.123456+00:00"
