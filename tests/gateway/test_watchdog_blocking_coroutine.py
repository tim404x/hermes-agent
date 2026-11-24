"""Attribute a liveness dump to the executing coroutine, not other waiting tasks."""
import asyncio
import threading

import pytest

from gateway.shutdown_watchdog import _write_watchdog_dump


@pytest.mark.parametrize('callback_only', [False, True], ids=['nested-coroutine', 'plain-callback'])
def test_watchdog_names_the_executing_coroutine_without_asking_the_frozen_loop(tmp_path, capsys, callback_only):
    path = tmp_path / 'watchdog.log'
    entered, release = threading.Event(), threading.Event()

    def sync_read():
        entered.set()
        release.wait(5)

    async def flush_completions():
        sync_read()

    async def main():
        if callback_only:
            loop = asyncio.get_running_loop()
            done = loop.create_future()
            loop.call_soon(lambda: (sync_read(), done.set_result(None)))
            await done
        else:
            await flush_completions()

    thread = threading.Thread(target=asyncio.run, args=(main(),), daemon=True)
    thread.start()
    try:
        assert entered.wait(5)
        _write_watchdog_dump(path, delay_s=1, snapshot={}, loop_thread_id=thread.ident)
        expected = ('monitored loop: no executing Python coroutine' if callback_only
                    else f'monitored loop: executing coroutine {flush_completions.__qualname__} ')
        assert expected in path.read_text()
        assert expected in capsys.readouterr().err
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
