from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ig_monitor.anonyig import (
    AnonyIGScraper, REELS_LIMITATION, SourceBlocked, SourceContractError,
    parse_post, parse_posts_page, parse_profile, parse_story, username_from_url,
)
from ig_monitor.config import BrowserConfig
from ig_monitor.models import CollectionObservation, TerminalState


FIXTURES = Path(__file__).parent / "fixtures" / "anonyig"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["payload"]


def profile():
    return parse_profile(fixture("01_initial_userInfo.json"), "fixture_account")


def posts():
    return parse_posts_page(fixture("02_initial_postsV2.json"))[0]


def scraper(pages=1, posts_limit=12):
    return AnonyIGScraper(BrowserConfig(True, 45, 0, Path("unused"), initial_posts=posts_limit,
                                      max_pages_per_collection=pages))


def test_profile_is_structured_and_cross_checks_requested_username():
    snapshot, profile_id = profile()
    assert snapshot.username == "fixture_account"
    assert profile_id == "100000"
    assert snapshot.posts == 4912
    assert snapshot.followers == 104402496
    with pytest.raises(SourceContractError, match="不同 username"):
        parse_profile(fixture("01_initial_userInfo.json"), "unrelated_account")


@pytest.mark.parametrize("change", [
    {"media_count": None}, {"following_count": True}, {"follower_count": "104.4M"},
    {"is_private": None}, {"id": "different-id"}, {"profile_pic_url_downloadable": "http://localhost/avatar"},
])
def test_profile_rejects_incomplete_or_contradictory_values(change):
    payload = fixture("01_initial_userInfo.json")
    payload["result"][0]["user"].update(change)
    if "id" in change:
        payload["result"][0]["user"]["pk"] = "original-id"
    with pytest.raises(SourceContractError):
        parse_profile(payload, "fixture_account")


def test_carousel_keeps_parent_identity_order_and_all_children():
    snapshot, profile_id = profile()
    node = next(node for node in posts() if node["__typename"] == "GraphSidecar")
    result = parse_post(node, snapshot.username, profile_id)
    assert len(result) == len(node["edge_sidecar_to_children"]["edges"]) == 2
    assert [item.position for item in result] == [0, 1]
    assert {item.parent_id for item in result} == {node["id"]}
    assert [item.source_media_id for item in result] == [edge["node"]["id"] for edge in node["edge_sidecar_to_children"]["edges"]]
    assert all(item.owner_username == snapshot.username for item in result)
    # Posts expose owner username, not an independently verified owner ID.
    assert all(item.owner_id is None for item in result)


def test_refuses_unproved_collaborator_in_profile_feed():
    node = next(node for node in posts() if node["owner"]["username"] == "other_owner")
    with pytest.raises(SourceContractError, match="共同作者"):
        parse_post(node, "fixture_account", "100000")


def test_invalid_carousel_child_rejects_whole_post_without_shortening_it():
    node = next(node for node in posts() if node["__typename"] == "GraphSidecar")
    del node["edge_sidecar_to_children"]["edges"][1]["node"]["id"]
    with pytest.raises(SourceContractError, match="子媒體 ID"):
        parse_post(node, "fixture_account", "100000")


def test_shared_posts_reels_asset_keeps_same_canonical_key_and_both_memberships():
    node = next(node for node in posts() if node["is_video"] and node["owner"]["username"] == "fixture_account")
    post = parse_post(node, "fixture_account", "100000", "posts")[0]
    reel = parse_post(node, "fixture_account", "100000", "reels")[0]
    assert post.media_key == reel.media_key
    assert post.url == reel.url
    assert (post.category, reel.category) == ("posts", "reels")


def test_story_ownership_and_highlight_album_membership_are_separate():
    item = fixture("03_stories_stories.json")["result"][0]
    album = fixture("04_highlights_highlights.json")["result"][0]
    story = parse_story(item, "fixture_account", "100000")
    highlight = parse_story(item, "fixture_account", "100000", position=3, album=album)
    second_album = {**album, "id": "second-album", "title": "Second album"}
    second = parse_story(item, "fixture_account", "100000", position=0, album=second_album)
    assert story.media_key == highlight.media_key == second.media_key
    assert highlight.owner_id == "100000"
    assert highlight.album_id != second.album_id
    assert highlight.album_title == "Roman"
    assert highlight.position == 3
    assert story.album_id is None


