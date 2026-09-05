"""Small public-source probe; never loads project config, secrets, or production DB.

The default run uses a fresh headless browser and queries only the public NASA
account. It stops on verification or rate limiting without attempting a bypass.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURE_KEYS = {
    "stage", "path", "status", "payload", "result", "user", "pk", "id", "username", "full_name",
    "is_private", "profile_pic_url", "profile_pic_url_wrapped", "profile_pic_url_downloadable",
    "biography", "media_count", "follower_count", "following_count", "count", "page_info",
    "has_next_page", "end_cursor", "edges", "node", "__typename", "is_video", "dimensions",
    "height", "width", "shortcode", "display_url", "display_resources", "src", "config_width",
    "config_height", "url", "url_wrapped", "url_downloadable", "video_url", "video_url_wrapped",
    "video_url_downloadable", "edge_media_to_caption", "text", "taken_at_timestamp", "owner",
    "edge_sidecar_to_children", "version", "image_versions2", "candidates", "original_height",
    "original_width", "taken_at", "video_versions", "type", "title", "cover_media", "cropped_image_version",
}


def minimize_fixture(value):
    """Keep only contract fields; remove bystander/viewer/sticker information."""
    if isinstance(value, dict):
        return {key: minimize_fixture(child) for key, child in value.items() if key in FIXTURE_KEYS}
    if isinstance(value, list):
        return [minimize_fixture(child) for child in value]
    return value


def describe(value, depth=0):
    if depth > 8:
        return type(value).__name__
    if isinstance(value, dict):
        return {k: describe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return {"length": len(value), "examples": [describe(v, depth + 1) for v in value[:2]]}
    if isinstance(value, str):
        if value.startswith("https://"):
            return "<url>"
        if len(value) > 100:
            return "<long-string>"
    return value


def browser_cache_path() -> Path:
    """Match Playwright's platform cache while respecting an explicit override."""
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return Path(configured)
    if sys.platform == "win32":
        cache = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        cache = Path.home() / "Library" / "Caches"
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "ms-playwright"


