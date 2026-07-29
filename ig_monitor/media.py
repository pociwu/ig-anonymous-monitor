from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import DedupConfig
from .db import Database
from .dedup import MediaFingerprint, fingerprint_bytes, fingerprint_file, is_similar, quality_rank, row_fingerprint
from .scraper import ProfileScraper
from .utils import extension_for, safe_name, sha256_bytes


LOG = logging.getLogger("ig_monitor")


def _validate_payload(data: bytes, content_type: str | None, kind: str) -> None:
    if not data:
        raise ValueError("下載內容為空")
    content = (content_type or "").lower()
    prefix = data[:200].lower()
    if "text/html" in content or b"<!doctype html" in prefix or b"<html" in prefix:
        raise ValueError("媒體網址回傳 HTML，可能已過期或被攔截")
    if kind == "video" and content and not content.startswith(("video/", "application/octet-stream")):
        raise ValueError(f"影片 Content-Type 不正確：{content_type}")
    if kind == "image" and content and not content.startswith(("image/", "application/octet-stream")):
        raise ValueError(f"圖片 Content-Type 不正確：{content_type}")


async def save_avatar(scraper: ProfileScraper, root: Path, account_key: str,
                      url: str, referer: str) -> tuple[str, str]:
    data, content_type = await scraper.download(url, referer)
    _validate_payload(data, content_type, "image")
    digest = sha256_bytes(data)
    directory = root / safe_name(account_key) / "avatar"
    directory.mkdir(parents=True, exist_ok=True)
    existing = next(directory.glob(f"*_{digest[:12]}.*"), None)
    if existing:
        return digest, str(existing)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{stamp}_{digest[:12]}{extension_for(url, content_type, 'image')}"
    if not path.exists():
        temp = path.with_suffix(path.suffix + ".part")
        temp.write_bytes(data)
        temp.replace(path)
    return digest, str(path)


async def download_account_media(db: Database, scraper: ProfileScraper, account: dict[str, Any],
                                 root: Path, limit: int, dedup: DedupConfig) -> dict[str, Any]:
    stats: dict[str, Any] = {"downloaded": 0, "photos": 0, "videos": 0,
                             "duplicate": 0, "upgraded": 0, "failed": 0,
                             "pending": 0, "attachments": []}
    items = db.pending_media(account["id"], limit)
    for item in items:
        try:
            data, content_type = await scraper.download(item["url"], account["url"])
            _validate_payload(data, content_type, item["kind"])
            digest = sha256_bytes(data)
            duplicate = db.downloaded_by_hash(account["id"], digest)
            if duplicate and duplicate.get("local_path"):
                db.mark_media_duplicate(item["id"], duplicate["id"], digest)
                stats["duplicate"] += 1
                continue
            fingerprint: MediaFingerprint | None = None
            if dedup.enabled:
                try:
                    fingerprint = await asyncio.to_thread(
                        fingerprint_bytes, data, item["kind"], Path(urlparse(item["url"]).path).suffix
                    )
                except Exception as exc:
                    LOG.warning("Media fingerprint failed for %s: %s", item["media_key"], exc)
            similar: tuple[dict[str, Any], MediaFingerprint] | None = None
            if fingerprint:
                similar = await _find_similar(db, account["id"], item["id"], item["kind"], fingerprint, dedup)

            category = safe_name(item["category"], "posts")
            directory = root / safe_name(account["account_key"]) / category
            directory.mkdir(parents=True, exist_ok=True)
            date = _date_prefix(item.get("published_at"))
            logical = safe_name(str(item.get("logical_id") or item["media_key"][:16]))
            position = int(item.get("position") or 0)
            ext = extension_for(item["url"], content_type, item["kind"])
            path = directory / f"{date}_{logical}_{position:03d}_{digest[:10]}{ext}"

            if similar:
                canonical, canonical_fingerprint = similar
                if quality_rank(fingerprint, item["id"]) <= quality_rank(canonical_fingerprint, canonical["id"]):
                    db.mark_media_duplicate(item["id"], canonical["id"], digest, fingerprint.to_json())
                    stats["duplicate"] += 1
                    continue

            if not path.exists():
                temp = path.with_suffix(path.suffix + ".part")
                temp.write_bytes(data)
                temp.replace(path)
            db.mark_media_downloaded(
                item["id"], str(path), digest, fingerprint.to_json() if fingerprint else None,
                fingerprint.width if fingerprint else item.get("width"),
                fingerprint.height if fingerprint else item.get("height"),
                fingerprint.size_bytes if fingerprint else len(data),
                fingerprint.duration_seconds if fingerprint else None,
                fingerprint.bitrate if fingerprint else None,
            )
            if similar:
                old, _ = similar
                old_paths = db.promote_canonical(item["id"], old["id"])
                _delete_unreferenced(db, old_paths, keep=path)
                stats["duplicate"] += 1
                stats["upgraded"] += 1
                LOG.info("%s media quality upgraded: %s -> %s", account["label"], old["id"], item["id"])
                continue
            stats["downloaded"] += 1
            stats["videos" if item["kind"] == "video" else "photos"] += 1
            stats["attachments"].append({"kind": item["kind"], "path": str(path)})
        except Exception as exc:
            db.mark_media_failed(item["id"], str(exc))
            stats["failed"] += 1
    counts = db.media_counts(account["id"])
    stats["pending"] = counts.get("pending", 0) + counts.get("failed", 0)
    return stats


async def _find_similar(
    db: Database, account_id: int, media_id: int, kind: str,
    fingerprint: MediaFingerprint, config: DedupConfig,
) -> tuple[dict[str, Any], MediaFingerprint] | None:
    for candidate in db.canonical_media(account_id, kind, media_id):
        candidate_fingerprint = row_fingerprint(candidate)
        candidate_path = Path(candidate["local_path"]) if candidate.get("local_path") else None
        if candidate_fingerprint is None and candidate_path and candidate_path.is_file():
            try:
                candidate_fingerprint = await asyncio.to_thread(fingerprint_file, candidate_path, kind)
                db.store_media_fingerprint(
                    candidate["id"], candidate.get("sha256") or sha256_bytes(candidate_path.read_bytes()),
                    candidate_fingerprint.to_json(), candidate_fingerprint.width, candidate_fingerprint.height,
                    candidate_fingerprint.size_bytes, candidate_fingerprint.duration_seconds,
                    candidate_fingerprint.bitrate,
                )
            except Exception as exc:
                LOG.warning("Existing media fingerprint failed for %s: %s", candidate["id"], exc)
        if candidate_fingerprint and is_similar(fingerprint, candidate_fingerprint, config):
            return candidate, candidate_fingerprint
    return None


def _delete_unreferenced(db: Database, paths: list[str], keep: Path | None = None) -> None:
    for value in paths:
        path = Path(value)
        if keep and path == keep:
            continue
        if not db.media_path_referenced(value):
            path.unlink(missing_ok=True)


def _date_prefix(value: str | None) -> str:
    if not value:
        return "undated"
    raw = str(value)
    try:
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw), UTC).strftime("%Y%m%dT%H%M%SZ")
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    except (ValueError, OSError, OverflowError):
        return safe_name(raw, "undated")[:24]
