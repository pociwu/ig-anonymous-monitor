from datetime import UTC, datetime, timedelta
import asyncio
from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

from ig_monitor.config import AccountConfig, load_config
from ig_monitor.db import Database
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot, ScrapeFailure
from ig_monitor.monitor import Monitor
from ig_monitor.telegram import format_event
from ig_monitor.cli import build_parser, _async_main
from ig_monitor.utils import process_lock


def populated_db(tmp_path):
    db = Database(tmp_path / 'state.sqlite3')
    db.sync_accounts([AccountConfig('https://instagram.com/alice/', True, 'Alice')])
    account = db.enabled_accounts()[0]
    snapshot = ProfileSnapshot('alice', None, 2, 2, 2, '', PrivacyState.PUBLIC, '')
    db.record_success(account['id'], snapshot, [], [
        MediaCandidate(str(i), 'posts', 'image', f'https://scontent.cdninstagram.com/{i}.jpg',
                       source='igwatcher', published_at=f'2026-09-{20+i}T00:00:00+00:00')
        for i in range(2)
    ])
    return db, account


def test_failed_item_waits_after_restart_and_does_not_starve_other_media(tmp_path):
    db, account = populated_db(tmp_path)
    try:
        first = db.pending_media(account['id'], 1, source='igwatcher')[0]
        now = datetime(2026, 9, 28, tzinfo=UTC)
        db.mark_media_failed(first['id'], 'HTTP 404', now=now)
        db.close()
        db = Database(tmp_path / 'state.sqlite3')
        waiting = db.pending_media(account['id'], 10, source='igwatcher', now=now)
        assert len(waiting) == 1
        assert waiting[0]['id'] != first['id']
        assert db.summary()['pending'] == 2
        due = db.pending_media(account['id'], 10, source='igwatcher', now=now+timedelta(hours=1))
        assert {r['id'] for r in due} == {first['id'], waiting[0]['id']}
    finally:
        db.close()


@pytest.mark.parametrize('mode', ['ok', 'failed', 'blocked', 'cooldown', 'disabled', 'bounded', 'mid_cooldown', 'account_failed', 'private', 'account_disabled', 'normal_failed', 'query_403', 'normal_query_403'])
def test_download_only_is_serial_bounded_and_never_scrapes_profiles(tmp_path, monkeypatch, mode):
    db, account = populated_db(tmp_path)
    p = tmp_path / 'config.yaml'
    p.write_text('accounts:\n  - url: https://instagram.com/alice/\nbrowser:\n  anonymous_source: igwatcher\n', encoding='utf-8')
    cfg = load_config(p, require_telegram=False)
    cfg = replace(cfg, schedule=replace(cfg.schedule, media_download_enabled=mode!='disabled',
                                       media_limit_per_account=1 if mode=='bounded' else 10),
                  apify=replace(cfg.apify, enabled=False), telegram=replace(cfg.telegram, enabled=False),
                  heartbeat=replace(cfg.heartbeat, enabled=False), dedup=replace(cfg.dedup, enabled=False))
    output = BytesIO()
    Image.new('RGB', (16, 16), 'red').save(output, format='PNG')
    calls, sleeps = [], []

    class Downloader:
        config = cfg.browser
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def scrape(self, *_args, **_kwargs):
            if mode == 'normal_failed': raise ScrapeFailure('source timeout', 'profile')
            pytest.fail('download-only must not request profiles')
        async def download(self, url, referer):
            calls.append(url)
            if len(calls) == 1 and mode in ('failed', 'blocked'):
                raise ScrapeFailure('fixed failure', 'test', blocker='source_blocked' if mode=='blocked' else None)
            return output.getvalue(), 'image/png'

    async def sleep(seconds):
        sleeps.append(seconds)
        if mode == 'mid_cooldown': db.record_source_block('igwatcher', 'blocked')
    monkeypatch.setattr('ig_monitor.monitor.ProfileScraper', lambda _: Downloader())
    monkeypatch.setattr('ig_monitor.monitor.asyncio.sleep', sleep)
    try:
        if mode in ('account_failed', 'cooldown'):
            db.record_failure(account['id'], 'Alice', 'source timeout', None)
        if mode == 'private':
            snapshot = ProfileSnapshot('alice', None, 2, 2, 2, '', PrivacyState.PRIVATE, '')
            db.record_success(account['id'], snapshot, [], [])
        if mode == 'account_disabled':
            db.conn.execute('UPDATE accounts SET enabled=0 WHERE id=?', (account['id'],))
            db.conn.commit()
        if mode == 'cooldown': db.record_source_block('igwatcher', 'blocked')
        if mode in ('query_403', 'normal_query_403'):
            db.record_source_block('igwatcher', 'http_status=403 phase=api endpoint=stories redirects=0 signal=http_status')
        if mode == 'account_failed':
            assert db.media_backlog('igwatcher')['ready'] == 2
            assert db.media_backlog('igwatcher')['ineligible'] == 0
        monitor = Monitor(cfg, db)
        asyncio.run(monitor.run() if mode in ('normal_failed','normal_query_403') else monitor.download_pending())
        assert len(calls) == (0 if mode in ('disabled', 'cooldown', 'private', 'account_disabled') else 1 if mode in ('blocked', 'bounded', 'mid_cooldown') else 2)
        assert sleeps == ([10] if mode in ('ok', 'failed', 'mid_cooldown', 'account_failed', 'query_403', 'normal_query_403', 'normal_failed') else [])
        if mode in ('account_failed', 'normal_failed'):
            assert db.enabled_accounts()[0]['fail_count'] == 1
            assert db.media_backlog('igwatcher')['ineligible'] == 0
        if mode == 'failed':
            assert db.media_backlog('igwatcher')['retry_wait'] == 1
            assert db.media_counts(account['id'])['downloaded'] == 1
        if mode == 'blocked': assert db.media_cooldown('igwatcher')
        assert not any(e['kind']=='recovery' for e in db.pending_events(100))
    finally:
        db.close()