def test_highlight_fixture_all_nine_items_not_only_six_visible_cards():
    album = fixture("04_highlights_highlights.json")["result"][0]
    items = fixture("05_highlight_album_highlightStories.json")["result"]
    media = [parse_story(item, "fixture_account", "100000", position=pos, album=album)
             for pos, item in enumerate(items)]
    assert len(media) == 9
    assert [item.position for item in media] == list(range(9))


def test_story_does_not_scan_sticker_or_other_arbitrary_urls():
    item = fixture("03_stories_stories.json")["result"][0]
    item["unexpected_payload"] = {"download_url": "https://media.example.test/unrelated"}
    parsed = parse_story(item, "fixture_account", "100000")
    assert parsed.url != item["unexpected_payload"]["download_url"]
    item["user"]["id"] = "unrelated-id"
    with pytest.raises(SourceContractError, match="作者"):
        parse_story(item, "fixture_account", "100000")


def test_explicit_empty_is_distinct_from_missing_or_failed_result():
    nodes, cursor = parse_posts_page({"result": {"edges": [], "page_info": {"has_next_page": False, "end_cursor": None}}})
    assert nodes == [] and cursor is None
    for payload in ({}, {"result": None}, {"result": [], "status": "error"},
                    {"result": {"edges": [], "page_info": {"has_next_page": True, "end_cursor": "next"}}}):
        with pytest.raises(SourceContractError):
            parse_posts_page(payload)


def test_real_second_page_has_distinct_opaque_cursor():
    first, first_cursor = parse_posts_page(fixture("02_initial_postsV2.json"))
    second, second_cursor = parse_posts_page(fixture("06_pagination_postsV2.json"))
    assert len(first) == len(second) == 12
    assert first_cursor and second_cursor and first_cursor != second_cursor
    assert {node["id"] for node in first}.isdisjoint(node["id"] for node in second)


@pytest.mark.parametrize("url", ["https://instagram.com/a/", "https://www.instagram.com/a/",
                                  "https://insta-stories-viewer.com/a/", "https://mollygram.com/a/"])
def test_supported_account_urls_do_not_drive_navigation_host(url):
    assert username_from_url(url) == "a"


@pytest.mark.parametrize("url", ["http://instagram.com/a/", "https://127.0.0.1/a/",
                                  "https://instagram.com/p/post/", "https://instagram.com/a/b/"])
def test_invalid_account_urls_are_rejected(url):
    with pytest.raises(SourceContractError):
        username_from_url(url)


def feed_session(payload=None):
    return SimpleNamespace(
        wait=AsyncMock(return_value=SimpleNamespace(payload=payload or fixture("02_initial_postsV2.json"))),
        next_posts=AsyncMock(return_value=fixture("06_pagination_postsV2.json")),
    )


def test_bounded_feed_records_ownership_gaps_and_reels_limitation():
    snapshot, profile_id = profile()
    session = feed_session()
    media, states = asyncio.run(scraper()._feed(session, snapshot, profile_id, {}, {}))
    assert media
    assert all(item.owner_username == snapshot.username for item in media)
    assert states["posts"].state == TerminalState.PARTIAL
    assert states["posts"].complete is False
    assert REELS_LIMITATION in states["reels"].error
    assert states["reels"].complete is False
    assert session.next_posts.await_count == 0


def test_resuming_partial_baseline_does_not_silently_expand_or_clear_gaps():
    snapshot, profile_id = profile()
    adapter = scraper()
    media, states = asyncio.run(adapter._feed(feed_session(), snapshot, profile_id, {}, {}))
    cursor = {category: state.cursor for category, state in states.items()}
    next_media, next_states = asyncio.run(adapter._feed(feed_session(), snapshot, profile_id, cursor, {}))
    # Re-observation refreshes expiring URLs, but does not add new identities or
    # memberships to the fixed initial scope.
    assert [(item.category, item.parent_id, item.position) for item in next_media] == [
        (item.category, item.parent_id, item.position) for item in media
    ]
    assert next_states["posts"].state == TerminalState.PARTIAL
    assert next_states["posts"].complete is False
    assert json.loads(next_states["posts"].cursor)["scope"] == json.loads(states["posts"].cursor)["scope"]


