"""Bounded refresh of a queued post through the site's normal public UI.

Fresh URLs are transport locators, not new authorship or carousel identities.
Historical URLs and memberships remain unchanged. No retry of a rejected URL.
"""
from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import parse_qs, urlsplit

from .models import ScrapeFailure

ORIGIN = 'https://igwatcher.com'


def failure(message='無法確認原媒體的新連結'):
    return ScrapeFailure(message, 'IGWatcher 網址刷新', error_code='refresh_unavailable')


def match_asset(parent, group_id, old_url, kind):
    from .igwatcher import _media_url
    if not isinstance(parent, dict) or parent.get('id') != group_id or parent.get('anon') is True:
        raise failure()
    key = 'image_url' if kind == 'image' else 'video_url'
    items = parent.get('children') if parent.get('is_carousel') or parent.get('media_type') == 8 or parent.get('children') else [parent]
    if not isinstance(items, list) or not items or len(items) > 20:
        raise failure()
    basename = urlsplit(old_url).path.rsplit('/', 1)[-1]
    # Generic names are not sufficient evidence for a historical observation.
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,}\.(?:jpg|jpeg|png|webp|mp4)', basename):
        raise failure()
    matches = [x.get(key) for x in items if isinstance(x, dict) and isinstance(x.get(key), str)
               and urlsplit(x[key]).path.rsplit('/', 1)[-1] == basename]
    if len(matches) != 1:
        raise failure()
    try:
        return _media_url(matches[0])
    except ValueError:
        raise failure('刷新後媒體網址不符合允許範圍') from None


