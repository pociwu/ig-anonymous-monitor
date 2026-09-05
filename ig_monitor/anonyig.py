"""AnonyIG adapter based on observed, structured website responses.

The website owns request signing and challenge handling. This adapter uses its
ordinary search/tab/scroll UI and only accepts media in verified response fields.
It never scans arbitrary HTML/URLs or solves verification challenges. Reels are
the site's video-filtered Posts view, so their independent completeness is not
established and is always reported as partial.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from .models import CollectionObservation, MediaCandidate, PrivacyState, ProfileSnapshot, ScrapeFailure, ScrapeResult, TerminalState
from .scraper import ProfileScraper
from .utils import normalize_text, stable_key


CATEGORIES = ("posts", "stories", "highlights", "reels")
REELS_LIMITATION = "來源 Reels 是已載貼文影片篩選，獨立完整性未驗證"
_API_ENDPOINTS = frozenset({
    "userInfo", "postsV2", "posts", "stories", "highlights", "highlightStories",
})
_DIAGNOSTIC_ENDPOINTS = _API_ENDPOINTS | {"page", "none"}
_HTTP_BLOCK_TYPES = {
    401: "http_unauthorized", 403: "http_forbidden",
    422: "http_unprocessable", 429: "http_rate_limit",
}
_BLOCK_BODY_TIMEOUT_SECONDS = 0.5
# AnonyIG's observed frontend sends normal HTTP 422/429 lookup failures into
# showCaptcha. This is site-specific inference, not proof of a response challenge.
_HTTP_ERROR_TYPES = {
    401: "unauthorized", 403: "forbidden",
    422: "captcha_required", 429: "rate_limited",
}


class SourceContractError(ValueError):
    """Missing/contradictory evidence is not an empty collection."""


class SourceBlocked(SourceContractError):
    pass


def username_from_url(value: str) -> str:
    parsed = urlsplit(value)
    parts = [part for part in parsed.path.split("/") if part]
    if (parsed.scheme != "https" or parsed.hostname not in {
        "instagram.com", "www.instagram.com", "insta-stories-viewer.com",
        "mollygram.com", "anonyig.com",
    } or len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", parts[0])):
        raise SourceContractError("帳號網址無法解析為單一 Instagram 使用者名稱")
    return parts[0]


def _identifier(value: Any, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise SourceContractError(f"缺少可靠的 {name}")
    return str(value)


def _url(value: Any) -> str:
    if not isinstance(value, str):
        raise SourceContractError("媒體網址缺失")
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}
            or not any(host == suffix or host.endswith("." + suffix) for suffix in
                       ("anonyig.com", "cdninstagram.com", "fbcdn.net", "media.example.test"))):
        raise SourceContractError("媒體網址不是已確認來源的 HTTPS 媒體主機")
    return value


def _body(payload: Any) -> Any:
    if not isinstance(payload, dict) or "result" not in payload:
        raise SourceContractError("來源回應缺少 result，不能視為空集合")
    if payload.get("error") or payload.get("success") is False or payload.get("status") in {"error", "fail", "failed"}:
        raise SourceContractError("來源回應明確表示失敗")
    return payload["result"]


def parse_profile(payload: Any, requested_username: str) -> tuple[ProfileSnapshot, str]:
    result = _body(payload)
    if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
        raise SourceContractError("userInfo 回應不是唯一帳號")
    user = result[0].get("user")
    if not isinstance(user, dict):
        raise SourceContractError("userInfo 缺少 user")
    username = _identifier(user.get("username"), "username")
    if username.casefold() != requested_username.casefold():
        raise SourceContractError("來源回傳不同 username，未取得跨名稱身分證據")
    profile_id = _identifier(user.get("id") or user.get("pk"), "profile ID")
    if user.get("id") and user.get("pk") and str(user["id"]) != str(user["pk"]):
        raise SourceContractError("profile ID 與 pk 衝突")
    for key in ("media_count", "follower_count", "following_count"):
        if type(user.get(key)) is not int or user[key] < 0:
            raise SourceContractError(f"{key} 不是完整非負整數")
    if type(user.get("is_private")) is not bool:
        raise SourceContractError("缺少明確的帳號隱私狀態")
    avatar = user.get("profile_pic_url_downloadable") or user.get("profile_pic_url")
    snapshot = ProfileSnapshot(
        username=username, display_name=user.get("full_name"), posts=user["media_count"],
        followers=user["follower_count"], following=user["following_count"],
        bio=normalize_text(user.get("biography")),
        privacy=PrivacyState.PRIVATE if user["is_private"] else PrivacyState.PUBLIC,
        avatar_url=_url(avatar), observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    return snapshot, profile_id


def _owner(owner: Any, username: str, profile_id: str) -> tuple[str | None, str]:
    if not isinstance(owner, dict):
        raise SourceContractError("缺少媒體作者證據")
    owner_name = _identifier(owner.get("username"), "作者 username")
    owner_id = str(owner.get("id") or owner.get("pk") or "") or None
    if owner_name.casefold() != username.casefold() or (owner_id and owner_id != profile_id):
        raise SourceContractError("媒體作者與查詢帳號不符；來源可能含協作貼文，未證實共同作者")
    return owner_id, owner_name


def _timestamp(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SourceContractError("缺少可靠的媒體發佈時間")
    try:
        return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError) as exc:
        raise SourceContractError("媒體發佈時間超出有效範圍") from exc


def _best_resource(resources: Any, width_key="width", height_key="height") -> dict:
    if not isinstance(resources, list) or not resources:
        raise SourceContractError("媒體資源列表缺失")
    valid = [r for r in resources if isinstance(r, dict)
             and type(r.get(width_key)) is int and type(r.get(height_key)) is int
             and r[width_key] > 0 and r[height_key] > 0]
    if not valid:
        raise SourceContractError("媒體資源尺寸缺失")
    return max(valid, key=lambda r: r[width_key] * r[height_key])


def _caption(node: dict) -> str | None:
    edges = node.get("edge_media_to_caption", {}).get("edges", [])
    if not isinstance(edges, list):
        raise SourceContractError("貼文文案結構異常")
    text = edges[0].get("node", {}).get("text") if edges else None
    return normalize_text(text) if isinstance(text, str) else None


def parse_post(node: Any, username: str, profile_id: str, category="posts") -> list[MediaCandidate]:
    if not isinstance(node, dict):
        raise SourceContractError("貼文節點不是物件")
    owner_id, owner_name = _owner(node.get("owner"), username, profile_id)
    parent_id = _identifier(node.get("id"), "父貼文 ID")
    published = _timestamp(node.get("taken_at_timestamp"))
    caption = _caption(node)
    if node.get("__typename") == "GraphSidecar":
        child_edges = node.get("edge_sidecar_to_children", {}).get("edges")
        if not isinstance(child_edges, list) or not child_edges:
            raise SourceContractError("輪播缺少有序子項目")
        children = [edge.get("node") if isinstance(edge, dict) else None for edge in child_edges]
    else:
        children = [node]
    output = []
    child_ids: set[str] = set()
    for position, child in enumerate(children):
        if not isinstance(child, dict):
            raise SourceContractError("輪播子項目格式異常")
        media_id = _identifier(child.get("id"), "子媒體 ID")
        if media_id in child_ids:
            raise SourceContractError("輪播子媒體 ID 重複")
        child_ids.add(media_id)
        if child.get("owner"):
            _owner(child["owner"], username, profile_id)
        if type(child.get("is_video")) is not bool:
            raise SourceContractError("子媒體類型缺失")
        kind = "video" if child["is_video"] else "image"
        if kind == "video":
            url = _url(child.get("video_url_downloadable") or child.get("video_url"))
            dimensions = child.get("dimensions") or {}
            width, height = dimensions.get("width"), dimensions.get("height")
        else:
            resource = _best_resource(child.get("display_resources"), "config_width", "config_height")
            url = _url(resource.get("url_downloadable") or resource.get("src"))
            width, height = resource["config_width"], resource["config_height"]
        if type(width) is not int or type(height) is not int or min(width, height) <= 0:
            raise SourceContractError("媒體尺寸無效")
        output.append(MediaCandidate(
            media_key=stable_key("anonyig", profile_id, media_id, kind), category=category,
            kind=kind, url=url, logical_id=parent_id, position=position, published_at=published,
            width=width, height=height, source_rank=100, source="anonyig", source_media_id=media_id,
            parent_id=parent_id, caption=caption, owner_id=owner_id, owner_username=owner_name,
        ))
    return output


def parse_story(item: Any, username: str, profile_id: str, *, position=0, album: dict | None = None) -> MediaCandidate:
    if not isinstance(item, dict):
        raise SourceContractError("限時動態節點不是物件")
    owner_id, owner_name = _owner(item.get("user"), username, profile_id)
    media_id = _identifier(item.get("pk"), "限時動態 ID")
    category = "highlights" if album is not None else "stories"
    album_id = _identifier(album.get("id"), "精選專輯 ID") if album is not None else None
    if album is not None:
        _owner(album.get("user"), username, profile_id)
    videos = item.get("video_versions")
    if videos:
        resource = _best_resource(videos)
        kind = "video"
    else:
        resource = _best_resource(item.get("image_versions2", {}).get("candidates"))
        kind = "image"
    return MediaCandidate(
        media_key=stable_key("anonyig", profile_id, media_id, kind), category=category, kind=kind,
        url=_url(resource.get("url_downloadable") or resource.get("url")), logical_id=media_id,
        position=position, published_at=_timestamp(item.get("taken_at")), width=resource["width"],
        height=resource["height"], source_rank=100, source="anonyig", source_media_id=media_id,
        parent_id=media_id, album_id=album_id, album_title=album.get("title") if album else None,
        owner_id=owner_id, owner_username=owner_name,
    )


def parse_posts_page(payload: Any) -> tuple[list[dict], str | None]:
    result = _body(payload)
    if not isinstance(result, dict) or not isinstance(result.get("edges"), list):
        raise SourceContractError("Posts 分頁契約異常")
    page_info = result.get("page_info")
    if not isinstance(page_info, dict) or type(page_info.get("has_next_page")) is not bool:
        raise SourceContractError("缺少明確的 Posts 分頁終態")
    cursor = _identifier(page_info.get("end_cursor"), "下一頁游標") if page_info["has_next_page"] else None
    nodes = [edge.get("node") if isinstance(edge, dict) else None for edge in result["edges"]]
    if not all(isinstance(node, dict) for node in nodes):
        raise SourceContractError("Posts 分頁包含異常節點")
    if cursor and not nodes:
        raise SourceContractError("空頁仍宣稱有下一頁，不能確認收集完成")
    return nodes, cursor


def _cursor(value: str | None) -> dict:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or decoded.get("v") != 1:
            raise ValueError
        return decoded
    except (TypeError, ValueError) as exc:
        raise SourceContractError("來源續抓狀態無效；保留既有資料") from exc


def _encode_cursor(**state: Any) -> str:
    return json.dumps({"v": 1, **state}, separators=(",", ":"), sort_keys=True)


@dataclass
class _Response:
    endpoint: str
    status: int
    payload: Any


class _Session:
    def __init__(self, page, timeout: float, guard=None):
        self.page = page
        self.timeout = timeout
        self.responses: list[_Response] = []
        self.tasks: set[asyncio.Task] = set()
        self.blocked = False
        self.block_diagnostic: dict[str, Any] | None = None
        self._block_body_task: asyncio.Task | None = None
        self._block_body_deadline = 0.0
        self._block_body_frozen = False
        self.guard = guard
        page.on("response", self._schedule)

    def _record_block(self, endpoint: str, status: int | None, block_type: str) -> bool:
        self.blocked = True
        if self.block_diagnostic is None:
            # Record the first observable signal synchronously: response bodies
            # may be delayed or unreadable. No URL or body enters the diagnostic.
            self.block_diagnostic = {
                "source": "anonyig",
                "endpoint": endpoint if endpoint in _DIAGNOSTIC_ENDPOINTS else "unknown_api",
                "http_status": status,
                "block_type": block_type,
                "observed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "verification_required": True if status in {422, 429} or block_type == "visible_challenge" else None,
                "error_type": _HTTP_ERROR_TYPES.get(status, "captcha_required" if block_type == "visible_challenge" else "unclassified"),
                "classification_basis": ("frontend_http_status" if status in _HTTP_BLOCK_TYPES else
                                         "visible_widget" if block_type == "visible_challenge" else "none"),
                "body_state": "unreadable" if status in _HTTP_BLOCK_TYPES else "not_applicable",
            }
            self._block_body_frozen = status not in _HTTP_BLOCK_TYPES
            self._block_body_deadline = time.monotonic() + _BLOCK_BODY_TIMEOUT_SECONDS
            return True
        return False

    def _classify_block_body(self, payload: Any, body_state: str) -> None:
        if self._block_body_frozen or self.block_diagnostic is None:
            return
        self._block_body_frozen = True
        detail = self.block_diagnostic
        if time.monotonic() > self._block_body_deadline:
            detail["body_state"] = "timeout"
            return
        detail["body_state"] = body_state
        challenge = payload.get("challenge") if isinstance(payload, dict) else None
        # Match extractTurnstileChallenge's exact observed body shape. Never copy
        # siteKey, free-form error messages, or any unknown body values into state.
        if (detail["http_status"] in {422, 429} and isinstance(challenge, dict)
                and challenge.get("type") == "turnstile"
                and isinstance(challenge.get("siteKey"), str) and challenge["siteKey"]):
            detail.update(verification_required=True, error_type="turnstile_required",
                          classification_basis="response_challenge")

    async def _finish_block_diagnostic(self) -> None:
        task = self._block_body_task
        if self._block_body_frozen or task is None:
            return
        remaining = self._block_body_deadline - time.monotonic()
        if not task.done() and remaining > 0:
            # Wait only for an already received response, never replay a request.
            # asyncio.wait preserves caller cancellation and does not cancel the
            # capture task implicitly when a caller is cancelled.
            await asyncio.wait({task}, timeout=remaining)
        if not self._block_body_frozen:
            self._block_body_frozen = True
            self.block_diagnostic["body_state"] = "unreadable" if task.done() else "timeout"
            if not task.done():
                task.cancel()

    def _blocked_error(self, message: str) -> SourceBlocked:
        if self.block_diagnostic is None:
            self._record_block("none", None, "unknown_block")
        detail = json.dumps(self.block_diagnostic, ensure_ascii=False, separators=(",", ":"))
        return SourceBlocked(f"{message} [ANONYIG-DIAG] {detail}")

    def _schedule(self, response):
        parsed = urlsplit(response.url)
        host = parsed.hostname or ""
        if (not (host == "anonyig.com" or host.endswith(".anonyig.com"))
                or not parsed.path.startswith("/api/v1/instagram/")):
            return
        first_block = False
        if response.status in _HTTP_BLOCK_TYPES:
            endpoint = parsed.path.removeprefix("/api/v1/instagram/")
            first_block = self._record_block(endpoint if endpoint in _API_ENDPOINTS else "unknown_api", response.status,
                                             _HTTP_BLOCK_TYPES[response.status])
        task = asyncio.create_task(self._capture(response, first_block=first_block))
        if first_block:
            self._block_body_task = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _capture(self, response, *, first_block=False):
        blocked = response.status in _HTTP_BLOCK_TYPES
        payload, body_state = None, "unreadable"
        if not blocked or first_block:
            try:
                payload = await response.json()
                body_state = "json"
            except Exception:
                pass
        if first_block:
            self._classify_block_body(payload, body_state)
        endpoint = urlsplit(response.url).path.rsplit("/", 1)[-1]
        if blocked:
            payload = None
            endpoint = endpoint if endpoint in _API_ENDPOINTS else "unknown_api"
        self.responses.append(_Response(endpoint, response.status, payload))

    async def check_blocked(self):
        if self.guard is not None and self.guard():
            self._record_block("none", None, "source_cooldown")
            await self._finish_block_diagnostic()
            raise self._blocked_error("AnonyIG 來源全域冷卻中；本輪不再發送請求")
        if self.blocked:
            await self._finish_block_diagnostic()
            raise self._blocked_error("AnonyIG 驗證／限流／拒絕存取；本輪停止所有來源請求")
        # Detect only visible challenge widgets/messages, not marketing FAQ text.
        selectors = ['iframe[src*="challenges.cloudflare.com"]', 'iframe[src*="recaptcha"][title*="challenge"]']
        for selector in selectors:
            locator = self.page.locator(selector)
            if await locator.count() and await locator.first.is_visible():
                self._record_block("page", None, "visible_challenge")
                await self._finish_block_diagnostic()
                raise self._blocked_error("AnonyIG 出現人工驗證；未嘗試完成")
        # A response or another worker's cooldown can arrive while UI checks yield.
        if self.guard is not None and self.guard():
            self._record_block("none", None, "source_cooldown")
            await self._finish_block_diagnostic()
            raise self._blocked_error("AnonyIG 來源全域冷卻中；本輪不再發送請求")
        if self.blocked:
            await self._finish_block_diagnostic()
            raise self._blocked_error("AnonyIG 驗證／限流／拒絕存取；本輪停止所有來源請求")

    async def wait(self, endpoints: set[str], after=0) -> _Response:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            await self.check_blocked()
            for response in self.responses[after:]:
                if response.endpoint in endpoints:
                    if response.status != 200:
                        raise SourceContractError(f"AnonyIG {response.endpoint} HTTP {response.status}")
                    return response
            await asyncio.sleep(0.2)
        raise SourceContractError("等待來源結構化回應逾時，不能視為空集合")

    async def tab(self, name: str, endpoint: str) -> Any:
        await self.check_blocked()
        after = len(self.responses)
        await self.page.get_by_role("button", name=name, exact=True).click()
        return (await self.wait({endpoint}, after)).payload

    async def next_posts(self) -> Any:
        await self.check_blocked()
        after = len(self.responses)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            await self.check_blocked()
            await self.page.locator("footer").scroll_into_view_if_needed()
            await self.check_blocked()
            await asyncio.sleep(0.5)
            await self.check_blocked()
            for response in self.responses[after:]:
                if response.endpoint in {"postsV2", "posts"}:
                    if response.status != 200:
                        raise SourceContractError(f"Posts 分頁 HTTP {response.status}")
                    return response.payload
            # Lazy rendering appends a local batch before requesting another page.
            await self.page.mouse.wheel(0, -400)
            await self.check_blocked()
            await self.page.mouse.wheel(0, 800)
        raise SourceContractError("Posts 分頁未產生新回應，保留續抓狀態")

    async def close(self):
        self.page.remove_listener("response", self._schedule)
        if self.tasks:
            done, pending = await asyncio.wait(self.tasks, timeout=3)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        await self.page.close()


class AnonyIGScraper(ProfileScraper):
    supports_progress = True
    media_referer = "https://anonyig.com/"

    async def _open(self, url: str) -> tuple[_Session, ProfileSnapshot, str]:
        if not self._context:
            raise RuntimeError("scraper 尚未啟動")
        username = username_from_url(url)
        page = await self._context.new_page()
        page.set_default_timeout(min(15000, self.config.timeout_seconds * 1000))
        session = _Session(page, self.config.timeout_seconds, getattr(self, "source_guard", None))
        try:
            await session.check_blocked()
            await page.goto("https://anonyig.com/en/", wait_until="domcontentloaded", timeout=self.config.timeout_seconds * 1000)
            await session.check_blocked()
            search = page.get_by_placeholder("@username or link", exact=True)
            await search.fill(username)
            await search.press("Enter")
            response = await session.wait({"userInfo"})
            snapshot, profile_id = parse_profile(response.payload, username)
            return session, snapshot, profile_id
        except Exception as exc:
            await session.close()
            if isinstance(exc, ScrapeFailure):
                raise
            raise ScrapeFailure(str(exc), "AnonyIG 個人檔案", blocker="CAPTCHA/網站限流" if isinstance(exc, SourceBlocked) else None) from exc

    async def scrape_profile_only(self, url: str) -> ProfileSnapshot:
        self.last_profile_id = None
        session, snapshot, profile_id = await self._open(url)
        self.last_profile_id = profile_id
        await session.close()
        return snapshot

    async def scrape(self, url: str, *, cursors=None, known_ids=None,
                     completed_categories: set[str] | None = None) -> ScrapeResult:
        cursors, known_ids = cursors or {}, known_ids or {}
        session, snapshot, profile_id = await self._open(url)
        media: list[MediaCandidate] = []
        observations: dict[str, CollectionObservation] = {}
        try:
            if snapshot.privacy == PrivacyState.PRIVATE:
                observations = {category: CollectionObservation(TerminalState.PRIVATE, complete=True) for category in CATEGORIES}
            else:
                try:
                    feed_media, feed_states = await self._feed(
                        session, snapshot, profile_id, cursors, known_ids, completed_categories,
                    )
                    media.extend(feed_media)
                    observations.update(feed_states)
                except SourceBlocked as exc:
                    observations.update({category: CollectionObservation(TerminalState.BLOCKED, str(exc), cursors.get(category)) for category in ("posts", "reels")})
                except Exception as exc:
                    observations.update({category: CollectionObservation(TerminalState.FAILED, str(exc), cursors.get(category)) for category in ("posts", "reels")})
                for category in ("stories", "highlights"):
                    try:
                        await session.check_blocked()
                        if category == "stories":
                            items, observation = await self._stories(session, snapshot, profile_id)
                        else:
                            items, observation = await self._highlights(session, snapshot, profile_id, cursors.get(category))
                        media.extend(items)
                        observations[category] = observation
                    except SourceBlocked as exc:
                        observations[category] = CollectionObservation(TerminalState.BLOCKED, str(exc), cursors.get(category))
                    except Exception as exc:
                        observations[category] = CollectionObservation(TerminalState.FAILED, str(exc), cursors.get(category))
            return ScrapeResult(snapshot, media, observations["posts"].state, observations["stories"].state,
                                collections=observations, source="anonyig", profile_id=profile_id)
        finally:
            await session.close()

    async def _feed(self, session, snapshot, profile_id, cursors, known_ids, completed_categories=None):
        completed_categories = completed_categories or set()
        response = await session.wait({"postsV2", "posts"})
        first_nodes, next_cursor = parse_posts_page(response.payload)
        records: dict[str, dict] = {}
        page_count = 1
        page_cursors: set[str] = set()
        feed_error = None
        blocked = False
        nodes = first_nodes
        # Posts and the site's Reels view share the same feed. Fetch a bounded
        # window once, retain all child media, and never stop at a pinned known ID.
        while True:
            for node in nodes:
                records[_identifier(node.get("id"), "貼文 ID")] = node
            if not next_cursor or page_count >= self.config.max_pages_per_collection:
                break
            if next_cursor in page_cursors:
                feed_error = "來源重複分頁游標，停止避免無限循環"
                break
            page_cursors.add(next_cursor)
            try:
                payload = await session.next_posts()
                nodes, next_cursor = parse_posts_page(payload)
                page_count += 1
            except SourceBlocked as exc:
                blocked, feed_error = True, str(exc)
                break
            except Exception as exc:
                feed_error = str(exc)
                break
        media, states = [], {}
        ordered = sorted(records.values(), key=lambda node: node.get("taken_at_timestamp") or 0, reverse=True)
        for category, limit in (("posts", self.config.initial_posts), ("reels", self.config.initial_reels)):
            state = _cursor(cursors.get(category))
            prior_selected = set(state.get("selected", []))
            known = set(known_ids.get(category, set()))
            selected = set(prior_selected)
            failures = []
            category_media = []
            category_nodes = [node for node in ordered if category == "posts" or node.get("is_video") is True]
            # Known logical IDs are still useful observations: signed media URLs
            # can expire while a durable row is pending/failed. Refresh candidates
            # in the current window without changing the incremental boundary or
            # counting those posts again toward the fixed initial scope.
            for node in category_nodes:
                if str(node.get("id")) not in known | prior_selected:
                    continue
                try:
                    category_media.extend(parse_post(node, snapshot.username, profile_id, category))
                except SourceContractError as exc:
                    failures.append(str(exc))
            seen_known = bool(known.intersection(str(node.get("id")) for node in category_nodes))
            baseline = category not in completed_categories and not known
            if state.get("mode") == "incremental":
                baseline = False
            scope = list(state.get("scope", [])) if baseline else []
            if baseline:
                for node in category_nodes:
                    node_id = str(node.get("id"))
                    if node_id not in scope and len(scope) < limit:
                        scope.append(node_id)
                candidates_by_id = {str(node.get("id")): node for node in category_nodes}
                if set(scope) - candidates_by_id.keys() - prior_selected:
                    failures.append("首次收集範圍內的未完成貼文不在本輪視窗，不能確認完成")
                category_nodes = [candidates_by_id[node_id] for node_id in scope if node_id in candidates_by_id]
            elif seen_known:
                boundary = max(node["taken_at_timestamp"] for node in category_nodes if str(node.get("id")) in known)
                category_nodes = [node for node in category_nodes if node.get("taken_at_timestamp", 0) >= boundary]
            for node in category_nodes:
                node_id = str(node.get("id"))
                if node_id in known or node_id in prior_selected:
                    continue
                try:
                    candidates = parse_post(node, snapshot.username, profile_id, category)
                except SourceContractError as exc:
                    failures.append(str(exc))
                    continue
                selected.add(node_id)
                category_media.extend(candidates)
            media.extend(category_media)
            coverage = ((baseline and len(selected) >= limit) or (not baseline and seen_known)
                        or (next_cursor is None and (not baseline or set(scope) <= selected)))
            error_parts = list(dict.fromkeys(failures + ([feed_error] if feed_error else [])))
            if category == "reels":
                error_parts.append(REELS_LIMITATION)
            if next_cursor and not coverage:
                error_parts.append("已達本輪分頁預算；普通網頁續抓須重播前頁，尚未驗證直接游標恢復")
            complete = coverage and not error_parts and not blocked
            cursor = None if complete else _encode_cursor(
                mode="baseline" if baseline else "incremental", selected=sorted(selected),
                scope=scope, end_cursor=next_cursor, pages=page_count,
            )
            terminal = (TerminalState.BLOCKED if blocked else TerminalState.PARTIAL if not complete
                        else TerminalState.MEDIA if category_media or known or selected else TerminalState.EMPTY)
            states[category] = CollectionObservation(terminal, "; ".join(error_parts) or None, cursor, complete)
        return media, states

    async def _stories(self, session, snapshot, profile_id):
        result = _body(await session.tab("stories", "stories"))
        if not isinstance(result, list):
            raise SourceContractError("Stories 回應不是明確列表")
        media, errors = [], []
        for position, item in enumerate(result):
            try:
                media.append(parse_story(item, snapshot.username, profile_id, position=position))
            except SourceContractError as exc:
                errors.append(str(exc))
        complete = not errors
        state = TerminalState.PARTIAL if errors else TerminalState.MEDIA if media else TerminalState.EMPTY
        return media, CollectionObservation(state, "; ".join(dict.fromkeys(errors)) or None, complete=complete)

    async def _highlights(self, session, snapshot, profile_id, cursor):
        state = _cursor(cursor)
        result = _body(await session.tab("highlights", "highlights"))
        if not isinstance(result, list):
            raise SourceContractError("Highlights 回應不是明確專輯列表")
        previous_completed = set(state.get("completed_albums", []))
        completed = set(previous_completed)
        media, errors = [], []
        attempted, blocked = 0, False
        observed_ids = set()
        for index, album in enumerate(result):
            try:
                if not isinstance(album, dict):
                    raise SourceContractError("精選專輯格式異常")
                album_id = _identifier(album.get("id"), "精選專輯 ID")
                observed_ids.add(album_id)
                _owner(album.get("user"), snapshot.username, profile_id)
                if not isinstance(album.get("title"), str) or not album["title"].strip():
                    raise SourceContractError("精選專輯名稱缺失，不能猜測標籤")
                if album_id in previous_completed:
                    continue
                if attempted >= self.config.max_pages_per_collection:
                    continue
                attempted += 1
                await session.check_blocked()
                before = len(session.responses)
                if sum(1 for entry in result if isinstance(entry, dict) and entry.get("title") == album["title"]) != 1:
                    raise SourceContractError("精選專輯名稱重複，未取得可核對 album ID 的唯一點擊目標")
                target = session.page.get_by_text(album["title"], exact=True)
                if await target.count() != 1:
                    raise SourceContractError("精選專輯沒有唯一可核對的頁面目標")
                await target.click()
                payload = (await session.wait({"highlightStories"}, before)).payload
                stories = _body(payload)
                if not isinstance(stories, list):
                    raise SourceContractError("Highlight 專輯內容不是明確列表")
                album_media = [parse_story(item, snapshot.username, profile_id, position=position, album=album)
                               for position, item in enumerate(stories)]
                media.extend(album_media)
                completed.add(album_id)
            except SourceBlocked as exc:
                blocked = True
                errors.append(str(exc))
                break
            except Exception as exc:
                errors.append(str(exc))
        pending = observed_ids - completed
        complete = not errors and not pending
        new_cursor = None if complete else _encode_cursor(completed_albums=sorted(completed))
        terminal = (TerminalState.BLOCKED if blocked else TerminalState.PARTIAL if not complete
                    else TerminalState.MEDIA if result else TerminalState.EMPTY)
        return media, CollectionObservation(terminal, "; ".join(dict.fromkeys(errors)) or None, new_cursor, complete)
