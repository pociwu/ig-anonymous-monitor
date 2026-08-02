from __future__ import annotations

import argparse
import logging
import signal
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import load_config
from .db import Database
from .instagram_source import InstagrapiRelationshipSource
from .relationships import RelationshipWorker


LOG = logging.getLogger("ig_monitor.relationship_worker")


def run(config_path: Path, session_path: Path, poll_seconds: int = 60) -> None:
    config = load_config(config_path, require_telegram=False)
    db = Database(config.paths.data_dir / "state.sqlite3")
    source = InstagrapiRelationshipSource(session_path)
    worker = RelationshipWorker(db, config.instagram_enrichment, source)
    stopped = False

    def stop(*_args):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopped:
            outcome = worker.run_once(datetime.now(UTC))
            if outcome.status not in ("idle", "spacing", "daily_budget", "disabled", "collector_unavailable"):
                LOG.info("relationship work status=%s job=%s detail=%s", outcome.status, outcome.job_id, outcome.detail)
            for _ in range(max(1, poll_seconds)):
                if stopped:
                    break
                time.sleep(1)
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run authenticated relationship collection worker")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--session", default="collector-secrets/session.json")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(Path(args.config).resolve(), Path(args.session).resolve(), args.poll_seconds)


if __name__ == "__main__":
    main()