class PostRefresher:
    def __init__(self, config, guard):
        self.config, self.guard = config, guard
        self.cache = {}
        self.attempted = set()

    def check(self):
        if self.guard():
            raise ScrapeFailure('查詢或下載冷卻中，未刷新網址', 'IGWatcher', blocker='source_cooldown')

    async def resolve(self, username, group_id, old_url, kind):
        self.check()
        if (not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.]{1,30}', username)
                or not isinstance(group_id, str) or not re.fullmatch(r'[1-9][0-9]*_[1-9][0-9]*', group_id)):
            raise failure('缺少可確認的查詢帳號或父貼文')
        key = (username, group_id)
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 180:
            return match_asset(cached[1], group_id, old_url, kind)
        if key in self.attempted:
            raise failure('本輪已刷新此貼文，不重複請求')
        self.attempted.add(key)
        parent = await self._load(username, group_id)
        self.check()
        self.cache[key] = (time.monotonic(), parent)
        return match_asset(parent, group_id, old_url, kind)

    async def _load(self, username, group_id):
        from playwright.async_api import async_playwright
        from .igwatcher import _json, JSON_BYTE_LIMIT
        seen, tasks, data = set(), set(), {}
        ready = {name: asyncio.Event() for name in ('search', 'posts', 'post-full')}
        stopped = asyncio.Event()
        rejection = None
        code = None
        count = 0

        async def route(r):
            nonlocal count, rejection
            try: self.check()
            except ScrapeFailure as exc:
                rejection = exc; stopped.set(); return await r.abort()
            u = urlsplit(r.request.url)
            endpoint = u.path.rsplit('/', 1)[-1]
            if stopped.is_set() or u.scheme != 'https' or u.netloc != 'igwatcher.com' or r.request.method != 'GET':
                return await r.abort()
            if u.path.startswith(('/api/', '/wp-json/')) and not u.path.endswith('.js'):
                q = parse_qs(u.query)
                allowed = ((u.path in ('/wp-json/igw/v1/search', '/wp-json/igw/v1/posts')
                            and q.get('username') == [username])
                           or (u.path == '/api/post-full' and code and q.get('code') == [code]
                               and q.get('kind') == ['p'] and q.get('owner') == [group_id.split('_')[1]]))
                if not allowed or endpoint in seen: return await r.abort()
                seen.add(endpoint)
            elif r.request.resource_type not in ('document', 'script', 'stylesheet'):
                return await r.abort()
            count += 1
            if count > 40:
                rejection = failure('網址刷新超過請求上限'); stopped.set(); return await r.abort()
            await r.continue_()

        async def response(r):
            nonlocal rejection
            endpoint = urlsplit(r.url).path.rsplit('/', 1)[-1]
            if r.status in (401, 403, 422, 429):
                rejection = ScrapeFailure(
                    f'網址刷新被拒絕 [http_status={r.status}] phase=refresh signal=http_status',
                    'IGWatcher', blocker='source_blocked')
                stopped.set(); return
            if endpoint not in ready: return
            try:
                body = await r.body()
                if len(body) > JSON_BYTE_LIMIT: raise ValueError
                lower = body.lower()
                if any(x in lower for x in (b'turnstile', b'verification_required', b'captcha', b'cf-chl-')):
                    rejection = ScrapeFailure('網址刷新要求驗證 phase=refresh signal=challenge', 'IGWatcher', blocker='source_blocked')
                    stopped.set(); return
                payload = _json(body)
                if (r.status != 200 or payload.get('status') != 'success' or payload.get('fetch_failed')
                        or any(payload.get(k) for k in ('error', 'errors', 'error_type', 'note', 'toolDown'))):
                    raise ValueError
                data[endpoint] = payload.get('data')
            except Exception:
                rejection = failure('網址刷新回應不完整'); stopped.set()
            finally: ready[endpoint].set()

        def on_response(r):
            task = asyncio.create_task(response(r)); tasks.add(task)
            task.add_done_callback(tasks.discard)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chromium', headless=self.config.headless, timeout=15000)
            try:
                context = await browser.new_context(service_workers='block')
                await context.route('**/*', route)
                context.on('response', on_response)
                page = await context.new_page()

                async def workflow():
                    nonlocal code
                    r = await page.goto(ORIGIN + '/', wait_until='domcontentloaded', timeout=30000)
                    if not r or r.status != 200: raise failure('網址刷新首頁未成功')
                    box = page.get_by_role('textbox', name='Instagram Nickname', exact=True)
                    await box.fill(username); await box.press('Enter')
                    await ready['search'].wait()
                    profile = data.get('search')
                    user = profile.get('user') if isinstance(profile, dict) else None
                    if not isinstance(user, dict) or user.get('username') != username or user.get('is_private') is not False:
                        raise failure('刷新時帳號身分或公開狀態未確認')
                    await page.locator('.tab-btn[data-tab="posts"]').click(timeout=15000)
                    await ready['posts'].wait()
                    items = data.get('posts')
                    if not isinstance(items, list) or len(items) > 100: raise failure()
                    parents = [x for x in items if isinstance(x, dict) and x.get('id') == group_id]
                    if len(parents) != 1: raise failure('第一頁沒有原貼文；保留待處理，不使用舊連結')
                    parent = parents[0]
                    if parent.get('anon') is True:
                        code = parent.get('shortcode')
                        if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{5,30}', code): raise failure()
                        ordered = sorted(items, key=lambda x: float(x.get('_ts0') or x.get('taken_at_timestamp') or x.get('taken_at') or 0), reverse=True)
                        index = next(i for i, x in enumerate(ordered) if x.get('id') == group_id)
                        await page.locator('.tab-btn[data-tab="posts"]').click()
                        await page.locator(f'.igw-posts [data-pidx="{index}"]').click(timeout=15000)
                        await ready['post-full'].wait()
                        full = data.get('post-full')
                        if not isinstance(full, dict) or full.get('shortcode') != code or full.get('id', group_id) != group_id:
                            raise failure('完整貼文身分不符')
                        parent = {**full, 'id': group_id}
                    return parent

                work = asyncio.create_task(workflow()); stop = asyncio.create_task(stopped.wait())
                try:
                    done, _ = await asyncio.wait((work, stop), timeout=90, return_when=asyncio.FIRST_COMPLETED)
                    if rejection: raise rejection
                    if work not in done: raise failure('網址刷新逾時；本輪不重試')
                    return await work
                finally:
                    stopped.set(); work.cancel(); stop.cancel()
                    await asyncio.gather(work, stop, return_exceptions=True)
            except ScrapeFailure:
                raise
            except Exception:
                raise failure('網址刷新流程未完成；本輪不重試') from None
            finally:
                for task in list(tasks): task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await browser.close()
