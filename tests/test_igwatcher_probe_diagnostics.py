"""Offline acceptance tests for passive, sanitized IGWatcher diagnostics.

The agreed seams are collection_diagnostics, album_comparison and run_probe.
All HTTP responses below are synthetic and supplied through MockTransport.
"""
from __future__ import annotations

from copy import deepcopy
import json
import platform

import pytest

from ig_monitor import igwatcher_probe_diagnostics as diagnostics_module
from ig_monitor.igwatcher_probe_diagnostics import collection_diagnostics
from test_igwatcher_probe import Scenario, assert_safe_report


BASELINE_URLS = [
    "https://igwatcher.com/wp-json/igw/v1/search?username=nasa",
    "https://igwatcher.com/wp-json/igw/v1/stories?username=nasa",
    "https://igwatcher.com/wp-json/igw/v1/posts?username=nasa&limit=24",
    "https://igwatcher.com/api/reels?username=nasa&limit=12",
    "https://igwatcher.com/wp-json/igw/v1/highlights?username=nasa",
    "https://igwatcher.com/api/highlight-items?highlight_id=highlight%3A3100000000000000004&user=nasa",
]


def owner_counts(**counts):
    return {
        "present": 0, "null": 0, "numeric": 0, "invalid": 0,
        "valid_string": 0, "target_match": 0, "target_mismatch": 0,
        "suffix_match": 0, "suffix_mismatch": 0, "suffix_unknown": 0,
        **counts,
    }


# Frozen from the schema-1 synthetic sample before diagnostics were integrated.
# Keep the old quality keys explicit: additions/removals are not passive changes.
OLD_ZERO_QUALITY = {
    "invalid_ids": 0, "numeric_ids": 0, "duplicate_ids": 0,
    "owner_suffix_mismatch": 0, "owner_suffix_unknown": 0,
    "timestamp_missing": 0, "timestamp_invalid": 0, "timestamp_future": 0,
    "expiry_missing": 0, "expiry_invalid": 0, "expired_stories": 0,
    "media_url_missing": 0, "media_url_invalid": 0, "reel_type_unconfirmed": 0,
    "media_type_unconfirmed": 0, "carousel_invalid": 0, "child_ids_invalid": 0,
    "child_owner_mismatch": 0, "child_owner_unknown": 0, "album_count_mismatch": 0,
}


def old_sample(*, owner_mismatch=False):
    events = [{
        "endpoint": "profile", "page": 1, "http_status": 200,
        "body_state": "json", "state": "observed", "identity_match": True,
    }]
    for endpoint in ("stories", "posts", "reels", "highlights", "highlight_items"):
        event = {
            "endpoint": endpoint, "page": 1, "http_status": 200,
            "body_state": "json", "state": "observed", "count": 1,
            "quality": dict(OLD_ZERO_QUALITY),
        }
        if endpoint in ("posts", "reels"):
            event["pagination"] = "exhausted"
        if endpoint == "reels" and owner_mismatch:
            event["quality"]["owner_suffix_mismatch"] = 1
        events.append(event)
    return {
        "source": "igwatcher", "target": "nasa", "platform": platform.system().lower(),
        "architecture": platform.machine().lower(), "observed_at": "2026-09-09T00:00:00+00:00",
        "request_count": 6, "events": events, "production_ready": False,
        "outcome": "sample_incomplete" if owner_mismatch else "sample_observed",
    }


def legacy_projection(report):
    projected = deepcopy(report)
    del projected["schema_version"]
    del projected["elapsed_ms"]
    for event in projected["events"]:
        event.pop("diagnostics", None)
    return projected


def test_diagnostics_are_passive_additions_to_the_previous_successful_sample():
    scenario = Scenario()
    report = scenario.run()
    assert_safe_report(report)
    assert report["schema_version"] == 2
    assert report["outcome"] == "sample_observed"
    assert report["request_count"] == 6
    assert [str(request.url) for request in scenario.requests] == BASELINE_URLS
    assert "diagnostics" not in report["events"][0]
    for event in report["events"][1:]:
        assert event["count"] == 1
        assert event["state"] == "observed"
        assert set(event["quality"].values()) == {0}
        assert isinstance(event["diagnostics"], dict)
        assert event["diagnostics"]["collection_error"] is False


