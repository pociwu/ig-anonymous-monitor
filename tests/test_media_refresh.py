import asyncio
from types import SimpleNamespace

import pytest

from ig_monitor.models import ScrapeFailure
from ig_monitor.media_refresh import match_asset, PostRefresher


OLD = 'https://s.cdninstagram.com/a/123456789_987654321_n.jpg?old=1'
NEW = 'https://other.cdninstagram.com/a/123456789_987654321_n.jpg?new=2'


def test_match_requires_parent_and_unique_asset_not_position():
    parent = {'id': '123_456', 'children': [{'image_url': NEW}]}
    assert match_asset(parent, '123_456', OLD, 'image') == NEW
    for bad in [
        {'id': '999_456', 'children': [{'image_url': NEW}]},
        {'id': '123_456', 'children': [{'image_url': NEW}] * 2},
        {'id': '123_456', 'children': [{'image_url': 'https://s.cdninstagram.com/other.jpg'}]},
        {'id': '123_456', 'children': []},
        {'id': '123_456', 'anon': True, 'image_url': NEW},
        {'id': '123_456', 'media_type': 8, 'children': [], 'image_url': NEW},
    ]:
        with pytest.raises(ScrapeFailure): match_asset(bad, '123_456', OLD, 'image')


def test_unsafe_refreshed_host_is_rejected():
    with pytest.raises(ScrapeFailure):
        match_asset({'id':'123_456','image_url':NEW.replace('other.cdninstagram.com','127.0.0.1')}, '123_456', OLD, 'image')


def test_refresh_cooldown_makes_zero_browser_calls():
    refresher = PostRefresher(SimpleNamespace(headless=True), lambda: True)
    with pytest.raises(ScrapeFailure) as error:
        asyncio.run(refresher.resolve('alice', '123_456', OLD, 'image'))
    assert error.value.blocker == 'source_cooldown'


def test_refresh_failure_never_downloads_old_url(monkeypatch):
    from pathlib import Path
    from ig_monitor.config import BrowserConfig
    from ig_monitor.igwatcher import IGWatcherScraper
    cfg = BrowserConfig(True, 30, 0, Path('.'), 'igwatcher', igwatcher_media_transport='browser_proxy')
    async def failed(*args): raise ScrapeFailure('not matched','refresh')
    async def forbidden(*args): pytest.fail('must not fall back to old URL')
    monkeypatch.setattr(PostRefresher, 'resolve', failed)
    monkeypatch.setattr(IGWatcherScraper, 'download', forbidden)
    async def run():
        async with IGWatcherScraper(cfg) as scraper:
            with pytest.raises(ScrapeFailure):
                await scraper.download_queued({'url':OLD,'kind':'image','category':'posts'},
                    {'queried_username':'alice','group_id':'123_456'})
    asyncio.run(run())


@pytest.mark.parametrize('cooldown', [False, True])
def test_real_queue_refreshes_before_download_and_preserves_history(tmp_path, monkeypatch, cooldown):
    from dataclasses import replace
    from io import BytesIO
    from PIL import Image
    from ig_monitor.config import AccountConfig, load_config
    from ig_monitor.db import Database
    from ig_monitor.igwatcher import IGWatcherScraper
    from ig_monitor.media import download_account_media
    from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot
    p=tmp_path/'config.yaml'
    p.write_text('accounts:\n  - url: https://instagram.com/alice/\nbrowser:\n  anonymous_source: igwatcher\n',encoding='utf8')
    cfg=load_config(p,require_telegram=False)
    browser=replace(cfg.browser,igwatcher_media_transport='browser_proxy')
    db=Database(tmp_path/'state.sqlite3')
    db.sync_accounts([AccountConfig('https://instagram.com/alice/',True,'Alice')])
    account=db.enabled_accounts()[0]
    snap=ProfileSnapshot('alice',None,1,1,1,'',PrivacyState.PUBLIC,'')
    db.record_success(account['id'],snap,[],[MediaCandidate('test','posts','image',OLD,source='igwatcher',parent_id='123_456')])
    db.conn.execute("UPDATE media_memberships SET queried_username='alice',group_id='123_456'"); db.conn.commit()
    calls=[]
    async def load(self, username, group):
        calls.append('refresh')
        return {'id':group,'children':[{'image_url':NEW}]}
    output=BytesIO(); Image.new('RGB',(10,10),'red').save(output,format='PNG')
    async def download(self,url,referer):
        assert url==NEW
        calls.append('download')
        return output.getvalue(),'image/png'
    monkeypatch.setattr(PostRefresher,'_load',load)
    monkeypatch.setattr(IGWatcherScraper,'download',download)
    if cooldown: db.record_source_block('igwatcher','http_status=403 phase=api signal=http_status')
    async def run():
        async with IGWatcherScraper(browser) as scraper:
            return await download_account_media(db,scraper,account,tmp_path/'downloads',1,replace(cfg.dedup,enabled=False))
    try:
        if cooldown:
            with pytest.raises(ScrapeFailure) as exc: asyncio.run(run())
            assert exc.value.blocker=='source_cooldown'
            assert calls==[]
        else:
            assert asyncio.run(run())['downloaded']==1
            assert calls==['refresh','download']
        assert db.conn.execute('SELECT url FROM media').fetchone()[0]==OLD
    finally: db.close()
