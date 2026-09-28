"""Run in Chromium with --network none; route fixtures exercise real UI refresh."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext
from ig_monitor.config import BrowserConfig
from ig_monitor.media_refresh import PostRefresher
from ig_monitor.models import ScrapeFailure

OLD = 'https://s.cdninstagram.com/123456789_987654321_n.jpg?old=1'
NEW = 'https://s.cdninstagram.com/123456789_987654321_n.jpg?new=2'
HTML = """<label>Instagram Nickname<input></label><div id='result'></div>
<script>
document.querySelector('input').onkeydown=async e=>{
 if(e.key!=='Enter')return;
 await fetch('/wp-json/igw/v1/search?username=alice');
 result.innerHTML='<button class="tab-btn" data-tab="posts">Posts</button><div class="igw-posts"></div>';
 document.querySelector('button').onclick=async()=>{
  await fetch('/wp-json/igw/v1/posts?username=alice');
  document.querySelector('.igw-posts').innerHTML='<button data-pidx="0">Post</button>';
  document.querySelector('[data-pidx]').onclick=()=>fetch('/api/post-full?code=ABCdef123&kind=p&owner=456');
 };
};
</script>"""


async def main():
    original = BrowserContext.route
    cfg = BrowserConfig(True, 30, 0, Path('/tmp'), 'igwatcher')
    for mode in ('full', 'lazy', 'refused', 'wrong_identity'):
        requests = []
        async def install(context, pattern, handler, **kwargs):
            async def intercepted(route):
                class Shim:
                    request = route.request
                    async def abort(self): await route.abort()
                    async def continue_(self):
                        path = urlsplit(route.request.url).path
                        requests.append(path)
                        if path == '/':
                            return await route.fulfill(status=200, content_type='text/html', body=HTML)
                        if path.endswith('search'):
                            payload={'user':{'username':'alice','is_private':False}}
                        else:
                            parent={'id':'123_456','shortcode':'ABCdef123','is_carousel':True,
                                    'taken_at':100,'children':[{'image_url':NEW}]}
                            if path.endswith('posts'):
                                if mode != 'full': parent.update(anon=True, children=[])
                                payload=[parent]
                            else:
                                if mode=='refused': return await route.fulfill(status=403,body='denied')
                                if mode=='wrong_identity': parent['shortcode']='wrong123'
                                payload=parent
                        await route.fulfill(status=200, content_type='application/json',body=json.dumps({'status':'success','data':payload}))
                await handler(Shim())
            await original(context, pattern, intercepted, **kwargs)
        BrowserContext.route = install
        try:
            refresher=PostRefresher(cfg,lambda:False)
            if mode in ('refused','wrong_identity'):
                try: await refresher.resolve('alice','123_456',OLD,'image')
                except ScrapeFailure as exc:
                    assert (exc.blocker=='source_blocked') == (mode=='refused')
                else: raise AssertionError(mode)
            else:
                assert await refresher.resolve('alice','123_456',OLD,'image') == NEW
                before=len(requests)
                assert await refresher.resolve('alice','123_456',OLD,'image') == NEW
                assert len(requests)==before
            assert requests.count('/wp-json/igw/v1/posts')==1
            assert '/wp-json/igw/v1/media' not in requests
            print(mode,'OK',flush=True)
        finally: BrowserContext.route=original


asyncio.run(main())
