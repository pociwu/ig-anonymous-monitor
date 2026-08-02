from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from .config import BrowserConfig
from .models import MediaCandidate, PrivacyState, ProfileSnapshot, ScrapeFailure, ScrapeResult, TerminalState
from .utils import normalize_text, stable_key


MEDIA_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
LOG = logging.getLogger("ig_monitor.scraper")


class ProfileScraper:
    def __init__(self, config: BrowserConfig):
        self.config = config
        self._playwright = None
        self._browser = None
        self._context = None

    async def __aenter__(self) -> "ProfileScraper":
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(self.config.browsers_path))
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.config.headless)
        self._context = await self._browser.new_context(
            locale="en-US",
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 1000},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def download(self, url: str, referer: str) -> tuple[bytes, str | None]:
        if not self._context:
            raise RuntimeError("scraper 尚未啟動")
        response = await self._context.request.get(url, headers={"Referer": referer}, timeout=60_000)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status}: {url[:160]}")
        return await response.body(), response.headers.get("content-type")

    async def scrape(self, url: str) -> ScrapeResult:
        last: ScrapeFailure | None = None
        for attempt in range(self.config.retry_count + 1):
            try:
                return await self._scrape_once(url)
            except ScrapeFailure as exc:
                last = exc
                if attempt < self.config.retry_count:
                    await asyncio.sleep(2)
        assert last is not None
        raise last

    async def scrape_profile_only(self, url: str) -> ProfileSnapshot:
        """Read profile metadata without activating or expanding any media tab."""
        if not self._context:
            raise RuntimeError("scraper is not started")
        page = await self._context.new_page()
        page.set_default_timeout(min(10_000, self.config.timeout_seconds * 1000))
        deadline = time.monotonic() + self.config.timeout_seconds
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.config.timeout_seconds * 1000)
            await page.locator(".profile__nickname").wait_for(
                state="visible", timeout=self._remaining_ms(deadline)
            )
            await page.locator(".profile__stats").wait_for(
                state="visible", timeout=self._remaining_ms(deadline)
            )
            await page.wait_for_function(
                """() => { const i=document.querySelector('.profile__avatar-pic');
                return !!i && i.complete && i.naturalWidth > 0; }""",
                timeout=self._remaining_ms(deadline),
            )
            profile = await self._profile_data(page)
            self._validate_profile(profile)
            private = await page.get_by_text("This account is private", exact=False).count() > 0
            return ProfileSnapshot(
                username=profile["username"], display_name=profile.get("display_name"),
                posts=profile["posts"], followers=profile["followers"],
                following=profile["following"], bio=normalize_text(profile.get("bio")),
                privacy=PrivacyState.PRIVATE if private else PrivacyState.PUBLIC,
                avatar_url=profile["avatar_url"],
                observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        finally:
            await page.close()

    async def _scrape_once(self, url: str) -> ScrapeResult:
        if not self._context:
            raise RuntimeError("scraper 尚未啟動")
        page = await self._context.new_page()
        page.set_default_timeout(min(10_000, self.config.timeout_seconds * 1000))
        deadline = time.monotonic() + self.config.timeout_seconds
        captured: list[dict[str, Any]] = []
        capture_tasks: set[asyncio.Task] = set()
        active_category = {"value": None}
        request_categories: dict[int, str] = {}

        def on_request(request) -> None:
            if active_category["value"] and request.resource_type in {"xhr", "fetch"}:
                request_categories[id(request)] = active_category["value"]

        def on_response(response) -> None:
            category = request_categories.pop(id(response.request), None)
            if category:
                task = asyncio.create_task(self._capture_json(response, category, captured))
                capture_tasks.add(task)
                task.add_done_callback(capture_tasks.discard)

        page.on("request", on_request)
        page.on("response", on_response)
        stage = "開啟頁面"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.config.timeout_seconds * 1000)
            stage = "等待個人資料"
            LOG.info("%s：%s", url, stage)
            await page.locator(".profile__nickname").wait_for(state="visible", timeout=self._remaining_ms(deadline))
            await page.locator(".profile__stats").wait_for(state="visible", timeout=self._remaining_ms(deadline))
            await page.wait_for_function(
                """() => { const i=document.querySelector('.profile__avatar-pic');
                return !!i && i.complete && i.naturalWidth > 0; }""",
                timeout=self._remaining_ms(deadline),
            )
            profile = await self._profile_data(page)
            self._validate_profile(profile)

            stage = "等待 Publications"
            LOG.info("%s：%s", url, stage)
            active_category["value"] = "posts"
            await self._activate_tab(page, "profile__tabs-posts")
            posts_state = await self._wait_terminal(page, "posts", deadline)
            if posts_state == TerminalState.UNKNOWN:
                raise await self._failure(page, stage, "Publications 未在期限內載入完成")
            if posts_state == TerminalState.MEDIA:
                await self._expand_media(page, "posts", deadline)

            stage = "等待 Stories"
            LOG.info("%s：%s", url, stage)
            active_category["value"] = "stories"
            await self._activate_tab(page, "profile__tabs-stories")
            stories_state = await self._wait_terminal(page, "stories", deadline)
            if stories_state == TerminalState.UNKNOWN:
                raise await self._failure(page, stage, "Stories 未在期限內載入完成")
            if stories_state == TerminalState.MEDIA:
                await self._expand_media(page, "stories", deadline)

            active_category["value"] = None
            if capture_tasks:
                await self._drain_tasks(capture_tasks)
            media = await self._dom_media(page, "posts") + await self._dom_media(page, "stories")
            media.extend(self._json_media(captured, url))
            media = self._best_candidates(media)

            if TerminalState.PRIVATE in {posts_state, stories_state}:
                privacy = PrivacyState.PRIVATE
            elif TerminalState.UNKNOWN not in {posts_state, stories_state}:
                privacy = PrivacyState.PUBLIC
            else:
                raise await self._failure(page, "終止狀態", "頁面未進入私人、公開媒體或無內容狀態")

            snapshot = ProfileSnapshot(
                username=profile["username"], display_name=profile.get("display_name"),
                posts=profile["posts"], followers=profile["followers"], following=profile["following"],
                bio=normalize_text(profile.get("bio")), privacy=privacy, avatar_url=profile["avatar_url"],
                observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
            return ScrapeResult(snapshot, media, posts_state, stories_state)
        except ScrapeFailure:
            raise
        except Exception as exc:
            raise await self._failure(page, stage, str(exc)) from exc
        finally:
            active_category["value"] = None
            if capture_tasks:
                await self._drain_tasks(capture_tasks)
            await page.close()

    @staticmethod
    async def _drain_tasks(tasks: set[asyncio.Task]) -> None:
        if not tasks:
            return
        done, pending = await asyncio.wait(list(tasks), timeout=5)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            try:
                task.result()
            except Exception:
                pass

    def _remaining_ms(self, deadline: float) -> int:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise TimeoutError("45 秒載入期限已到")
        return remaining

    async def _profile_data(self, page) -> dict[str, Any]:
        raw = await page.evaluate("""() => {
          const text = s => document.querySelector(s)?.innerText ?? '';
          const nick = document.querySelector('.profile__nickname');
          const username = nick ? Array.from(nick.childNodes)
            .filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim() : '';
          const displaySelectors = ['.profile__full-name','.profile__name','.profile__display-name'];
          let displayName = null;
          for (const s of displaySelectors) { const v=text(s).trim(); if(v){displayName=v;break;} }
          const avatar=document.querySelector('.profile__avatar-pic');
          return {username,display_name:displayName,
            posts:text('.profile__stats-posts'),followers:text('.profile__stats-followers'),
            following:text('.profile__stats-follows'),bio:text('.profile__description'),
            avatar_url:avatar?.currentSrc || avatar?.src || '', avatar_ok:!!avatar && avatar.complete && avatar.naturalWidth>0};
        }""")
        for key in ("posts", "followers", "following"):
            raw[key] = self._parse_count(raw[key])
        return raw

    @staticmethod
    def _parse_count(value: str) -> int:
        clean = value.strip().replace(",", "").replace(" ", "")
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMB])?", clean, re.I)
        if not match:
            raise ValueError(f"無法解析數字：{value!r}")
        number = float(match.group(1))
        scale = {None: 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2).upper() if match.group(2) else None]
        return int(number * scale)

    @staticmethod
    def _validate_profile(profile: dict[str, Any]) -> None:
        if not profile.get("username") or not profile.get("avatar_url") or not profile.get("avatar_ok"):
            raise ValueError("必要個人資料或大頭貼未完整載入")
        for key in ("posts", "followers", "following"):
            if not isinstance(profile.get(key), int) or profile[key] < 0:
                raise ValueError(f"{key} 未完整載入")

    async def _activate_tab(self, page, tab_name: str) -> None:
        selector = f".profile__tabs-item[data-tab='{tab_name}'] .profile__tabs-item-link"
        locator = page.locator(selector)
        if await locator.count() != 1:
            raise ValueError(f"找不到唯一頁籤：{tab_name}")
        await locator.click()

    async def _wait_terminal(self, page, category: str, deadline: float) -> TerminalState:
        previous = TerminalState.UNKNOWN
        stable = 0
        while time.monotonic() < deadline:
            state = TerminalState(await page.evaluate("""category => {
              const prefix=category==='posts'?'posts':'stories';
              const visible=e=>!!e && getComputedStyle(e).display!=='none' && getComputedStyle(e).visibility!=='hidden';
              const priv=document.querySelector(`.profile__${prefix}-is-private`);
              const empty=document.querySelector(`.profile__${prefix}-no-content`);
              const tab=category==='posts'?'profile__tabs-posts':'profile__tabs-stories';
              const media=document.querySelector(`.profile__tabs-media-outer[data-tab='${tab}']`);
              if(visible(priv) && /This account is private/i.test(priv.innerText)) return 'private';
              if(visible(empty)) return 'empty';
              if(visible(media) && media.children.length>0) return 'media';
              return 'unknown';
            }""", category))
            if state != TerminalState.UNKNOWN and state == previous:
                stable += 1
                if stable >= 2:
                    return state
            else:
                stable = 1 if state != TerminalState.UNKNOWN else 0
                previous = state
            await page.wait_for_timeout(2000)
        return TerminalState.UNKNOWN

    async def _expand_media(self, page, category: str, deadline: float) -> None:
        tab = "profile__tabs-posts" if category == "posts" else "profile__tabs-stories"
        selector = f".profile__tabs-media-outer[data-tab='{tab}']"
        previous = -1
        stable = 0
        for _ in range(8):
            if time.monotonic() >= deadline:
                return
            count = await page.locator(f"{selector} .profile__tabs-media-item").count()
            if count == previous:
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
                previous = count
            more = page.get_by_text("Load more", exact=True)
            if await more.count() == 1 and await more.is_visible():
                await more.click()
            else:
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

    async def _dom_media(self, page, category: str) -> list[MediaCandidate]:
        tab = "profile__tabs-posts" if category == "posts" else "profile__tabs-stories"
        items = await page.evaluate("""tab => {
          const root=document.querySelector(`.profile__tabs-media-outer[data-tab='${tab}']`);
          if(!root) return [];
          const out=[];
          for(const el of root.querySelectorAll('*')){
            const attrs={}; for(const a of el.attributes||[]) attrs[a.name]=a.value;
            const near=el.closest('[data-id],[data-pk],[data-shortcode],[data-media-id]');
            const card=el.closest('.profile__tabs-media-item');
            out.push({tag:el.tagName.toLowerCase(),attrs,html:el.outerHTML.slice(0,800),
              card_has_content:!!card?.querySelector('[data-content]'),
              logical_id:near?.dataset?.id||near?.dataset?.pk||near?.dataset?.shortcode||near?.dataset?.mediaId||null});
          }
          return out.slice(0,5000);
        }""", tab)
        result: list[MediaCandidate] = []
        position = 0
        for item in items:
            values: list[tuple[str, int]] = []
            for name, value in item["attrs"].items():
                low = name.lower()
                if item.get("card_has_content") and "data-content" not in item["attrs"] and item["tag"] not in {"video", "source"}:
                    continue
                if low == "srcset":
                    for part in value.split(","):
                        bits = part.strip().split()
                        if bits and bits[0].startswith("http"):
                            rank = int(re.sub(r"\D", "", bits[1])) if len(bits) > 1 and re.sub(r"\D", "", bits[1]) else 20
                            values.append((bits[0], rank))
                elif (low in {"src", "href", "poster", "data-src", "data-url", "data-download", "data-original", "data-content"}
                      or "url" in low or "video" in low or "image" in low):
                    rank = 100 if low == "data-content" or "download" in low or "original" in low else 40
                    values.extend((u, rank) for u in MEDIA_URL_RE.findall(value))
            if not item.get("card_has_content") or "data-content" in item["attrs"]:
                values.extend((u, 30) for u in MEDIA_URL_RE.findall(item.get("html", "")))
            if values:
                position += 1
            for media_url, rank in values:
                media_url = urljoin(page.url, media_url.replace("&amp;", "&"))
                if self._is_site_asset(media_url):
                    continue
                declared_type = str(item["attrs"].get("data-media-type", "")).lower()
                kind = "video" if declared_type == "video" or item["tag"] in {"video", "source"} or re.search(r"\.(mp4|webm|mov)(?:\?|$)", media_url, re.I) else "image"
                logical = item.get("logical_id")
                slide = item["attrs"].get("data-slide")
                actual_position = int(slide) if str(slide).isdigit() else position
                key = stable_key(category, logical or media_url, actual_position if logical else 0, kind)
                result.append(MediaCandidate(key, category, kind, media_url, logical, actual_position, source_rank=rank))
        return result

    async def _capture_json(self, response, category: str, captured: list[dict[str, Any]]) -> None:
        try:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type and response.request.resource_type not in {"xhr", "fetch"}:
                return
            try:
                data = await response.json()
            except Exception:
                data = await response.text()
                if not data:
                    return
            captured.append({"category": category, "data": data})
        except Exception:
            return

    def _json_media(self, payloads: list[dict[str, Any]], base_url: str) -> list[MediaCandidate]:
        result: list[MediaCandidate] = []
        position = 0

        def walk(value: Any, category: str, parent: dict[str, Any] | None = None, key_name: str = "") -> None:
            nonlocal position
            if isinstance(value, dict):
                for key, child in value.items():
                    child_category = "highlights" if "highlight" in str(key).lower() else category
                    walk(child, child_category, value, str(key))
            elif isinstance(value, list):
                for child in value:
                    walk(child, category, parent, key_name)
            elif isinstance(value, str):
                normalized = value.replace("\\/", "/").replace("&amp;", "&")
                urls = MEDIA_URL_RE.findall(normalized)
                if not urls:
                    return
                key_lower = key_name.lower()
                if "profile_pic" in key_lower or "avatar" in key_lower:
                    return
                logical = next((str(parent[k]) for k in ("shortcode", "pk", "id", "media_id")
                                if parent and parent.get(k) is not None), None)
                published = next((str(parent[k]) for k in ("taken_at", "timestamp", "date", "created_at")
                                  if parent and parent.get(k)), None)
                explicit_position = next((int(parent[k]) for k in ("position", "index", "carousel_index")
                                          if parent and str(parent.get(k, "")).isdigit()), 0)
                for found in urls:
                    if self._is_site_asset(found):
                        continue
                    looks_media = bool(re.search(r"url|src|image|video|media|display|download|resource|html", key_lower))
                    looks_media = looks_media or bool(re.search(r"\.(jpe?g|png|webp|mp4|webm|mov)(?:\?|$)", found, re.I))
                    looks_media = looks_media or "cdn." in found.lower()
                    if not looks_media:
                        continue
                    position += 1
                    pos = explicit_position or position
                    kind = "video" if "video" in key_lower or "<video" in normalized.lower() or re.search(r"\.(mp4|webm|mov)(?:\?|$)", found, re.I) else "image"
                    rank = 100 if any(x in key_lower for x in ("original", "download", "resource")) else 60
                    result.append(MediaCandidate(stable_key(category, logical or found, pos if logical else 0, kind),
                                                 category, kind, urljoin(base_url, found), logical, pos,
                                                 published_at=published, source_rank=rank))

        for payload in payloads:
            walk(payload["data"], payload["category"])
        return result

    @staticmethod
    def _is_site_asset(url: str) -> bool:
        low = url.lower()
        return any(part in low for part in ("/static/", "googleads", "doubleclick", "googletagmanager",
                                             "yandex", "cloudflareinsights", "preload.gif", "faq_",
                                             "w3.org/2000/svg"))

    @staticmethod
    def _best_candidates(items: list[MediaCandidate]) -> list[MediaCandidate]:
        high_quality_categories = {item.category for item in items if item.source_rank >= 100}
        items = [item for item in items if item.category not in high_quality_categories or item.source_rank >= 100]
        by_url: dict[str, MediaCandidate] = {}
        for item in items:
            url_key = item.url.rstrip("/ ")
            current = by_url.get(url_key)
            score = item.source_rank + (item.width or 0) + (item.height or 0) + (10 if item.logical_id else 0)
            current_score = (current.source_rank + (current.width or 0) + (current.height or 0)
                             + (10 if current.logical_id else 0)) if current else -1
            if score > current_score:
                by_url[url_key] = item
        best: dict[tuple[str, str, int, str], MediaCandidate] = {}
        for item in by_url.values():
            identity = (item.category, item.logical_id or item.media_key, item.position if item.logical_id else 0, item.kind)
            current = best.get(identity)
            score = item.source_rank + (item.width or 0) + (item.height or 0)
            current_score = current.source_rank + (current.width or 0) + (current.height or 0) if current else -1
            if score > current_score:
                best[identity] = item
        return list(best.values())

    async def _failure(self, page, stage: str, reason: str) -> ScrapeFailure:
        try:
            text = (await page.locator("body").inner_text(timeout=3000))[:20_000]
        except Exception:
            text = ""
        blocker = None
        low = text.lower()
        if "captcha" in low or "verify you are human" in low:
            blocker = "CAPTCHA"
        elif "cloudflare" in low or "access denied" in low or "firewall" in low:
            blocker = "防火牆或 Cloudflare"
        elif "too many requests" in low or "rate limit" in low:
            blocker = "網站限流"
        try:
            html = await page.evaluate("""() => { const d=document.documentElement.cloneNode(true);
              d.querySelectorAll('iframe,ins,.adsbygoogle,[class*=advert],[id*=advert]').forEach(e=>e.remove());
              return '<!doctype html>'+d.outerHTML; }""")
        except Exception:
            html = None
        try:
            screenshot = await page.screenshot(full_page=True, timeout=5000)
        except Exception:
            screenshot = None
        return ScrapeFailure(reason, stage, html, screenshot, blocker)
