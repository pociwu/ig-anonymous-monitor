from datetime import UTC, datetime
from types import SimpleNamespace
import asyncio

from ig_monitor.media import stale_cdn_locator, download_account_media
from ig_monitor.config import AccountConfig, DedupConfig
from ig_monitor.db import Database
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot


def test_expiry_hint_is_scoped_and_conservative():
    now = datetime(2026, 9, 29, tzinfo=UTC)
    assert stale_cdn_locator('https://s.cdninstagram.com/a.mp4?oe=65000000', now)
    assert not stale_cdn_locator('https://example.org/a?oe=65000000', now)
    assert not stale_cdn_locator('https://s.cdninstagram.com/a?oe=FFFFFFFF', now)
    assert not stale_cdn_locator('https://s.cdninstagram.com/a?oe=garbage', now)
    assert not stale_cdn_locator('https://s.cdninstagram.com/a', now)


def test_stale_story_does_not_request_or_cool_source(tmp_path):
    db = Database(tmp_path / 'state.sqlite3')
    try:
        db.sync_accounts([AccountConfig('https://instagram.com/alice/', True, 'Alice')])
        account = db.enabled_accounts()[0]
        db.record_success(account['id'], ProfileSnapshot('alice', None, 1, 1, 1, '', PrivacyState.PUBLIC, ''), [], [
            MediaCandidate('old', 'stories', 'video', 'https://s.cdninstagram.com/a.mp4?oe=65000000', source='igwatcher')])
        class Scraper:
            config = SimpleNamespace(anonymous_source='igwatcher')
            async def download(self, *args):
                raise AssertionError('stale URL must not reach network')
        stats = asyncio.run(download_account_media(db, Scraper(), account, tmp_path, 1, DedupConfig(False, 6, 2, 1, 2)))
        assert stats['failed'] == 1
        assert db.media_cooldown('igwatcher') is None
        row = db.conn.execute('select status,last_error,next_retry_at from media').fetchone()
        assert row['status'] == 'failed' and '待刷新' in row['last_error']
        assert row['next_retry_at']
    finally:
        db.close()
