from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import imagehash
from PIL import Image

from .config import DedupConfig


@dataclass(frozen=True, slots=True)
class MediaFingerprint:
    kind: str
    width: int
    height: int
    size_bytes: int
    image_phash: str | None = None
    image_colorhash: str | None = None
    duration_seconds: float | None = None
    bitrate: int | None = None
    frame_phashes: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str | None) -> MediaFingerprint | None:
        if not value:
            return None
        data = json.loads(value)
        data["frame_phashes"] = tuple(data.get("frame_phashes") or ())
        return cls(**data)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_bytes(data: bytes, kind: str, suffix: str = "") -> MediaFingerprint:
    if kind == "image":
        return _image_fingerprint(data)
    with tempfile.NamedTemporaryFile(suffix=suffix or ".mp4", delete=False) as temp:
        temp.write(data)
        temp_path = Path(temp.name)
    try:
        return _video_fingerprint(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)


def fingerprint_file(path: Path, kind: str) -> MediaFingerprint:
    if kind == "image":
        return _image_fingerprint(path.read_bytes())
    return _video_fingerprint(path)


def _image_fingerprint(data: bytes) -> MediaFingerprint:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        width, height = image.size
        phash = str(imagehash.phash(image.convert("RGB")))
        colorhash = str(imagehash.colorhash(image.convert("RGB")))
    return MediaFingerprint(
        "image", int(width), int(height), len(data),
        image_phash=phash, image_colorhash=colorhash,
    )


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _video_fingerprint(path: Path) -> MediaFingerprint:
    if not ffmpeg_available():
        return MediaFingerprint("video", 0, 0, path.stat().st_size)
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_entries", "format=duration,bit_rate,size:stream=codec_type,width,height,bit_rate",
            str(path),
        ],
        capture_output=True, text=True, timeout=30, check=True,
    )
    payload = json.loads(probe.stdout or "{}")
    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    fmt = payload.get("format") or {}
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    duration = float(fmt.get("duration") or 0)
    bitrate = int(video.get("bit_rate") or fmt.get("bit_rate") or 0)
    hashes: list[str] = []
    for fraction in (0.1, 0.5, 0.9):
        position = max(0.0, duration * fraction)
        try:
            frame = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-ss", f"{position:.3f}", "-i", str(path),
                    "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
                ],
                capture_output=True, timeout=30, check=True,
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            frame = b""
        if frame:
            with Image.open(io.BytesIO(frame)) as image:
                hashes.append(str(imagehash.phash(image.convert("RGB"))))
    return MediaFingerprint(
        "video", width, height, int(fmt.get("size") or path.stat().st_size),
        duration_seconds=duration, bitrate=bitrate, frame_phashes=tuple(hashes),
    )


def is_similar(first: MediaFingerprint, second: MediaFingerprint, config: DedupConfig) -> bool:
    if first.kind != second.kind:
        return False
    if not _same_aspect_ratio(first, second, config.aspect_ratio_tolerance_percent):
        return False
    if first.kind == "image":
        if not first.image_phash or not second.image_phash:
            return False
        if _hash_distance(first.image_phash, second.image_phash) > config.image_phash_distance:
            return False
        return not (
            first.image_colorhash and second.image_colorhash
            and _hash_distance(first.image_colorhash, second.image_colorhash) > config.image_phash_distance
        )
    if not first.frame_phashes or len(first.frame_phashes) != len(second.frame_phashes):
        return False
    first_duration = float(first.duration_seconds or 0)
    second_duration = float(second.duration_seconds or 0)
    absolute = abs(first_duration - second_duration)
    relative = absolute / max(first_duration, second_duration, 0.001) * 100
    if absolute > config.video_duration_tolerance_seconds and relative > config.video_duration_tolerance_percent:
        return False
    return all(
        _hash_distance(left, right) <= config.image_phash_distance
        for left, right in zip(first.frame_phashes, second.frame_phashes)
    )


def quality_rank(fingerprint: MediaFingerprint, downloaded_order: int = 0) -> tuple[int, int, int, int]:
    pixels = fingerprint.width * fingerprint.height
    if fingerprint.kind == "video":
        return pixels, int(fingerprint.bitrate or 0), fingerprint.size_bytes, -downloaded_order
    return pixels, 0, fingerprint.size_bytes, -downloaded_order


