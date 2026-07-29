from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import Database
from .scraper import ProfileScraper
from .utils import extension_for, safe_name, sha256_bytes


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
                                 root: Path, limit: int) -> dict[str, Any]:
    stats: dict[str, Any] = {"downloaded": 0, "photos": 0, "videos": 0,
                             "duplicate": 0, "failed": 0, "pending": 0, "attachments": []}
    items = db.pending_media(account["id"], limit)
    for item in items:
        try:
            data, content_type = await scraper.download(item["url"], account["url"])
            _validate_payload(data, content_type, item["kind"])
            digest = sha256_bytes(data)
            duplicate = db.downloaded_by_hash(account["id"], digest)
            if duplicate and duplicate.get("local_path"):
                db.mark_media_downloaded(item["id"], duplicate["local_path"], digest)
                stats["duplicate"] += 1
                continue
            category = safe_name(item["category"], "posts")
            directory = root / safe_name(account["account_key"]) / category
            directory.mkdir(parents=True, exist_ok=True)
            date = _date_prefix(item.get("published_at"))
            logical = safe_name(str(item.get("logical_id") or item["media_key"][:16]))
            position = int(item.get("position") or 0)
            ext = extension_for(item["url"], content_type, item["kind"])
            path = directory / f"{date}_{logical}_{position:03d}_{digest[:10]}{ext}"
            if not path.exists():
                temp = path.with_suffix(path.suffix + ".part")
                temp.write_bytes(data)
                temp.replace(path)
            db.mark_media_downloaded(item["id"], str(path), digest)
            stats["downloaded"] += 1
            stats["videos" if item["kind"] == "video" else "photos"] += 1
            stats["attachments"].append({"kind": item["kind"], "path": str(path)})
        except Exception as exc:
            db.mark_media_failed(item["id"], str(exc))
            stats["failed"] += 1
    counts = db.media_counts(account["id"])
    stats["pending"] = counts.get("pending", 0) + counts.get("failed", 0)
    return stats


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
