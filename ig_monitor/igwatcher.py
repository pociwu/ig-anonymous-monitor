"""Bounded IGWatcher adapter for explicitly unverified source memberships.

This does not relax the independent validation probe. Source IDs and local
observation IDs are different things; neither establishes the queried author.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx

from .config import BrowserConfig
from .models import (
    COLLECTIONS, CollectionObservation, MediaCandidate, MediaGroupObservation,
    PrivacyState, ProfileSnapshot, ScrapeFailure, ScrapeResult, TerminalState,
)
from .scraper import ProfileScraper


BASE_URL = "https://igwatcher.com"
PATHS = {
    "profile": "/wp-json/igw/v1/search", "stories": "/wp-json/igw/v1/stories",
    "posts": "/wp-json/igw/v1/posts", "reels": "/api/reels",
    "highlights": "/wp-json/igw/v1/highlights", "highlight_items": "/api/highlight-items",
}
JSON_BYTE_LIMIT = 2 * 1024 * 1024
MEDIA_BYTE_LIMIT = 100 * 1024 * 1024
ITEM_LIMIT = 100
CHILD_LIMIT = 20
_MEDIA_ID = re.compile(r"([1-9][0-9]{0,24})(?:_([1-9][0-9]{0,24}))?", re.ASCII)
_PROFILE_ID = re.compile(r"[1-9][0-9]{0,24}", re.ASCII)
_ALBUM_ID = re.compile(r"highlight:[1-9][0-9]{0,24}", re.ASCII)


class _ContractError(ValueError):
    pass


def _profile_count(user: dict, direct: str, edge: str) -> int:
    """Resolve known count locations without masking malformed or stale aliases."""
    values = []
    if direct in user:
        values.append(user[direct])
    if edge in user:
        container = user[edge]
        if not isinstance(container, dict) or "count" not in container:
            raise _ContractError(f"個人檔案計數容器無效：{edge}.count")
        values.append(container["count"])
    if not values or any(type(value) is not int or value < 0 for value in values):
        raise _ContractError(f"個人檔案計數缺失或不是非負整數：{direct}/{edge}.count")
    if any(value != values[0] for value in values[1:]):
        raise _ContractError(f"個人檔案計數互相矛盾：{direct}/{edge}.count")
    return values[0]


def _username(url: str) -> str:
    try:
        parsed = urlsplit(url)
        parts = [part for part in parsed.path.split("/") if part]
        if (parsed.scheme != "https" or parsed.hostname not in {
            "instagram.com", "www.instagram.com", "insta-stories-viewer.com",
            "mollygram.com", "anonyig.com",
        } or parsed.username or parsed.password or parsed.port not in (None, 443)
                or len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", parts[0])):
            raise ValueError
    except (TypeError, ValueError):
        raise _ContractError("帳號網址不是單一 Instagram 使用者名稱") from None
    return parts[0]


def _media_url(value) -> str:
    try:
        if not isinstance(value, str) or len(value) > 8192:
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or "\\" in value
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)
                or not any(host == suffix or host.endswith("." + suffix)
                           for suffix in ("cdninstagram.com", "fbcdn.net"))):
            raise ValueError
    except (ValueError, TypeError):
        raise _ContractError("媒體網址不是允許的 HTTPS 媒體主機") from None
    return value


def _json(body: bytes) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(_):
        raise ValueError

    try:
        data = json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise _ContractError("來源回應不是明確的 JSON 物件") from None


def _items(data: dict) -> list:
    items = data.get("data")
    if not isinstance(items, list) or len(items) > ITEM_LIMIT:
        raise _ContractError("來源集合不是容量範圍內的明確陣列")
    return items


def _challenge(data: dict) -> bool:
    diagnostic = " ".join(str(data.get(key, ""))[:512]
                          for key in ("error", "error_type", "note", "message"))
    return data.get("verification_required") is True or bool(re.search(
        r"captcha|turnstile|rate.?limit|too many requests|access denied|forbidden|unauthorized|verification required",
        diagnostic, re.I,
    ))


def _cursor(data: dict) -> str | None:
    has_more = data.get("has_more")
    cursor = data.get("nextMaxId", data.get("next_max_id"))
    if has_more is None:
        return None  # Unknown pagination is never upgraded to complete.
    if type(has_more) is not bool:
        raise _ContractError("來源分頁狀態無效")
    if has_more is False:
        if cursor not in (None, ""):
            raise _ContractError("來源分頁游標與結束狀態矛盾")
        return None
    if (not isinstance(cursor, str) or not 1 <= len(cursor) <= 256
            or any(ord(c) < 32 or ord(c) == 127 for c in cursor)):
        raise _ContractError("來源分頁游標缺失或無效")
    return cursor


def _id(value) -> str:
    if not isinstance(value, str) or not _MEDIA_ID.fullmatch(value):
        raise _ContractError("媒體 ID 缺失或不是有效字串")
    return value


def _timestamp(value) -> str:
    if (type(value) not in (int, float) or not 1262304000 <= value <= time.time() + 300):
        raise _ContractError("媒體發佈時間缺失或超出有效範圍")
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds")


def _asset(item: dict, *, typed: bool = False) -> tuple[str, str]:
    kind = item.get("media_type")
    flag = item.get("is_video")
    if ((typed and type(kind) is not int) or (kind is not None and (type(kind) is not int or kind not in (1, 2)))
            or ("is_video" in item and type(flag) is not bool)
            or (kind == 1 and flag is True) or (kind == 2 and flag is False)):
        raise _ContractError("媒體型別缺失或互相矛盾")
    video_url = item.get("video_url")
    video = flag is True or kind == 2 or (kind is None and flag is None and bool(video_url))
    return ("video", _media_url(video_url)) if video else ("image", _media_url(item.get("image_url")))


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _candidate(item_id: str | None, category: str, kind: str, url: str, snapshot: ProfileSnapshot,
               profile_id: str, *, parent_id=None, album=None, revision="", position=0,
               published_at=None, caption=None) -> MediaCandidate:
    canonical = _MEDIA_ID.fullmatch(item_id)[1] if item_id else None
    # A URL participates only in a provisional observation, never in an IG ID.
    key = ("igwatcher:local:" + _digest([profile_id, parent_id, revision, position, kind])
           if item_id is None else "igwatcher:source:" + _digest([profile_id, canonical, kind]))
    return MediaCandidate(
        media_key=key, category=category, kind=kind, url=url,
        logical_id=parent_id or item_id, position=position, published_at=published_at,
        source="igwatcher", source_media_id=item_id, parent_id=parent_id,
        album_id=album["id"] if album else None, album_title=album.get("title") if album else None,
        caption=caption, identity_kind="source" if item_id else "local",
        ownership_status="pending", queried_username=snapshot.username, revision_id=revision,
    )


def _post(item: dict, category: str, snapshot: ProfileSnapshot, profile_id: str):
    parent_id = _id(item.get("id"))
    published = _timestamp(item.get("taken_at_timestamp"))
    caption = item.get("caption") if isinstance(item.get("caption"), str) else None
    kind = item.get("media_type")
    if type(kind) is not int or kind not in (1, 2, 8):
        raise _ContractError("貼文媒體型別不明")
    if category == "reels":
        if (kind != 2 or item.get("is_video") is not True
                or item.get("product_type") not in ("clips", "feed")):
            raise _ContractError("來源未確認為 Reel 或明確的影片貼文")
        # IGWatcher also returns ordinary feed videos through its reels endpoint.
        # Preserve them as pending posts, never relabel them as confirmed Reels.
        if item["product_type"] == "feed":
            category = "posts"
    if "is_carousel" in item and type(item["is_carousel"]) is not bool:
        raise _ContractError("輪播型別不明")
    if item.get("is_carousel") is True and kind != 8:
        raise _ContractError("輪播型別互相矛盾")
    if kind != 8:
        if item.get("children") not in (None, []):
            raise _ContractError("單項貼文包含未解釋子項")
        media_kind, media_url = _asset(item, typed=True)
        candidate = _candidate(parent_id, category, media_kind, media_url, snapshot, profile_id,
                               parent_id=parent_id, published_at=published, caption=caption)
        return [candidate], MediaGroupObservation(category, parent_id, "", 1, 1, snapshot.observed_at), False
    children = item.get("children")
    if not isinstance(children, list) or not 1 <= len(children) <= CHILD_LIMIT:
        raise _ContractError("輪播子項缺失或超出容量上限")
    count = item.get("carousel_count")
    if type(count) is not int or not 0 <= count <= CHILD_LIMIT:
        raise _ContractError("輪播宣告數量缺失或無效")
    revision = "observation:" + _digest([count, [
        [child.get("id"), child.get("is_video"), child.get("image_url"), child.get("video_url")]
        if isinstance(child, dict) else ["invalid"] for child in children
    ]])
    candidates, invalid, seen = [], count != len(children), set()
    for position, child in enumerate(children):
        try:
            if not isinstance(child, dict):
                raise _ContractError("輪播子項不是物件")
            child_id = _id(child["id"]) if "id" in child else None
            if child_id is not None:
                canonical = _MEDIA_ID.fullmatch(child_id)[1]
                if canonical in seen:
                    raise _ContractError("輪播子項 ID 重複")
                seen.add(canonical)
            media_kind, media_url = _asset(child)
            candidates.append(_candidate(child_id, category, media_kind, media_url, snapshot, profile_id,
                parent_id=parent_id, revision=revision, position=position, published_at=published, caption=caption))
        except _ContractError:
            invalid = True
    group = MediaGroupObservation(category, parent_id, revision, count, len(children), snapshot.observed_at)
    return candidates, group, invalid


def _story(item: dict, snapshot: ProfileSnapshot, profile_id: str, *, position=0, album=None):
    item_id = _id(item.get("id"))
    published = _timestamp(item.get("taken_at"))
    if album is None:
        expiry, taken = item.get("expiring_at"), item["taken_at"]
        if (type(expiry) not in (int, float) or not max(time.time(), taken) < expiry <= taken + 2 * 86400):
            raise _ContractError("限時動態已過期或到期時間不明")
    kind, url = _asset(item)
    return _candidate(item_id, "highlights" if album else "stories", kind, url, snapshot, profile_id,
                      parent_id=None if album else item_id,
                      album=album, position=position, published_at=published)


class IGWatcherScraper(ProfileScraper):
    """Same scraper interface, with no browser, retries, credentials or redirects."""

    media_referer = BASE_URL + "/"
    supports_progress = True

    def __new__(cls, config: BrowserConfig, *, transport=None):
        return object.__new__(cls)

    def __init__(self, config: BrowserConfig, *, transport=None):
        self.config = config
        self.source_guard = None
        self._transport = transport
        self._client = None
        self._blocked = False

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            transport=self._transport, trust_env=False, follow_redirects=False,
            timeout=min(self.config.timeout_seconds, 20),
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )
        return self

    async def __aexit__(self, *_):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _guard(self):
        if self._blocked or (self.source_guard and self.source_guard()):
            raise ScrapeFailure("來源暫停，未送出請求", "IGWatcher", blocker="source_cooldown")

    async def _read(self, url: str, *, params=None, media=False, _redirects=0) -> tuple[bytes, str]:
        self._guard()
        if self._client is None:
            raise ScrapeFailure("來源 HTTP 工作階段尚未開啟", "IGWatcher")
        self._client.cookies.clear()
        limit = MEDIA_BYTE_LIMIT if media else JSON_BYTE_LIMIT
        try:
            async with asyncio.timeout(min(self.config.timeout_seconds, 30 if media else 20)):
                headers = {"Accept": "image/*, video/*, application/octet-stream", "Referer": self.media_referer} if media else None
                async with self._client.stream("GET", url, params=params, headers=headers) as response:
                    if response.status_code in (401, 403, 422, 429):
                        self._blocked = True
                        raise ScrapeFailure("來源驗證／限流／拒絕存取", "IGWatcher", blocker="source_blocked")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise _ContractError("來源回應壓縮格式未驗證")
                    response_limit = limit if response.status_code == 200 else JSON_BYTE_LIMIT
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(body) + len(chunk) > response_limit:
                            raise _ContractError("來源回應超出容量上限")
                        body.extend(chunk)
                    result = bytes(body)
                    blocked = b"cf-chl-" in result[:JSON_BYTE_LIMIT] or b"challenges.cloudflare.com/turnstile" in result[:JSON_BYTE_LIMIT]
                    if len(result) <= JSON_BYTE_LIMIT and result.lstrip().startswith(b"{"):
                        try:
                            blocked |= _challenge(_json(result))
                        except _ContractError:
                            pass
                    if blocked:
                        self._blocked = True
                        raise ScrapeFailure("來源要求驗證", "IGWatcher", blocker="source_blocked")
                    if media and response.status_code in (301, 302, 303, 307, 308):
                        detail = f" [http_status={response.status_code}]"
                        if _redirects >= 3:
                            raise _ContractError("媒體重新導向超過 3 次上限" + detail)
                        location = response.headers.get("location", "")
                        try:
                            if (not location or len(location) > 8192 or "\\" in location
                                    or any(ord(c) <= 32 or ord(c) == 127 for c in location)):
                                raise ValueError
                            target = _media_url(urljoin(url, location))
                        except (ValueError, _ContractError):
                            raise _ContractError("媒體重新導向目的地缺失或不在允許範圍" + detail) from None
                        # Keep the outer timeout active across the entire chain.
                        # _read clears cookies and checks the source guard on every hop.
                        return await self._read(target, media=True, _redirects=_redirects + 1)
                    if response.status_code != 200:
                        detail = f" [http_status={response.status_code}]" if media else ""
                        raise _ContractError("來源 HTTP 回應不成功" + detail)
                    return result, response.headers.get("content-type", "")
        except ScrapeFailure:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ScrapeFailure("來源請求逾時；本輪不重試", "IGWatcher") from None
        except httpx.HTTPError:
            raise ScrapeFailure("來源連線失敗；本輪不重試", "IGWatcher") from None

    async def _get(self, endpoint: str, params: dict) -> dict:
        body, _ = await self._read(BASE_URL + PATHS[endpoint], params=params)
        data = _json(body)
        if _challenge(data):
            self._blocked = True
            raise ScrapeFailure("來源要求驗證", "IGWatcher", blocker="source_blocked")
        if (any(data.get(key) for key in ("error", "errors", "error_type", "note", "toolDown"))
                or ("success" in data and data["success"] is not True)
                or data.get("status") != "success" or type(data.get("code")) is not int or data["code"] != 200):
            raise _ContractError("來源未回傳明確成功狀態")
        return data

    async def _profile(self, url: str) -> tuple[ProfileSnapshot, str]:
        username = _username(url)
        data = await self._get("profile", {"username": username})
        user = data.get("data", {}).get("user") if isinstance(data.get("data"), dict) else None
        if not isinstance(user, dict):
            raise _ContractError("個人檔案缺少唯一 user")
        actual = user.get("username")
        profile_id = user.get("id")
        if (not isinstance(actual, str) or actual.casefold() != username.casefold()
                or not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id)
                or user.get("pk", profile_id) != profile_id):
            raise _ContractError("個人檔案身分缺失或與查詢不符")
        if type(user.get("is_private")) is not bool:
            raise _ContractError("個人檔案隱私狀態不明")
        posts = _profile_count(user, "media_count", "edge_owner_to_timeline_media")
        followers = _profile_count(user, "follower_count", "edge_followed_by")
        following = _profile_count(user, "following_count", "edge_follow")
        if not isinstance(user.get("biography"), str) or not isinstance(user.get("full_name"), str):
            raise _ContractError("個人檔案文字欄位缺失")
        return ProfileSnapshot(
            username=actual, display_name=user["full_name"], bio=user["biography"],
            posts=posts, followers=followers, following=following,
            privacy=PrivacyState.PRIVATE if user["is_private"] else PrivacyState.PUBLIC,
            avatar_url=_media_url(user.get("profile_pic_url")),
            observed_at=datetime.now(UTC).isoformat(timespec="microseconds"),
        ), profile_id

    async def scrape_profile_only(self, url: str) -> ProfileSnapshot:
        self.last_profile_id = None
        try:
            snapshot, self.last_profile_id = await self._profile(url)
            return snapshot
        except _ContractError as exc:
            raise ScrapeFailure(str(exc), "IGWatcher 個人檔案") from None

    async def download(self, url: str, referer: str) -> tuple[bytes, str]:
        """Download only allowlisted HTTPS CDN objects, never arbitrary proxies."""
        try:
            safe_url = _media_url(url)
            body, content_type = await self._read(safe_url, media=True)
            mime = content_type.split(";", 1)[0].strip().lower()
            image = (body.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a"))
                     or (body.startswith(b"RIFF") and body[8:12] == b"WEBP")
                     or (body[4:8] == b"ftyp" and body[8:12] in (b"avif", b"avis")))
            video = (body[4:8] == b"ftyp" and body[8:12] not in (b"avif", b"avis")) or body.startswith(b"\x1a\x45\xdf\xa3")
            if not body or not (
                (image and mime in {"image/jpeg", "image/png", "image/gif", "image/webp", "image/avif", "application/octet-stream"})
                or (video and mime in {"video/mp4", "video/webm", "application/octet-stream"})
            ):
                raise _ContractError("下載回應不是受支援的媒體檔案")
            return body, content_type
        except _ContractError as exc:
            raise ScrapeFailure(str(exc), "IGWatcher 媒體下載") from None

    async def scrape(self, url: str, *, cursors=None, known_ids=None, completed_categories=None) -> ScrapeResult:
        try:
            snapshot, profile_id = await self._profile(url)
        except _ContractError as exc:
            raise ScrapeFailure(str(exc), "IGWatcher 個人檔案") from None
        result = ScrapeResult(snapshot=snapshot, source="igwatcher", profile_id=profile_id)
        if snapshot.privacy == PrivacyState.PRIVATE:
            result.collections = {key: CollectionObservation(TerminalState.PRIVATE) for key in COLLECTIONS}
            result.posts_state = result.stories_state = TerminalState.PRIVATE
            return result
        for category in ("stories", "posts", "reels", "highlights"):
            invalid, progress_cursor = False, None
            try:
                params = {"username": snapshot.username}
                if category in ("posts", "reels"):
                    params["limit"] = 24 if category == "posts" else 12
                payload = await self._get(category, params)
                items = _items(payload)
                max_pages = max(1, min(20, self.config.max_pages_per_collection))
                if category == "highlights":
                    start, prior = 0, (cursors or {}).get("highlights")
                    if prior:
                        if not isinstance(prior, str) or not prior.startswith("after:") or not _ALBUM_ID.fullmatch(prior[6:]):
                            raise _ContractError("本地精華進度游標無效")
                        start = next((index + 1 for index, album in enumerate(items)
                                      if isinstance(album, dict) and album.get("id") == prior[6:]), 0)
                    album_seen = set()
                    for album_index, album in enumerate(items[start:start + max_pages], start):
                        if (not isinstance(album, dict) or not isinstance(album.get("id"), str)
                                or not _ALBUM_ID.fullmatch(album["id"]) or type(album.get("media_count")) is not int
                                or album["media_count"] < 0 or not isinstance(album.get("title", ""), str)
                                or album["id"] in album_seen):
                            invalid = True
                            continue
                        album_seen.add(album["id"])
                        # Ordinary album failures are revisited on the next
                        # cycle; they must not starve other accessible albums.
                        progress_cursor = "after:" + album["id"] if album_index + 1 < len(items) else None
                        try:
                            children = _items(await self._get("highlight_items", {"highlight_id": album["id"], "user": snapshot.username}))
                        except (_ContractError, ScrapeFailure) as exc:
                            if isinstance(exc, ScrapeFailure) and exc.blocker:
                                raise
                            invalid = True
                            continue
                        received, seen = [], set()
                        for position, item in enumerate(children):
                            try:
                                if not isinstance(item, dict):
                                    raise _ContractError("媒體項目不是物件")
                                candidate = _story(item, snapshot, profile_id, position=position, album=album)
                                if candidate.media_key in seen:
                                    raise _ContractError("精華項目 ID 重複")
                                seen.add(candidate.media_key)
                                received.append(candidate)
                            except _ContractError:
                                invalid = True
                        result.media.extend(received)
                        revision = "observation:" + _digest([
                            album["media_count"], len(children), [item.source_media_id for item in received],
                        ])
                        result.groups.append(MediaGroupObservation(category, album["id"], revision, album["media_count"],
                            len(children), snapshot.observed_at, album_title=album.get("title")))
                else:
                    limit = max(1, min(100, self.config.initial_posts if category == "posts" else self.config.initial_reels if category == "reels" else ITEM_LIMIT))
                    seen, cursor_seen = set(), set()
                    for page in range(1 if category == "stories" else max_pages):
                        for item in items:
                            if len(seen) >= limit:
                                break
                            try:
                                if not isinstance(item, dict):
                                    raise _ContractError("媒體項目不是物件")
                                item_id = _id(item.get("id"))
                                canonical = _MEDIA_ID.fullmatch(item_id)[1]
                                if canonical in seen:
                                    raise _ContractError("媒體 ID 重複")
                                seen.add(canonical)
                                if category == "stories":
                                    candidate = _story(item, snapshot, profile_id)
                                    result.media.append(candidate)
                                    result.groups.append(MediaGroupObservation(category, item_id, "", 1, 1, snapshot.observed_at))
                                else:
                                    candidates, group, bad = _post(item, category, snapshot, profile_id)
                                    result.media.extend(candidates)
                                    result.groups.append(group)
                                    invalid |= bad
                            except _ContractError:
                                invalid = True
                        if category == "stories":
                            break
                        next_cursor = _cursor(payload)
                        if next_cursor and (not items or next_cursor in cursor_seen):
                            raise _ContractError("來源分頁游標未前進或回傳空續頁")
                        if not next_cursor or len(seen) >= limit or page + 1 == max_pages:
                            break
                        cursor_seen.add(next_cursor)
                        payload = await self._get(category, {**params, "maxId": next_cursor})
                        items = _items(payload)
                result.collections[category] = CollectionObservation(
                    TerminalState.PARTIAL, error="部分項目未成功取得或格式無效，已保留可接納的項目" if invalid else None,
                    cursor=progress_cursor)
            except (_ContractError, ScrapeFailure) as exc:
                blocked = isinstance(exc, ScrapeFailure) and bool(exc.blocker)
                result.collections[category] = CollectionObservation(
                    TerminalState.BLOCKED if blocked else TerminalState.PARTIAL, error=str(exc))
                if blocked:
                    for key in COLLECTIONS:
                        result.collections.setdefault(key, CollectionObservation(TerminalState.BLOCKED, error="來源暫停，未送出後續請求"))
                    break
        result.posts_state = result.collections["posts"].state
        result.stories_state = result.collections["stories"].state
        return result
