import asyncio
import unittest

from ig_monitor.scheduler import run_scheduler


class SchedulerTests(unittest.TestCase):
    def test_scheduler_repeats_until_stopped(self):
        async def scenario():
            stop = asyncio.Event()
            calls = []

            async def run_once():
                calls.append(len(calls) + 1)
                if len(calls) == 2:
                    stop.set()
                return 0

            await run_scheduler(0.001, run_once, stop)
            return calls

        self.assertEqual(asyncio.run(scenario()), [1, 2])

    def test_scheduler_retries_after_unexpected_failure(self):
        async def scenario():
            stop = asyncio.Event()
            calls = []

            async def run_once():
                calls.append(len(calls) + 1)
                if len(calls) == 1:
                    raise RuntimeError("temporary failure")
                stop.set()
                return 0

            await run_scheduler(0.001, run_once, stop)
            return calls

        self.assertEqual(asyncio.run(scenario()), [1, 2])


if __name__ == "__main__":
    unittest.main()
