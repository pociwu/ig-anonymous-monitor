from __future__ import annotations

import argparse
import logging
import signal
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import load_config
from .db import Database
from .member_enrichment import MemberEnrichmentWorker, PlaywrightMemberProfileSource


def run(config_path: Path, poll_seconds: int) -> None:
    config = load_config(config_path, require_telegram=False, require_apify=False)
    db = Database(config.paths.data_dir / "state.sqlite3")
    stopped = False

    def stop(*_args):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        worker = MemberEnrichmentWorker(
            db, config.instagram_enrichment, PlaywrightMemberProfileSource(config.browser)
        )
        while not stopped:
            outcome = worker.run_once(datetime.now(UTC))
            if outcome.status not in ("idle", "spacing", "daily_budget", "disabled"):
                logging.info("member enrichment status=%s job=%s", outcome.status, outcome.job_id)
            time.sleep(max(1, poll_seconds))
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run anonymous member profile enrichment worker")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(Path(args.config).resolve(), args.poll_seconds)


if __name__ == "__main__":
    main()