def test_incremental_scan_uses_time_order_not_first_pinned_known_item():
    snapshot, profile_id = profile()
    payload = fixture("02_initial_postsV2.json")
    nodes = payload["result"]["edges"]
    for edge in nodes:
        edge["node"]["owner"]["username"] = snapshot.username
    oldest = min(nodes, key=lambda edge: edge["node"]["taken_at_timestamp"])
    nodes.remove(oldest)
    nodes.insert(0, oldest)
    known = {oldest["node"]["id"]}
    media, states = asyncio.run(scraper()._feed(feed_session(payload), snapshot, profile_id, {}, {"posts": known}))
    assert states["posts"].complete is True
    assert len({item.parent_id for item in media if item.category == "posts"} - known) == 11


def test_known_carousel_refreshes_expired_urls_without_counting_or_reordering_again():
    snapshot, profile_id = profile()
    carousel = next(node for node in posts() if node["__typename"] == "GraphSidecar")
    payload = {"result": {"edges": [{"node": carousel}],
                          "page_info": {"has_next_page": False, "end_cursor": None}}}
    adapter = scraper(posts_limit=1)
    first_media, first_states = asyncio.run(adapter._feed(feed_session(payload), snapshot, profile_id, {}, {}))
    first_posts = [item for item in first_media if item.category == "posts"]
    assert len(first_posts) == 2 and first_states["posts"].complete
    for position, edge in enumerate(carousel["edge_sidecar_to_children"]["edges"]):
        for resource in edge["node"]["display_resources"]:
            resource["url_downloadable"] = f"https://media.example.test/refreshed-{position}"
    known = {"posts": {carousel["id"]}}
    refreshed_media, refreshed_states = asyncio.run(adapter._feed(
        feed_session(payload), snapshot, profile_id, {}, known, {"posts"},
    ))
    refreshed_posts = [item for item in refreshed_media if item.category == "posts"]
    assert [item.url for item in refreshed_posts] == ["https://media.example.test/refreshed-0",
                                                    "https://media.example.test/refreshed-1"]
    assert [(item.media_key, item.parent_id, item.position) for item in refreshed_posts] == [
        (item.media_key, item.parent_id, item.position) for item in first_posts
    ]
    again_media, _ = asyncio.run(adapter._feed(feed_session(payload), snapshot, profile_id, {}, known, {"posts"}))
    assert len([item for item in again_media if item.category == "posts"]) == 2
    assert refreshed_states["posts"].complete and refreshed_states["posts"].cursor is None


@pytest.mark.parametrize("has_next_page", [False, True])
def test_completed_empty_baseline_does_not_reapply_initial_twelve_post_limit(has_next_page):
    snapshot, profile_id = profile()
    first = fixture("02_initial_postsV2.json")
    second = fixture("06_pagination_postsV2.json")
    # Explicit test scenario based on observed shapes, not a new live fixture:
    # the formerly empty account now has 24 owned posts across two pages.
    for payload in (first, second):
        for edge in payload["result"]["edges"]:
            edge["node"]["owner"]["username"] = snapshot.username
    second["result"]["page_info"]["has_next_page"] = has_next_page
    if not has_next_page:
        second["result"]["page_info"]["end_cursor"] = None
    session = feed_session(first)
    session.next_posts.return_value = second
    media, states = asyncio.run(scraper(pages=2)._feed(
        session, snapshot, profile_id, {}, {"posts": set()}, {"posts"},
    ))
    assert len({item.parent_id for item in media if item.category == "posts"}) == 24
    assert states["posts"].complete is (not has_next_page)
    assert states["posts"].state == (TerminalState.PARTIAL if has_next_page else TerminalState.MEDIA)
    if has_next_page:
        assert json.loads(states["posts"].cursor)["mode"] == "incremental"
    else:
        assert states["posts"].cursor is None