async def probe() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--fixtures", type=Path, help="Optional directory for sanitized observed responses.")
    parser.add_argument("--inspect-frontend", action="store_true", help="Read only relevant public script excerpts; do not query.")
    parser.add_argument("--adapter-smoke", action="store_true", help="Run actual adapter once against NASA, no production state.")
    parser.add_argument("--download-sample", action="store_true", help="With adapter smoke, verify one owned image in memory.")
    args = parser.parse_args()
    if args.adapter_smoke:
        from ig_monitor.anonyig import AnonyIGScraper
        from ig_monitor.config import BrowserConfig
        config = BrowserConfig(True, 45, 0, browser_cache_path(),
                               max_pages_per_collection=1)
        async with AnonyIGScraper(config) as adapter:
            result = await adapter.scrape("https://instagram.com/nasa/")
            print(json.dumps({"event": "adapter_result", "source": result.source,
                              "profile_matches": result.snapshot.username == "nasa",
                              "profile_id_present": bool(result.profile_id),
                              "collections": {name: {"state": observation.state.value,
                                  "complete": observation.complete, "error": observation.error,
                                  "resume_present": bool(observation.cursor),
                                  "media": sum(item.category == name for item in result.media)}
                                  for name, observation in result.collections.items()},
                              "media_hosts": sorted({urlsplit(item.url).hostname for item in result.media})}))
            if args.download_sample:
                from PIL import Image
                candidate = next((item for item in result.media if item.kind == "image"), None)
                if candidate is None:
                    print(json.dumps({"event": "download_unverified", "reason": "no owned image candidate"}))
                    return 2
                data, content_type = await adapter.download(candidate.url, adapter.media_referer)
                with Image.open(io.BytesIO(data)) as downloaded:
                    dimensions = downloaded.size
                    downloaded.verify()
                print(json.dumps({"event": "download_verified_in_memory", "bytes": len(data),
                                  "content_type": content_type, "dimensions": dimensions,
                                  "sha256": hashlib.sha256(data).hexdigest()}))
        return 0
    async with async_playwright() as p:
        options = {"headless": True}
        if args.executable:
            options["executable_path"] = str(args.executable.resolve())
        browser = await p.chromium.launch(**options)
        context = await browser.new_context(locale="en-US", viewport={"width": 1440, "height": 1000})
        page = await context.new_page()
        page.set_default_timeout(15000)
        responses: list[dict] = []
        tasks: set[asyncio.Task] = set()
        stage = "initial"
        strings: dict[str, str] = {}

        def sanitize(value, key=""):
            if key in {"viewer", "profile_context_links_with_user_ids", "profile_context_facepile_users",
                       "friendship_status", "bio_links", "video_dash_manifest", "external_lynx_url"}:
                return None
            if any(part in key.lower() for part in ("token", "signature", "secret")):
                return "<redacted>"
            if isinstance(value, dict):
                return {k: sanitize(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [sanitize(v, key) for v in value]
            if isinstance(value, str):
                if value.startswith(("https://", "http://")):
                    if value not in strings:
                        strings[value] = f"https://media.example.test/asset-{len(strings) + 1}"
                    return strings[value]
                if key == "username":
                    return "fixture_account" if value == "nasa" else "other_owner"
                if key in {"caption", "text", "biography", "full_name"}:
                    return "Fixture text" if value else value
                if key in {"pk", "id", "fbid_v2", "shortcode", "end_cursor", "reel_id", "user_id"}:
                    if value not in strings:
                        strings[value] = str(100000 + len(strings))
                    return strings[value]
            return value

        async def capture(response):
            parsed = urlsplit(response.url)
            if args.inspect_frontend and parsed.path == "/js/app.js":
                script = await response.text()
                for term in ("postsV2", "page_info", "highlightStories", "product_type", "reelsData"):
                    matches = list(re.finditer(re.escape(term), script))
                    print(json.dumps({"term": term, "count": len(matches), "snippets": [
                        script[max(0, m.start() - 300):m.start() + 500] for m in matches[:6]]}))
                return
            if "/api/" not in parsed.path:
                return
            try:
                payload = await response.json()
            except Exception:
                payload = None
            record = {"stage": stage, "path": parsed.path, "status": response.status, "payload": payload}
            responses.append(record)
            if args.fixtures:
                args.fixtures.mkdir(parents=True, exist_ok=True)
                name = f"{len(responses):02d}_{stage}_{parsed.path.rsplit('/', 1)[-1]}.json"
                (args.fixtures / name).write_text(json.dumps(minimize_fixture(sanitize(record)), indent=2), encoding="utf-8")
            print(json.dumps({"event": "response", "path": parsed.path, "status": response.status,
                              "payload_shape": describe(minimize_fixture(sanitize(payload)))}))

        def on_response(response):
            task = asyncio.create_task(capture(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on("response", on_response)
        try:
            await page.goto("https://anonyig.com/en/", wait_until="domcontentloaded", timeout=45000)
            if args.inspect_frontend:
                await page.wait_for_timeout(2000)
                return 0
            await page.get_by_placeholder("@username or link", exact=True).fill("nasa")
            await page.get_by_placeholder("@username or link", exact=True).press("Enter")
            for _ in range(15):
                await page.wait_for_timeout(2000)
                if responses:
                    break
            await page.wait_for_timeout(3000)
            for next_stage in ("stories", "highlights", "highlight_album", "reels", "pagination"):
                if any(r["status"] in {422, 429, 403} for r in responses):
                    print(json.dumps({"event": "blocked", "reason": "source returned challenge/rate limit; stopped"}))
                    return 2
                stage = next_stage
                before = len(responses)
                if stage in {"stories", "highlights", "reels"}:
                    await page.get_by_role("button", name=stage, exact=True).click()
                elif stage == "highlight_album":
                    await page.get_by_text("Roman", exact=True).click()
                else:
                    await page.get_by_role("button", name="posts", exact=True).click()
                    for _ in range(6):
                        await page.locator("footer").scroll_into_view_if_needed()
                        await page.wait_for_timeout(1000)
                        if len(responses) > before:
                            break
                for _ in range(10):
                    await page.wait_for_timeout(1000)
                    if len(responses) > before:
                        break
                print(json.dumps({"event": "stage", "stage": stage,
                                  "text": (await page.locator("body").inner_text())[:2000]}))
            print(json.dumps({"event": "page", "url": page.url, "title": await page.title(),
                              "text": (await page.locator("body").inner_text())[:6000],
                              "inputs": await page.locator("input").evaluate_all(
                                  "xs=>xs.map(x=>({type:x.type,placeholder:x.placeholder,name:x.name}))"),
                              "buttons": await page.locator("button").all_text_contents()}))
            return 0
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await context.close()
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(probe()))