def test_carousel_id_reasons_are_exclusive_but_affected_parents_count_once():
    items = [{
        "id": "3100000000000000002_528817151",
        "media_type": 8,
        "children": [
            {}, {"id": None}, {"id": 3100000000000000010},
            {"id": "RAW_INVALID_ID"}, {"id": True}, {"id": ["RAW_NESTED_ID"]},
            {"id": "3100000000000000011_528817151"},
            {"id": "3100000000000000011_528817151"},
            "RAW_NONOBJECT_CHILD",
        ],
    }]
    before = deepcopy(items)
    diagnostics = collection_diagnostics(items, "posts")
    assert diagnostics["collection_error"] is False
    assert diagnostics["truncated"] is False
    carousel = diagnostics["carousel"]
    assert carousel["parents"] == 1
    assert carousel["id_affected_parents"] == 1
    assert carousel["children_checked"] == 9
    assert carousel["non_object_children"] == 1
    assert carousel["uninspected_parents"] == 0
    assert carousel["id_states"] == {
        "missing": 1, "null": 1, "numeric": 1,
        "invalid_string": 1, "invalid_type": 2, "valid_string": 2,
    }
    assert carousel["duplicate_ids"] == 1
    assert carousel["ownership"]["items_checked"] == 8
    assert items == before


def test_explicit_owner_fields_and_collaboration_shapes_are_only_sanitized_observations():
    items = [
        {
            "id": "3100000000000000002_528817151",
            "owner": {"id": "528817151", "pk": "528817151", "username": "RAW_OWNER_NAME"},
            "user": {"id": "999", "pk": True},
            "owner_id": "528817151", "user_id": "999",
            "coauthor_producers": [{"id": "RAW_COLLABORATOR_ID"}],
            "RAW_DYNAMIC_FIELD": "https://RAW_PRIVATE_HOST/RAW_TOKEN",
        },
        {
            "id": "3100000000000000003_999",
            "owner": {"id": "528817151", "pk": None}, "user": None,
            "owner_id": 528817151, "user_id": None,
            "collaborators": {"RAW_PERSONAL_KEY": "RAW_PERSONAL_VALUE"},
        },
        {
            "id": "3100000000000000004", "owner": "RAW_OWNER_OBJECT",
            "user": {"id": "RAW_BAD_USER_ID", "pk": []}, "owner_id": False,
            "invited_coauthor_producers": None,
        },
    ]
    before = deepcopy(items)
    diagnostics = collection_diagnostics(items, "stories")
    assert "carousel" not in diagnostics
    ownership = diagnostics["ownership"]
    assert ownership["items_checked"] == 3
    assert ownership["fields"] == {
        "owner.id": owner_counts(present=2, valid_string=2, target_match=2, suffix_match=1, suffix_mismatch=1),
        "owner.pk": owner_counts(present=2, null=1, valid_string=1, target_match=1, suffix_match=1),
        "user.id": owner_counts(present=2, invalid=1, valid_string=1, target_mismatch=1, suffix_mismatch=1),
        "user.pk": owner_counts(present=2, invalid=2),
        "owner_id": owner_counts(present=3, numeric=1, invalid=1, valid_string=1, target_match=1, suffix_match=1),
        "user_id": owner_counts(present=2, null=1, valid_string=1, target_mismatch=1, suffix_mismatch=1),
    }
    assert ownership["containers"] == {
        "owner": {"object": 2, "null": 0, "other": 1},
        "user": {"object": 2, "null": 1, "other": 0},
    }
    assert ownership["collaboration_fields"] == {
        "coauthor_producers": {"array": 1, "object": 0, "null": 0, "other": 0},
        "collaborators": {"array": 0, "object": 1, "null": 0, "other": 0},
        "invited_coauthor_producers": {"array": 0, "object": 0, "null": 1, "other": 0},
    }
    encoded = json.dumps(diagnostics, allow_nan=False)
    assert not any(marker in encoded for marker in ("RAW_", "528817151", "310000000", "999", "https://"))
    assert items == before


@pytest.mark.parametrize(("album", "received", "declared_state", "declared_count", "received_state", "received_count", "relation"), [
    ({}, 2, "missing", None, "integer", 2, "unknown"),
    ({"media_count": None}, 2, "null", None, "integer", 2, "unknown"),
    ({"media_count": "RAW_COUNT"}, 2, "invalid_type", None, "integer", 2, "unknown"),
    ({"media_count": True}, 2, "invalid_type", None, "integer", 2, "unknown"),
    ({"media_count": -1}, 2, "out_of_range", None, "integer", 2, "unknown"),
    ({"media_count": 1_000_001}, 2, "out_of_range", None, "integer", 2, "unknown"),
    ({"media_count": 1_000_000}, 2, "integer", 1_000_000, "integer", 2, "declared_more"),
    ({"media_count": 1}, 2, "integer", 1, "integer", 2, "declared_less"),
    ({"media_count": 0}, 0, "integer", 0, "integer", 0, "equal"),
    ({"media_count": 2}, None, "integer", 2, "null", None, "unknown"),
    ({"media_count": 2}, False, "integer", 2, "invalid_type", None, "unknown"),
    ({"media_count": 2}, -1, "integer", 2, "out_of_range", None, "unknown"),
    ({"media_count": 2}, 1_000_001, "integer", 2, "out_of_range", None, "unknown"),
])
def test_album_counts_distinguish_absence_invalid_values_and_real_zero(
    album, received, declared_state, declared_count, received_state, received_count, relation,
):
    album = {**album, "id": "RAW_ALBUM_ID", "title": "RAW_ALBUM_TITLE"}
    before = deepcopy(album)
    assert diagnostics_module.album_comparison(album, received) == {
        "declared_state": declared_state, "declared_count": declared_count,
        "received_state": received_state, "received_count": received_count,
        "relation": relation,
    }
    assert album == before