def test_heartbeat_does_not_claim_health_from_process_liveness():
    text = format_event('heartbeat', dict(accounts=11, normal=8, private=5, public=6, error=3, pending=154))
    assert '運作正常' not in text
    assert '狀態摘要' in text


def test_retry_delay_grows_and_is_capped(tmp_path):
    db, account = populated_db(tmp_path)
    try:
        media = db.pending_media(account['id'], 1)[0]
        now = datetime(2026, 9, 28, tzinfo=UTC)
        for hours in [1, 2, 4, 8, 16, 24, 24]:
            db.mark_media_failed(media['id'], 'HTTP 404', now=now)
            row = db.conn.execute('SELECT next_retry_at FROM media WHERE id=?', (media['id'],)).fetchone()
            assert datetime.fromisoformat(row[0]) == now + timedelta(hours=hours)
            now += timedelta(days=2)
        assert db.summary()['pending'] == 2
    finally:
        db.close()


def test_download_only_cli_respects_shared_lock(tmp_path, monkeypatch):
    config = tmp_path / 'config.yaml'
    config.write_text('accounts:\n  - url: https://instagram.com/alice/\ntelegram:\n  enabled: false\nbrowser:\n  anonymous_source: igwatcher\n', encoding='utf-8')
    cfg = load_config(config, require_telegram=False, require_apify=False)
    monkeypatch.setattr('ig_monitor.cli.load_config', lambda *_args, **_kwargs: cfg)
    monkeypatch.setattr('ig_monitor.cli.setup_logging', lambda *_args, **_kwargs: None)
    async def forbidden(_self): pytest.fail('shared lock must prevent download invocation')
    monkeypatch.setattr(Monitor, 'download_pending', forbidden)
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    with process_lock(cfg.paths.data_dir / 'monitor.lock') as acquired:
        assert acquired
        assert asyncio.run(_async_main(build_parser().parse_args(['--download-pending']))) == 0


def test_backlog_separates_other_sources_and_disabled_downloads(tmp_path):
    db, account = populated_db(tmp_path)
    try:
        first = db.pending_media(account['id'], 1)[0]
        db.conn.execute("UPDATE media_memberships SET source='legacy' WHERE media_id=?", (first['id'],))
        db.conn.commit()
        report = db.media_backlog('igwatcher', enabled=False)
        assert report['total'] == report['other_source'] + report['paused'] == 2
        assert report['other_source'] == report['paused'] == 1
        assert report['ready'] == 0
    finally:
        db.close()


def test_backlog_explains_source_cooldown_and_other_source_without_losing_total(tmp_path):
    db, account = populated_db(tmp_path)
    try:
        now = datetime.now(UTC)
        first = db.pending_media(account['id'], 1, source='igwatcher')[0]
        db.mark_media_failed(first['id'], 'HTTP 404', now=now)
        db.record_source_block('igwatcher', 'source blocked', now=now)
        report = db.media_backlog('igwatcher', now=now)
        assert report['total'] == 2
        assert report['retry_wait'] == 1
        assert report['source_wait'] == 1
        assert report['ready'] == 0
        assert report['other_source'] == 0
        text = format_event('heartbeat', {**db.summary(), 'media_backlog': report})
        assert '檔案退避：1' in text
        assert '來源冷卻：1' in text
        assert '運作正常' not in text
    finally:
        db.close()
