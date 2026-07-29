from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from .models import PROFILE_FIELDS, PrivacyState, ProfileSnapshot


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_key(*parts: object) -> str:
    raw = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_name(value: str, fallback: str = "item") -> str:
    clean = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("._-")
    return clean[:120] or fallback


def extension_for(url: str, content_type: str | None, kind: str) -> str:
    content = (content_type or "").split(";", 1)[0].lower()
    known = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
             "image/gif": ".gif", "video/mp4": ".mp4", "video/webm": ".webm",
             "video/quicktime": ".mov"}
    if content in known:
        return known[content]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".mov"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".mp4" if kind == "video" else ".jpg"


def privacy_label(value: PrivacyState | str) -> str:
    raw = value.value if isinstance(value, PrivacyState) else value
    return {"private": "私人", "public": "公開", "unknown": "未知"}.get(raw, str(raw))


def snapshot_changes(old: ProfileSnapshot, new: ProfileSnapshot) -> dict[str, tuple[Any, Any]]:
    changes: dict[str, tuple[Any, Any]] = {}
    for field in PROFILE_FIELDS:
        before = getattr(old, field)
        after = getattr(new, field)
        if field == "bio":
            before, after = normalize_text(before), normalize_text(after)
        if before != after:
            changes[field] = (before, after)
    return changes


def save_diagnostic(root: Path, account_key: str, html: str | None, screenshot: bytes | None,
                    detail: str, keep: int) -> Path:
    directory = root / safe_name(account_key)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = directory / stamp
    (base.with_suffix(".txt")).write_text(detail, encoding="utf-8")
    if html:
        base.with_suffix(".html").write_text(html, encoding="utf-8", errors="replace")
    if screenshot:
        base.with_suffix(".png").write_bytes(screenshot)
    groups = sorted({p.stem for p in directory.iterdir() if p.is_file()}, reverse=True)
    for old in groups[keep:]:
        for path in directory.glob(f"{old}.*"):
            path.unlink(missing_ok=True)
    return base


@contextmanager
def process_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()
