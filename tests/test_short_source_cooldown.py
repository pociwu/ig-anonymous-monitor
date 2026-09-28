from datetime import UTC, datetime, timedelta

import pytest

from ig_monitor.db import Database


@pytest.mark.parametrize('source', ['igwatcher', 'igwatcher:media'])
def test_igwatcher_cooldown_is_ten_then_fifteen_minutes(tmp_path, source):
    db = Database(tmp_path / 'state.sqlite3')
    try:
        now = datetime(2026, 9, 28, tzinfo=UTC)
        for minutes in (10, 15, 15, 15, 15):
            state = db.record_source_block(source, 'http_status=403', now)
            end = now + timedelta(minutes=minutes)
            assert datetime.fromisoformat(state['next_allowed_at']) == end
            repeated = db.record_source_block(source, 'http_status=403', now + timedelta(seconds=1))
            assert repeated['next_allowed_at'] == state['next_allowed_at']
            assert repeated['block_count'] == state['block_count']
            assert db.source_cooldown(source, end - timedelta(seconds=1))
            assert db.source_cooldown(source, end) is None
            now = end
    finally:
        db.close()