def test_source_block_preserves_already_parsed_feed():
    snapshot, profile_id = profile()
    session = feed_session()
    session.next_posts.side_effect = SourceBlocked("challenge")
    media, states = asyncio.run(scraper(pages=2)._feed(session, snapshot, profile_id, {}, {}))
    assert media
    assert all(state.state == TerminalState.BLOCKED for state in states.values())
    assert session.next_posts.await_count == 1


def test_successful_categories_survive_another_category_error():
    snapshot, profile_id = profile()
    adapter = scraper()
    session = SimpleNamespace(check_blocked=AsyncMock(), close=AsyncMock())
    adapter._open = AsyncMock(return_value=(session, snapshot, profile_id))
    adapter._feed = AsyncMock(return_value=([], {category: CollectionObservation(TerminalState.EMPTY, complete=True)
                                                for category in ("posts", "reels")}))
    adapter._stories = AsyncMock(side_effect=SourceContractError("bad stories response"))
    album = fixture("04_highlights_highlights.json")["result"][0]
    item = fixture("05_highlight_album_highlightStories.json")["result"][0]
    saved = parse_story(item, snapshot.username, profile_id, album=album)
    adapter._highlights = AsyncMock(return_value=([saved], CollectionObservation(TerminalState.MEDIA, complete=True)))
    result = asyncio.run(adapter.scrape("https://instagram.com/fixture_account/"))
    assert result.media == [saved]
    assert result.collections["stories"].state == TerminalState.FAILED
    assert result.collections["highlights"].complete is True
    assert result.profile_id == profile_id
    session.close.assert_awaited_once()


def test_profile_only_exposes_id_without_collecting_categories():
    snapshot, profile_id = profile()
    adapter = scraper()
    session = SimpleNamespace(close=AsyncMock())
    adapter._open = AsyncMock(return_value=(session, snapshot, profile_id))
    adapter._feed = AsyncMock()
    result = asyncio.run(adapter.scrape_profile_only("https://instagram.com/fixture_account/"))
    assert result == snapshot
    assert adapter.last_profile_id == profile_id
    adapter._feed.assert_not_awaited()


def test_highlights_resume_after_budget_without_repeating_completed_albums():
    snapshot, profile_id = profile()
    albums = fixture("04_highlights_highlights.json")
    stories = fixture("05_highlight_album_highlightStories.json")
    clicked = []

    class Locator:
        def __init__(self, title):
            self.title = title

        def nth(self, index):
            return self

        async def count(self):
            return 1

        async def click(self):
            clicked.append(self.title)

    session = SimpleNamespace(
        tab=AsyncMock(return_value=albums), check_blocked=AsyncMock(), responses=[],
        wait=AsyncMock(return_value=SimpleNamespace(payload=stories)),
        page=SimpleNamespace(get_by_text=lambda title, exact: Locator(title)),
    )
    adapter = scraper(pages=4)
    first_media, first = asyncio.run(adapter._highlights(session, snapshot, profile_id, None))
    assert first.state == TerminalState.PARTIAL
    assert first.error is None
    assert first.complete is False
    assert len(json.loads(first.cursor)["completed_albums"]) == 4
    assert clicked == [album["title"] for album in albums["result"][:4]]
    second_media, second = asyncio.run(adapter._highlights(session, snapshot, profile_id, first.cursor))
    assert clicked == [album["title"] for album in albums["result"]]
    assert second.complete is True
    assert second.cursor is None
    assert len(first_media) == 36 and len(second_media) == 9
    asyncio.run(adapter._highlights(session, snapshot, profile_id, None))
    assert len(clicked) == 9  # A new completed cycle checks the first four albums again.


def test_source_guard_stops_before_ui_or_network_actions():
    from ig_monitor.anonyig import _Session

    page = SimpleNamespace(on=lambda *args: None)
    session = _Session(page, 1, lambda: True)
    with pytest.raises(SourceBlocked, match="全域冷卻"):
        asyncio.run(session.check_blocked())


def test_fixtures_have_no_live_urls_viewer_or_credentials():
    for path in FIXTURES.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "viewer" not in text
        assert "token" not in text.lower()
        assert "signature" not in text.lower()
        assert "anonyig.com" not in text
        assert "nasa" not in text.lower()