def test_missing_album_declaration_is_diagnosed_without_relaxing_old_quality_checks():
    scenario = Scenario()
    del scenario.payloads["highlights"]["data"][0]["media_count"]
    report = scenario.run()
    assert_safe_report(report)
    assert report["outcome"] == "sample_incomplete"
    assert [str(request.url) for request in scenario.requests] == BASELINE_URLS
    albums = next(event for event in report["events"] if event["endpoint"] == "highlights")
    items = next(event for event in report["events"] if event["endpoint"] == "highlight_items")
    assert albums["quality"]["album_count_mismatch"] == 1
    assert items["quality"]["album_count_mismatch"] == 1
    assert items["diagnostics"]["album_comparison"] == {
        "declared_state": "missing", "declared_count": None,
        "received_state": "integer", "received_count": 1, "relation": "unknown",
    }


def test_omitted_received_count_is_not_reported_as_null_or_zero():
    assert diagnostics_module.album_comparison({"media_count": 0}) == {
        "declared_state": "integer", "declared_count": 0,
        "received_state": "missing", "received_count": None, "relation": "unknown",
    }


@pytest.mark.parametrize(("value", "expected"), [
    ("528817151", owner_counts(present=1, valid_string=1, target_match=1, suffix_match=1)),
    ("999", owner_counts(present=1, valid_string=1, target_mismatch=1, suffix_mismatch=1)),
    (None, owner_counts(present=1, null=1)),
    (0, owner_counts(present=1, numeric=1)),
    (float("nan"), owner_counts(present=1, numeric=1)),
    (True, owner_counts(present=1, invalid=1)),
    ("0", owner_counts(present=1, invalid=1)),
    ("0528817151", owner_counts(present=1, invalid=1)),
    ("1" * 26, owner_counts(present=1, invalid=1)),
    ("RAW_UNKNOWN_OWNER", owner_counts(present=1, invalid=1)),
])
def test_owner_comparisons_only_use_strict_string_ids(value, expected):
    diagnostics = collection_diagnostics([{
        "id": "3100000000000000002_528817151", "owner_id": value,
    }], "stories")
    assert diagnostics["ownership"]["fields"] == {"owner_id": expected}
    assert "RAW_" not in json.dumps(diagnostics, allow_nan=False)


@pytest.mark.parametrize("identifier", ["3100000000000000002", 3100000000000000002, "0_528817151", "RAW_MEDIA_ID"])
def test_explicit_owner_does_not_invent_a_missing_or_invalid_media_suffix(identifier):
    diagnostics = collection_diagnostics([{"id": identifier, "owner_id": "528817151"}], "stories")
    assert diagnostics["ownership"]["fields"]["owner_id"] == owner_counts(
        present=1, valid_string=1, target_match=1, suffix_unknown=1,
    )


def test_unknown_fields_and_collaborator_contents_are_never_inspected():
    class Sensitive:
        def __str__(self):
            raise AssertionError("Diagnostic must not stringify source content")

        def __repr__(self):
            raise AssertionError("Diagnostic must not represent source content")

        def __iter__(self):
            raise AssertionError("Diagnostic must not traverse collaborator content")

    diagnostics = collection_diagnostics([{
        "id": "3100000000000000002_528817151", "owner_id": Sensitive(),
        "RAW_SOURCE_KEY": Sensitive(),
        "owner": {"username": Sensitive(), "RAW_PRIVATE_OWNER_KEY": Sensitive()},
        "coauthor_producers": [Sensitive()], "collaborators": {"RAW_PRIVATE_KEY": Sensitive()},
    }], "stories")
    assert diagnostics["ownership"]["fields"] == {"owner_id": owner_counts(present=1, invalid=1)}
    assert diagnostics["ownership"]["collaboration_fields"] == {
        "coauthor_producers": {"array": 1, "object": 0, "null": 0, "other": 0},
        "collaborators": {"array": 0, "object": 1, "null": 0, "other": 0},
    }
    assert "RAW_" not in json.dumps(diagnostics, allow_nan=False)


