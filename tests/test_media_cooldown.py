from ig_monitor.db import Database
import pytest


@pytest.mark.parametrize('error,blocks_media', [
    ('http_status=403 phase=api endpoint=stories redirects=0 signal=http_status', False),
    ('http_status=429 phase=api endpoint=stories redirects=0 signal=http_status', True),
    ('http_status=403 phase=api endpoint=stories redirects=0 signal=challenge', True),
    ('http_status=403 phase=media endpoint=cdn redirects=0 signal=http_status', True),
    ('unknown legacy reason', True),
])
def test_query_cooldown_scope(tmp_path,error,blocks_media):
    db=Database(tmp_path/'state.sqlite3')
    try:
        db.record_source_block('igwatcher',error)
        assert db.source_cooldown('igwatcher')
        assert bool(db.media_cooldown('igwatcher')) == blocks_media
        db.record_media_block('igwatcher','download refused')
        assert db.media_cooldown('igwatcher')
        db.record_source_recovery('igwatcher')
        assert db.media_cooldown('igwatcher')
    finally: db.close()


def test_media_refusal_does_not_suspend_query_but_rate_limit_does(tmp_path):
    db=Database(tmp_path/'state.sqlite3')
    try:
        db.record_media_block('igwatcher','[http_status=403] signal=http_status')
        assert db.media_cooldown('igwatcher')
        assert db.source_cooldown('igwatcher') is None
        db.record_media_block('igwatcher','[http_status=429] signal=http_status')
        assert db.source_cooldown('igwatcher')
    finally: db.close()
