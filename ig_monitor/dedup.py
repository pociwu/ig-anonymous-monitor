from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
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


def quarantine_cross_account_media(
    db,
    *,
    apply: bool = False,
    min_accounts: int = 3,
    since: str | None = None,
    kind: str = "video",
    dedup: DedupConfig | None = None,
) -> dict[str, Any]:
    """Find and non-destructively hide media shared by many monitored accounts.

    This is an incident-response tool for upstream account/media cross-contamination. It is
    intentionally separate from normal perceptual deduplication, whose scope remains one account.
    """
    if min_accounts < 2:
        raise ValueError("min_accounts must be at least 2")
    if kind not in {"video", "image", "all"}:
        raise ValueError("kind must be video, image, or all")
    query = """
      SELECT m.*,a.label
      FROM media m JOIN accounts a ON a.id=m.account_id
      WHERE m.status IN ('pending','failed','downloaded','duplicate')
        AND NOT EXISTS (
          SELECT 1 FROM media_quarantine mq
          WHERE mq.media_id=m.id AND mq.decision='kept'
        )
    """
    params: list[Any] = []
    if kind != "all":
        query += " AND m.kind=?"
        params.append(kind)
    if since:
        query += " AND COALESCE(m.downloaded_at,m.discovered_at)>=?"
        params.append(since)
    query += " ORDER BY m.account_id,m.id"
    rows = [dict(row) for row in db.conn.execute(query, params)]

    by_hash: dict[str, list[dict[str, Any]]] = {}
    by_url: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("sha256") and row["status"] == "downloaded":
            by_hash.setdefault(row["sha256"], []).append(row)
        by_url.setdefault(row["url"].rstrip("/ "), []).append(row)
    candidate_groups: list[tuple[str, str, list[dict[str, Any]]]] = [
        ("sha256", group[0]["sha256"], group) for group in by_hash.values()
        if len({row["account_id"] for row in group}) >= min_accounts
    ]
    candidate_groups.extend(
        ("url", url, group) for url, group in by_url.items()
        if len({row["account_id"] for row in group}) >= min_accounts
    )

    fingerprint_missing = 0
    if dedup and dedup.enabled and kind in {"video", "all"}:
        fingerprint_rows: list[tuple[dict[str, Any], MediaFingerprint]] = []
        for row in rows:
            if row.get("kind") != "video" or row.get("status") != "downloaded":
                continue
            try:
                fingerprint = row_fingerprint(row)
            except (TypeError, ValueError, json.JSONDecodeError):
                fingerprint = None
            if fingerprint is None or not fingerprint.frame_phashes:
                fingerprint_missing += 1
                continue
            fingerprint_rows.append((row, fingerprint))

        perceptual_groups: list[list[tuple[dict[str, Any], MediaFingerprint]]] = []
        for entry in fingerprint_rows:
            row, fingerprint = entry
            matching = next((
                group for group in perceptual_groups
                if is_similar(group[0][1], fingerprint, dedup)
            ), None)
            if matching is None:
                perceptual_groups.append([entry])
            else:
                matching.append(entry)
        for group in perceptual_groups:
            media_rows = [entry[0] for entry in group]
            if len({row["account_id"] for row in media_rows}) < min_accounts:
                continue
            signature = hashlib.sha256(
                ",".join(str(row["id"]) for row in media_rows).encode("ascii")
            ).hexdigest()
            candidate_groups.append(("perceptual", signature, media_rows))

    groups = _merge_cross_account_groups(candidate_groups)
    target_by_id = {
        int(row["id"]): row
        for group in groups
        for row in group["rows"]
    }
    target_rows = list(target_by_id.values())
    target_ids = [int(row["id"]) for row in target_rows]
    paths = {str(row["local_path"]) for row in target_rows if row.get("local_path")}

    if apply and target_ids:
        placeholders = ",".join("?" for _ in target_ids)
        detected_by_id = {
            int(row["id"]): (group["signal"], group["value"])
            for group in groups for row in group["rows"]
        }
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with db.transaction() as con:
            for row in target_rows:
                signal, value = detected_by_id[int(row["id"])]
                con.execute(
                    """INSERT INTO media_quarantine(
                         media_id,reason,signal,signal_value,original_status,
                         quarantined_at,decision,reviewed_at
                       ) VALUES(?,?,?,?,?,?,'pending',NULL)
                       ON CONFLICT(media_id) DO UPDATE SET
                         reason=excluded.reason,signal=excluded.signal,
                         signal_value=excluded.signal_value,
                         original_status=excluded.original_status,
                         quarantined_at=excluded.quarantined_at,
                         decision='pending',reviewed_at=NULL""",
                    (
                        row["id"], "cross-account-source-contamination", signal,
                        value, row["status"], now,
                    ),
                )
            con.execute(
                f"""UPDATE media SET status='quarantined',
                       last_error='cross-account-source-contamination'
                       WHERE id IN ({placeholders})""",
                target_ids,
            )

    samples = [
        {
            "signal": group["signal"],
            "signals": group["signals"],
            "value": group["value"],
            "accounts": sorted({str(row["label"]) for row in group["rows"]}),
            "rows": len(group["rows"]),
            "media_ids": sorted(int(row["id"]) for row in group["rows"]),
            "kinds": sorted({str(row["kind"]) for row in group["rows"]}),
        }
        for group in groups
    ]
    return {
        "scanned": len(rows),
        "groups": len(groups),
        "media_rows": len(target_rows),
        "files": len(paths),
        "files_preserved": len(paths),
        "samples": samples,
        "errors": [],
        "applied": apply,
        "min_accounts": min_accounts,
        "since": since,
        "kind": kind,
        "fingerprint_missing": fingerprint_missing,
    }


