from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import load_config


LOG = logging.getLogger("ig_monitor.scheduler")
RunOnce = Callable[[], Awaitable[int]]


async def run_scheduler(interval_seconds: float, run_once: RunOnce, stop: asyncio.Event) -> None:
    """Run one inspection immediately, then wait between completed inspections."""
    while not stop.is_set():
        try:
            result = await run_once()
            if result:
                LOG.warning("Inspection exited with status %d; it will be retried", result)
        except Exception:
            LOG.exception("Inspection crashed; it will be retried")
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


async def _run_monitor_process(config_path: Path) -> int:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "ig_monitor", "--config", str(config_path)
    )
    return await process.wait()


async def _async_main(config_path: Path) -> None:
    config = load_config(config_path)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
    interval_seconds = config.schedule.interval_minutes * 60
    LOG.info("Scheduler started; interval=%d minutes", config.schedule.interval_minutes)
    await run_scheduler(
        interval_seconds,
        lambda: _run_monitor_process(config.config_path),
        stop,
    )
    LOG.info("Scheduler stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run IG Monitor continuously")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    try:
        asyncio.run(_async_main(Path(args.config).expanduser().resolve()))
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