def test_standalone_collection_diagnostics_never_inspect_item_101():
    class Uninspected(dict):
        def get(self, *args, **kwargs):
            raise AssertionError("Item beyond the cap must not be inspected")

    diagnostics = collection_diagnostics([{} for _ in range(100)] + [Uninspected()], "posts")
    assert diagnostics["truncated"] is True
    assert diagnostics["ownership"]["items_checked"] == 100
    assert diagnostics["carousel"]["parents"] == 0


@pytest.mark.parametrize("uninspected_children", [None, "RAW_BAD_CHILDREN", [{}] * 21])
def test_uninspectable_carousels_are_reported_without_partial_child_inspection(uninspected_children):
    valid_children = [{"id": str(3100000000000000010 + index)} for index in range(20)]
    diagnostics = collection_diagnostics([
        {"media_type": 8, "children": valid_children},
        {"media_type": 8, "children": uninspected_children},
    ], "posts")
    assert diagnostics["truncated"] is True
    assert diagnostics["carousel"]["parents"] == 2
    assert diagnostics["carousel"]["children_checked"] == 20
    assert diagnostics["carousel"]["uninspected_parents"] == 1
    assert diagnostics["carousel"]["id_affected_parents"] == 0


def test_duplicate_child_ids_are_per_parent_and_separate_from_owner_mismatches():
    diagnostics = collection_diagnostics([
        {"media_type": 8, "children": [
            {"id": "3100000000000000010_528817151", "owner_id": "999"},
            {"id": "3100000000000000010_999"},
        ]},
        {"is_carousel": True, "children": [
            {"id": "3100000000000000010_528817151", "owner_id": "999"},
        ]},
    ], "reels")
    carousel = diagnostics["carousel"]
    assert carousel["parents"] == 2
    assert carousel["id_states"]["valid_string"] == 3
    assert carousel["duplicate_ids"] == 1
    assert carousel["id_affected_parents"] == 1
    assert carousel["ownership"]["fields"]["owner_id"] == owner_counts(
        present=2, valid_string=2, target_mismatch=2, suffix_mismatch=2,
    )


@pytest.mark.parametrize("owner_mismatch", [False, True])
def test_added_owner_and_collaboration_observations_preserve_every_legacy_field(owner_mismatch):
    scenario = Scenario()
    post = scenario.payloads["posts"]["data"][0]
    post.update(owner_id="999", coauthor_producers=[{"id": "RAW_COLLABORATOR"}])
    if owner_mismatch:
        reel = scenario.payloads["reels"]["data"][0]
        reel.update(id="3100000000000000003_999", owner_id="528817151")
    before = deepcopy(scenario.payloads)
    report = scenario.run()
    assert_safe_report(report)
    assert legacy_projection(report) == old_sample(owner_mismatch=owner_mismatch)
    assert [str(request.url) for request in scenario.requests] == BASELINE_URLS
    post_event = next(event for event in report["events"] if event["endpoint"] == "posts")
    assert post_event["diagnostics"]["ownership"]["fields"]["owner_id"]["target_mismatch"] == 1
    assert scenario.payloads == before


@pytest.mark.parametrize("helper", ["collection_diagnostics", "album_comparison"])
@pytest.mark.parametrize("owner_mismatch", [False, True])
def test_diagnostic_failure_preserves_old_outcome_quality_and_request_sequence(monkeypatch, helper, owner_mismatch):
    def failure(*args, **kwargs):
        raise RuntimeError("RAW_DIAGNOSTIC_FAILURE https://RAW_SOURCE_URL/RAW_TOKEN")

    monkeypatch.setattr(diagnostics_module, helper, failure)
    scenario = Scenario()
    if owner_mismatch:
        scenario.payloads["reels"]["data"][0]["id"] = "3100000000000000003_999"
    report = scenario.run()
    assert_safe_report(report)
    assert legacy_projection(report) == old_sample(owner_mismatch=owner_mismatch)
    assert [str(request.url) for request in scenario.requests] == BASELINE_URLS
    final_diagnostics = report["events"][-1]["diagnostics"]
    assert final_diagnostics["collection_error"] is True
    if helper == "collection_diagnostics":
        assert final_diagnostics["album_comparison"]["relation"] == "equal"
    else:
        assert final_diagnostics["ownership"]["items_checked"] == 1
        assert "album_comparison" not in final_diagnostics
