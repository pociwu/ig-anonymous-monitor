import asyncio
from ig_monitor.scheduler import run_split_scheduler
from ig_monitor.cli import build_parser


def test_download_repeats_without_repeating_inspection():
    async def scenario():
        stop = asyncio.Event()
        calls = []
        async def inspect():
            calls.append('inspect')
            return 1
        async def download():
            calls.append('download')
            if calls.count('download') == 3:
                stop.set()
            return 0
        await run_split_scheduler(60, inspect, download, stop, download_interval=.001)
        assert calls == ['download', 'inspect', 'download', 'download']
    asyncio.run(scenario())


def test_download_exception_does_not_stop_independent_clocks():
    async def scenario():
        stop = asyncio.Event()
        calls = []
        async def download():
            calls.append('download')
            if len(calls) == 1:
                raise RuntimeError('test')
            stop.set()
            return 0
        async def inspect():
            calls.append('inspect')
            return 0
        await run_split_scheduler(60, inspect, download, stop, download_interval=.001)
        assert calls == ['download', 'inspect', 'download']
    asyncio.run(scenario())


def test_inspect_only_option():
    assert build_parser().parse_args(['--inspect-only']).inspect_only
