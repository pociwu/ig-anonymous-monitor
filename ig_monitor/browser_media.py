"""Opt-in, bounded IGWatcher same-origin media transport; no API fallback."""
from __future__ import annotations

import asyncio
import base64
import binascii
from urllib.parse import quote, urlsplit

from .models import ScrapeFailure

ORIGIN = 'https://igwatcher.com'


def decode_result(result: dict, limit: int) -> tuple[bytes, str]:
    status = result.get('status')
    if status in (401, 403, 422, 429) or result.get('challenge'):
        raise ScrapeFailure(f'瀏覽器媒體來源拒絕或要求驗證 [http_status={status}]',
                            'IGWatcher', blocker='source_blocked', error_code='source_blocked')
    if status != 200 or result.get('error'):
        raise ScrapeFailure(f'瀏覽器媒體未取得完整回應 [http_status={status}]',
                            'IGWatcher', error_code='http_error')
    mime = result.get('mime', '').split(';')[0].strip().lower()
    if not mime.startswith(('image/', 'video/')) and mime != 'application/octet-stream':
        raise ScrapeFailure('瀏覽器媒體格式不符', 'IGWatcher', error_code='invalid_media')
    try:
        encoded = result['body']
        if not isinstance(encoded, str) or len(encoded) > ((limit + 2) // 3) * 4:
            raise ValueError
        body = base64.b64decode(encoded, validate=True)
        if not body or len(body) > limit:
            raise ValueError
    except (KeyError, ValueError, binascii.Error):
        raise ScrapeFailure('瀏覽器媒體內容缺失或超過上限', 'IGWatcher', error_code='invalid_media') from None
    return body, mime


# Stream before crossing the browser/Python boundary, avoiding an unbounded blob.
FETCH_MEDIA = """async ({url, limit, timeout}) => {
 const controller = new AbortController();
 const timer = setTimeout(() => controller.abort(), timeout);
 try {
  const r = await fetch(url, {cache:'no-store', redirect:'error', signal:controller.signal});
  const mime = r.headers.get('content-type') || '';
  if (r.status !== 200) return {status:r.status};
  const declared = r.headers.get('content-length');
  if (declared && (!/^\\d+$/.test(declared) || Number(declared)>limit))
   { await r.body.cancel(); return {status:r.status,error:'size'}; }
  const reader=r.body.getReader(), chunks=[]; let size=0;
  while(true) {
   const {done,value}=await reader.read(); if(done) break;
   size+=value.length;
   if(size>limit) { await reader.cancel(); return {status:r.status,error:'size'}; }
   chunks.push(value);
  }
  if(declared && Number(declared)!==size) return {status:r.status,error:'length'};
  const bytes=new Uint8Array(size); let offset=0;
  for(const c of chunks){bytes.set(c,offset);offset+=c.length;}
  const prefix=new TextDecoder().decode(bytes.slice(0,131072)).toLowerCase();
  if(['cf-chl-','challenge-platform','turnstile','verify you are human','verification_required','captcha'].some(x=>prefix.includes(x)))
   return {status:r.status,challenge:true};
  let binary=''; for(let i=0;i<size;i+=32768) binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
  return {status:r.status,mime,body:btoa(binary)};
 } finally {clearTimeout(timer);}
}"""


class BrowserMedia:
    def __init__(self, config, guard, limit):
        self.config, self.guard, self.limit = config, guard, limit
        self._pw = self._browser = self._context = self._page = None
        self._allowed_media = None
        self._denied = False

    def _check(self):
        self.guard()
        if self._denied:
            raise ScrapeFailure('瀏覽器媒體來源已暫停', 'IGWatcher', blocker='source_blocked')

    async def _route(self, route):
        try:
            self._check()
        except ScrapeFailure:
            await route.abort()
            return
        # This transport loads no scripts, API data, advertising, or credentials.
        # Only its origin document and the exact validated media request are allowed.
        url = route.request.url
        if route.request.method != 'GET' or url not in (ORIGIN + '/', self._allowed_media):
            await route.abort()
            return
        await route.continue_()

    async def _start(self):
        if self._page is not None:
            return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(channel='chromium', headless=self.config.headless)
            self._context = await self._browser.new_context(service_workers='block')
            await self._context.route('**/*', self._route)
            self._page = await self._context.new_page()
            r = await self._page.goto(ORIGIN + '/', wait_until='domcontentloaded', timeout=30000)
            if r is None or r.status != 200:
                decode_result({'status': r.status if r else None}, self.limit)
            if urlsplit(self._page.url).netloc != 'igwatcher.com':
                raise ScrapeFailure('瀏覽器媒體來源位置不符', 'IGWatcher')
            body = (await self._page.locator('body').inner_text())[:131072].lower()
            if any(x in body for x in ('verify you are human', 'captcha', 'turnstile')):
                decode_result({'status': 200, 'challenge': True}, self.limit)
        except BaseException:
            await self.close()
            raise

    async def download(self, url):
        from .igwatcher import _media_url
        self._check()
        safe = _media_url(url)
        self._allowed_media = ORIGIN + '/wp-json/igw/v1/media?u=' + quote(base64.b64encode(safe.encode()).decode(), safe='')
        try:
            await self._start()
            self._check()
            async with asyncio.timeout(65):
                result = await self._page.evaluate(FETCH_MEDIA, {
                    'url': self._allowed_media, 'limit': self.limit, 'timeout': 60000})
            self._check()
            return decode_result(result, self.limit)
        except ScrapeFailure as exc:
            if exc.blocker == 'source_blocked':
                self._denied = True
            raise
        except Exception:
            raise ScrapeFailure('瀏覽器媒體請求未完成；本輪不重試', 'IGWatcher',
                                error_code='browser_media_failed') from None
        finally:
            self._allowed_media = None

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()
            self._pw = self._browser = self._context = self._page = None