def _merge_cross_account_groups(
    candidates: list[tuple[str, str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Collapse SHA, URL, and perceptual detections that refer to the same media rows."""
    groups: list[dict[str, Any]] = []
    for signal, value, rows in candidates:
        row_by_id = {int(row["id"]): row for row in rows}
        overlapping = [group for group in groups if set(group["row_by_id"]) & set(row_by_id)]
        if not overlapping:
            groups.append({
                "signal": signal, "value": value, "signals": [signal], "row_by_id": row_by_id,
            })
            continue
        primary = overlapping[0]
        primary["row_by_id"].update(row_by_id)
        if signal not in primary["signals"]:
            primary["signals"].append(signal)
        for extra in overlapping[1:]:
            primary["row_by_id"].update(extra["row_by_id"])
            for extra_signal in extra["signals"]:
                if extra_signal not in primary["signals"]:
                    primary["signals"].append(extra_signal)
            groups.remove(extra)
    for group in groups:
        group["rows"] = list(group.pop("row_by_id").values())
    return groups


def restore_quarantined_media(db, media_id: int) -> bool:
    """Trust one quarantined item and restore its previous state without moving its file."""
    row = db.conn.execute(
        """SELECT m.local_path,m.status,mq.original_status
           FROM media m JOIN media_quarantine mq ON mq.media_id=m.id
           WHERE m.id=? AND m.status='quarantined' AND mq.decision='pending'""",
        (media_id,),
    ).fetchone()
    if row is None:
        return False
    original_status = str(row["original_status"])
    local_path = row["local_path"]
    if original_status == "downloaded" and (not local_path or not Path(local_path).is_file()):
        raise FileNotFoundError(f"quarantined media file is missing: {local_path or media_id}")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with db.transaction() as con:
        con.execute(
            "UPDATE media SET status=?,last_error=NULL WHERE id=?",
            (original_status, media_id),
        )
        con.execute(
            "UPDATE media_quarantine SET decision='kept',reviewed_at=? WHERE media_id=?",
            (now, media_id),
        )
    return True


def delete_quarantined_media(db, media_id: int) -> dict[str, Any]:
    """Permanently remove one reviewed file while retaining a database tombstone."""
    row = db.conn.execute(
        """SELECT m.local_path,m.status
           FROM media m JOIN media_quarantine mq ON mq.media_id=m.id
           WHERE m.id=? AND m.status='quarantined' AND mq.decision='pending'""",
        (media_id,),
    ).fetchone()
    if row is None:
        return {"deleted": False, "file_deleted": False, "error": None}
    local_path = str(row["local_path"]) if row["local_path"] else None
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with db.transaction() as con:
        con.execute(
            """UPDATE media SET status='deleted',local_path=NULL,duplicate_of_id=NULL,
                 last_error='cross-account-source-contamination-deleted' WHERE id=?""",
            (media_id,),
        )
        con.execute(
            """UPDATE media SET status='deleted',local_path=NULL,duplicate_of_id=NULL,
                 last_error='cross-account-source-contamination-deleted'
               WHERE duplicate_of_id=?""",
            (media_id,),
        )
        con.execute(
            "UPDATE media_quarantine SET decision='deleted',reviewed_at=? WHERE media_id=?",
            (now, media_id),
        )
    file_deleted = False
    error = None
    if local_path and not db.media_path_referenced(local_path):
        try:
            path = Path(local_path)
            file_deleted = path.is_file()
            path.unlink(missing_ok=True)
        except OSError as exc:
            error = str(exc)
    return {"deleted": True, "file_deleted": file_deleted, "error": error}
