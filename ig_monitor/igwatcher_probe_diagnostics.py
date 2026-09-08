"""Passive, bounded shape observations; never a source validation decision."""
from __future__ import annotations

import re


_MEDIA_ID = re.compile(r"([1-9][0-9]{0,24})(?:_([1-9][0-9]{0,24}))?", re.ASCII)
_OWNER_ID = re.compile(r"[1-9][0-9]{0,24}", re.ASCII)
_TARGET_ID = "528817151"
_ID_STATES = ("missing", "null", "numeric", "invalid_string", "invalid_type", "valid_string")
_OWNER_COUNTS = (
    "present", "null", "numeric", "invalid", "valid_string",
    "target_match", "target_mismatch", "suffix_match", "suffix_mismatch", "suffix_unknown",
)
_COLLABORATION_FIELDS = ("coauthor_producers", "invited_coauthor_producers", "collaborators")
_MISSING = object()


def _owner_field(fields: dict, path: str, value, suffix: str | None) -> None:
    counts = fields.setdefault(path, dict.fromkeys(_OWNER_COUNTS, 0))
    counts["present"] += 1
    if value is None:
        counts["null"] += 1
    elif type(value) in (int, float):
        counts["numeric"] += 1
    elif isinstance(value, str) and _OWNER_ID.fullmatch(value):
        counts["valid_string"] += 1
        counts["target_match" if value == _TARGET_ID else "target_mismatch"] += 1
        comparison = "suffix_unknown" if suffix is None else "suffix_match" if value == suffix else "suffix_mismatch"
        counts[comparison] += 1
    else:
        counts["invalid"] += 1


def _ownership(items: list) -> dict:
    result = {"items_checked": 0, "fields": {}, "containers": {}, "collaboration_fields": {}}
    for item in items:
        if not isinstance(item, dict):
            continue
        result["items_checked"] += 1
        media_id = item.get("id")
        match = _MEDIA_ID.fullmatch(media_id) if isinstance(media_id, str) else None
        suffix = match[2] if match else None
        for name in ("owner", "user"):
            if name not in item:
                continue
            value = item[name]
            shape = "object" if isinstance(value, dict) else "null" if value is None else "other"
            counts = result["containers"].setdefault(name, dict.fromkeys(("object", "null", "other"), 0))
            counts[shape] += 1
            if isinstance(value, dict):
                for key in ("id", "pk"):
                    if key in value:
                        _owner_field(result["fields"], name + "." + key, value[key], suffix)
        for name in ("owner_id", "user_id"):
            if name in item:
                _owner_field(result["fields"], name, item[name], suffix)
        for name in _COLLABORATION_FIELDS:
            if name not in item:
                continue
            value = item[name]
            shape = ("array" if isinstance(value, list) else "object" if isinstance(value, dict)
                     else "null" if value is None else "other")
            counts = result["collaboration_fields"].setdefault(
                name, dict.fromkeys(("array", "object", "null", "other"), 0))
            counts[shape] += 1
    return result


def _id_state(item: dict) -> str:
    if "id" not in item:
        return "missing"
    value = item["id"]
    if value is None:
        return "null"
    if type(value) in (int, float):
        return "numeric"
    if isinstance(value, str):
        return "valid_string" if _MEDIA_ID.fullmatch(value) else "invalid_string"
    return "invalid_type"


def collection_diagnostics(items: list, endpoint: str) -> dict:
    inspected = items[:100]
    result = {
        "collection_error": False, "truncated": len(items) > 100,
        "ownership": _ownership(inspected),
    }
    if endpoint not in ("posts", "reels"):
        return result
    carousel = {
        "parents": 0, "id_affected_parents": 0, "children_checked": 0,
        "non_object_children": 0, "uninspected_parents": 0,
        "id_states": dict.fromkeys(_ID_STATES, 0), "duplicate_ids": 0,
    }
    child_objects = []
    for item in inspected:
        if not isinstance(item, dict) or not (item.get("is_carousel") is True or item.get("media_type") == 8):
            continue
        carousel["parents"] += 1
        children = item.get("children", [])
        if not isinstance(children, list) or len(children) > 20:
            carousel["uninspected_parents"] += 1
            result["truncated"] = True
            continue
        affected = False
        seen = set()
        for child in children:
            carousel["children_checked"] += 1
            if not isinstance(child, dict):
                carousel["non_object_children"] += 1
                affected = True
                continue
            child_objects.append(child)
            state = _id_state(child)
            carousel["id_states"][state] += 1
            if state != "valid_string":
                affected = True
                continue
            canonical = _MEDIA_ID.fullmatch(child["id"])[1]
            if canonical in seen:
                carousel["duplicate_ids"] += 1
                affected = True
            seen.add(canonical)
        carousel["id_affected_parents"] += int(affected)
    carousel["ownership"] = _ownership(child_objects)
    result["carousel"] = carousel
    return result


def _count_observation(value) -> tuple[str, int | None]:
    if value is _MISSING:
        return "missing", None
    if value is None:
        return "null", None
    if type(value) is not int:
        return "invalid_type", None
    if not 0 <= value <= 1_000_000:
        return "out_of_range", None
    return "integer", value


def album_comparison(album: dict, received_count=_MISSING) -> dict:
    declared_state, declared = _count_observation(album.get("media_count", _MISSING))
    received_state, received = _count_observation(received_count)
    relation = "unknown"
    if declared is not None and received is not None:
        relation = "equal" if declared == received else "declared_more" if declared > received else "declared_less"
    return {
        "declared_state": declared_state, "declared_count": declared,
        "received_state": received_state, "received_count": received,
        "relation": relation,
    }
