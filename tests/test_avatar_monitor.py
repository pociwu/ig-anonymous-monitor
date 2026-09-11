"""Avatar encoding changes through real save/compare/database/event boundaries."""
import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageOps

from ig_monitor.config import load_config
from ig_monitor.db import Database
from ig_monitor.models import PrivacyState, ProfileSnapshot, ScrapeResult
from ig_monitor.monitor import Monitor
from ig_monitor.utils import sha256_bytes


def picture():
    image = Image.new("RGB", (640, 640), (180, 200, 220))
    draw = ImageDraw.Draw(image)
    draw.ellipse((60, 90, 490, 580), fill=(190, 80, 40))
    draw.rectangle((120, 180, 220, 300), fill=(30, 40, 70))
    draw.polygon([(350, 100), (610, 470), (420, 560)], fill=(50, 120, 40))
    return image


def jpeg(image, size=640, quality=95):
    out = BytesIO()
    image.resize((size, size), Image.Resampling.LANCZOS).save(out, "JPEG", quality=quality)
    return out.getvalue()


def run_avatars(tmp_path, monkeypatch, payloads, *, followers=None, before_run=None):
    settings = tmp_path / "config.yaml"
    settings.write_text("accounts:\n  - url: https://instagram.com/alice/\n"
                        "browser:\n  anonymous_source: igwatcher\n"
                        "telegram:\n  enabled: false\nheartbeat:\n  enabled: false\n"
                        "apify:\n  enabled: false\n", encoding="utf-8")
    config = load_config(settings)
    db = Database(config.paths.data_dir / "state.sqlite3")
    current = {"index": 0}

    class Source:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def scrape(self, url, **kwargs):
            index = current["index"]
            snapshot = ProfileSnapshot("alice", "Alice", 10,
                followers[index] if followers else 20, 30, "bio", PrivacyState.PUBLIC,
                f"https://cdninstagram.com/avatar-{index}.jpg", observed_at=f"2026-09-12T00:0{index}:00+00:00")
            return ScrapeResult(snapshot, source="igwatcher", profile_id="123")

        async def download(self, url, referer):
            return payloads[current["index"]], "image/jpeg"

    monkeypatch.setattr("ig_monitor.monitor.ProfileScraper", Source)
    snapshots = []
    try:
        for index in range(len(payloads)):
            current["index"] = index
            if before_run:
                before_run(index, snapshots)
            assert asyncio.run(Monitor(config, db).run()) == 0
            snapshots.append(db.snapshot_from_row(db.enabled_accounts()[0]))
        events = db.pending_events(100)
        return snapshots, [e for e in events if e["kind"] == "change"]
    finally:
        db.close()


def test_smaller_recompressed_avatar_keeps_clear_file_without_change(tmp_path, monkeypatch):
    high, low = jpeg(picture()), jpeg(picture(), 150, 65)
    snapshots, events = run_avatars(tmp_path, monkeypatch, [high, low, low])
    assert events == []
    assert all(s.avatar_sha256 == sha256_bytes(high) for s in snapshots)
    assert len({s.avatar_path for s in snapshots}) == 1
    assert Path(snapshots[-1].avatar_path).read_bytes() == high


def test_higher_quality_avatar_updates_real_file_hash_without_notification(tmp_path, monkeypatch):
    low, high = jpeg(picture(), 150, 65), jpeg(picture())
    snapshots, events = run_avatars(tmp_path, monkeypatch, [low, high, low])
    assert events == []
    assert snapshots[0].avatar_sha256 == sha256_bytes(low)
    assert all(s.avatar_sha256 == sha256_bytes(high) for s in snapshots[1:])
    assert Path(snapshots[0].avatar_path).read_bytes() == low  # History is not deleted.
    assert Path(snapshots[-1].avatar_path).read_bytes() == high


def test_recompression_does_not_hide_other_profile_changes(tmp_path, monkeypatch):
    high, low = jpeg(picture()), jpeg(picture(), 150, 65)
    snapshots, events = run_avatars(tmp_path, monkeypatch, [high, low], followers=[20, 21])
    assert len(events) == 1
    assert events[0]["payload"]["changes"] == {"followers": [20, 21]}
    assert snapshots[-1].followers == 21


def test_same_resolution_recompression_keeps_larger_original(tmp_path, monkeypatch):
    high, low = jpeg(picture(), 640, 95), jpeg(picture(), 640, 70)
    snapshots, events = run_avatars(tmp_path, monkeypatch, [high, low])
    assert events == []
    assert snapshots[-1].avatar_sha256 == sha256_bytes(high)


def test_equal_hash_and_rotating_url_do_not_notify(tmp_path, monkeypatch):
    data = jpeg(picture())
    snapshots, events = run_avatars(tmp_path, monkeypatch, [data, data])
    assert events == []
    assert snapshots[0].avatar_path == snapshots[1].avatar_path


def test_same_shape_but_changed_solid_color_is_not_hidden(tmp_path, monkeypatch):
    red = jpeg(Image.new("RGB", (640, 640), "red"))
    blue = jpeg(Image.new("RGB", (640, 640), "blue"))
    _snapshots, events = run_avatars(tmp_path, monkeypatch, [red, blue])
    assert len(events) == 1


@pytest.mark.parametrize("edit", ["different", "color", "crop", "text"])
def test_actual_visual_change_still_notifies_even_when_smaller(tmp_path, monkeypatch, edit):
    image = picture()
    if edit == "different":
        image = Image.new("RGB", image.size, "white")
    elif edit == "color":
        image = ImageOps.grayscale(image).convert("RGB")
    elif edit == "crop":
        image = image.crop((80, 80, 600, 600))
    else:
        ImageDraw.Draw(image).rectangle((300, 330, 460, 410), fill="white")
    high, changed = jpeg(picture()), jpeg(image, 150, 65)
    snapshots, events = run_avatars(tmp_path, monkeypatch, [high, changed, high])
    assert len(events) == 2  # Returning to a historical avatar is a real change too.
    assert all("avatar_sha256" in e["payload"]["changes"] for e in events)
    assert snapshots[1].avatar_sha256 == sha256_bytes(changed)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "replaced"])
def test_unverifiable_old_file_cannot_suppress_notification(tmp_path, monkeypatch, damage):
    def damage_old(index, snapshots):
        if index != 1:
            return
        path = Path(snapshots[0].avatar_path)
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"broken" if damage == "corrupt" else jpeg(Image.new("RGB", (640, 640), "white")))

    _snapshots, events = run_avatars(tmp_path, monkeypatch,
        [jpeg(picture()), jpeg(picture(), 150, 65)], before_run=damage_old)
    assert len(events) == 1