def _same_aspect_ratio(first: MediaFingerprint, second: MediaFingerprint, tolerance_percent: float) -> bool:
    if not first.width or not first.height or not second.width or not second.height:
        return False
    first_ratio = first.width / first.height
    second_ratio = second.width / second.height
    return abs(first_ratio - second_ratio) / max(first_ratio, second_ratio) * 100 <= tolerance_percent


def _hash_distance(first: str, second: str) -> int:
    return (int(first, 16) ^ int(second, 16)).bit_count()


def row_fingerprint(row: dict[str, Any]) -> MediaFingerprint | None:
    return MediaFingerprint.from_json(row.get("fingerprint_json"))


def deduplicate_existing_media(db, config: DedupConfig, apply: bool = False) -> dict[str, Any]:
    rows = db.downloaded_media()
    analyzed: dict[int, tuple[dict[str, Any], str, MediaFingerprint]] = {}
    errors: list[str] = []
    ffmpeg_missing = False
    for row in rows:
        path = Path(row["local_path"])
        if not path.is_file():
            errors.append(f"media {row['id']}: file not found: {path}")
            continue
        try:
            digest = row.get("sha256") or sha256_file(path)
            fingerprint = row_fingerprint(row) or fingerprint_file(path, row["kind"])
            if row["kind"] == "video" and not fingerprint.frame_phashes:
                ffmpeg_missing = True
            analyzed[row["id"]] = (row, digest, fingerprint)
        except Exception as exc:
            errors.append(f"media {row['id']}: {exc}")

    groups: list[list[tuple[dict[str, Any], str, MediaFingerprint]]] = []
    for entry in analyzed.values():
        row, digest, fingerprint = entry
        matching = None
        for group in groups:
            sample_row, sample_digest, sample_fingerprint = group[0]
            if row["account_id"] != sample_row["account_id"] or row["kind"] != sample_row["kind"]:
                continue
            if digest == sample_digest or (config.enabled and is_similar(fingerprint, sample_fingerprint, config)):
                matching = group
                break
        if matching is None:
            groups.append([entry])
        else:
            matching.append(entry)

    duplicate_groups = [group for group in groups if len(group) > 1]
    duplicate_rows = sum(len(group) - 1 for group in duplicate_groups)
    removable_paths: set[str] = set()
    upgrades = 0
    plans = []
    for group in duplicate_groups:
        canonical = max(group, key=lambda item: quality_rank(item[2], item[0]["id"]))
        if canonical[0]["id"] != min(item[0]["id"] for item in group):
            upgrades += 1
        duplicates = [item for item in group if item[0]["id"] != canonical[0]["id"]]
        canonical_path = canonical[0]["local_path"]
        removable_paths.update(
            item[0]["local_path"] for item in duplicates if item[0]["local_path"] != canonical_path
        )
        plans.append((canonical, duplicates))

    if apply:
        for row, digest, fingerprint in analyzed.values():
            db.store_media_fingerprint(
                row["id"], digest, fingerprint.to_json(), fingerprint.width, fingerprint.height,
                fingerprint.size_bytes, fingerprint.duration_seconds, fingerprint.bitrate,
            )
        paths_to_check: set[str] = set()
        for canonical, duplicates in plans:
            canonical_id = canonical[0]["id"]
            for duplicate, digest, fingerprint in duplicates:
                paths_to_check.update(
                    db.mark_media_duplicate(duplicate["id"], canonical_id, digest, fingerprint.to_json())
                )
        for value in paths_to_check:
            if not db.media_path_referenced(value):
                Path(value).unlink(missing_ok=True)

    return {
        "scanned": len(rows), "analyzed": len(analyzed),
        "duplicate_groups": len(duplicate_groups), "duplicate_rows": duplicate_rows,
        "quality_upgrades": upgrades, "removable_files": len(removable_paths),
        "errors": errors, "ffmpeg_missing": ffmpeg_missing, "applied": apply,
    }
