"""Launch waves for subagents (delegation.launch_wave_size / delegation.launch_wave_seconds).

80 children whose first requests land in the same second trip a provider's ramp limit (Anthropic 429s),
so starts are paced: at most N per window across the process, the rest wait for their wave.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from hermes_constants import get_hermes_home
from tests.waiting import wait_until
from tools import delegate_tool_launch_waves as waves
from tools.delegate_tool_child_run import _attach_child
from tools.delegate_tool_dispatch import _run_children_parallel


def _config(extra: str = "") -> None:
    (get_hermes_home() / "config.yaml").write_text("delegation:\n  max_concurrent_children: 80\n" + extra,
                                                   encoding="utf-8")


def test_reservations_fill_one_wave_per_window_in_launch_order():
    now = [100.0]
    pacer = waves.LaunchPacer(clock=lambda: now[0])
    assert [pacer.reserve(size=2, window_s=10) for _ in range(5)] == [100, 100, 110, 110, 120]
    now[0] = 135.0  # every wave has gone: the next launch starts at once
    assert pacer.reserve(size=2, window_s=10) == 135


def test_waves_off_by_default_and_a_child_stopped_in_its_wave_wait_never_starts(monkeypatch):
    monkeypatch.setattr(waves, "_PACER", waves.LaunchPacer())
    _config()
    child = SimpleNamespace(_interrupt_requested=False)
    assert waves.wait_for_launch_wave(child) is True  # unset = today's behaviour, no wait
    _config("  launch_wave_size: 1\n  launch_wave_seconds: 30\n")
    assert waves.wait_for_launch_wave(child) is True  # first slot of the wave
    stopped = SimpleNamespace(_interrupt_requested=True)
    t0 = time.monotonic()
    assert waves.wait_for_launch_wave(stopped) is False  # its wave is 30 s out; the stop wins at once
    assert time.monotonic() - t0 < 5


class _RampLimitedProvider:
    """A fake provider with a ramp limit: a request is a 429 when more than ``burst`` started within ``window_s``."""

    def __init__(self, burst: int, window_s: float) -> None:
        self.burst, self.window_s, self.lock, self.starts = burst, window_s, threading.Lock(), []

    def request(self) -> int:
        with self.lock:
            now = time.monotonic()
            self.starts.append(now)
            return 429 if sum(now - t < self.window_s for t in self.starts) > self.burst else 200


def _run_batch(n: int, provider: _RampLimitedProvider) -> list:
    parent = SimpleNamespace(_interrupt_requested=False, _delegate_spinner=None, _delegate_depth=0,
                             _active_children=[], _active_children_lock=threading.Lock())
    tasks = [{"goal": f"g{i}"} for i in range(n)]
    kids = [SimpleNamespace(_interrupt_requested=False, _delegate_role="leaf", provider="x", base_url="",
                            model="m", api_mode="", _use_prompt_caching=False) for _ in range(n)]

    def run_child(i, task, child):
        code = provider.request()
        return {"task_index": i, "status": "completed" if code == 200 else "error", "summary": str(code),
                "api_calls": 1, "duration_seconds": 0}

    batch = SimpleNamespace(parent_agent=parent, task_list=tasks, children=[(i, tasks[i], c) for i, c in enumerate(kids)],
                            live_deleg_id=None, max_children=n, unit_id=None, live_paths=[], run_child=run_child)
    results: list = []
    _run_children_parallel(batch, results, honor_parent_interrupt=True)
    return results


@pytest.mark.parametrize("waves_on", [False, True])
def test_a_wide_batch_trips_a_ramp_limit_unless_it_launches_in_waves(monkeypatch, waves_on):
    monkeypatch.setattr(waves, "_PACER", waves.LaunchPacer())
    _config("  launch_wave_size: 4\n  launch_wave_seconds: 0.5\n" if waves_on else "")
    provider = _RampLimitedProvider(burst=4, window_s=0.25)
    results = _run_batch(12, provider)
    rejected = [r for r in results if r["summary"] == "429"]
    if not waves_on:
        assert rejected  # 12 simultaneous first requests: the provider refuses the ramp
        return
    assert rejected == [] and len(results) == 12
    starts = sorted(provider.starts)
    assert starts[4] - starts[3] >= 0.4 and starts[8] - starts[7] >= 0.4  # three waves of four


def test_a_foreground_child_waiting_for_its_wave_is_released_when_the_parent_stops(monkeypatch):
    monkeypatch.setattr(waves, "_PACER", waves.LaunchPacer())
    _config("  launch_wave_size: 1\n  launch_wave_seconds: 60\n")
    parent = SimpleNamespace(_interrupt_requested=False, _delegate_spinner=None, _delegate_depth=0,
                             _active_children=[], _active_children_lock=threading.Lock())
    closed, ran = [], []
    kids = [SimpleNamespace(_interrupt_requested=False, _delegate_role="leaf", provider="x", base_url="", model="m",
                            api_mode="", _use_prompt_caching=False, close=lambda n=n: closed.append(n)) for n in (0, 1)]
    for c in kids:
        _attach_child(parent, c)

    def run_child(i, task, child):
        ran.append(i)
        parent._interrupt_requested = True  # the user stops the turn while child 1 waits 60 s for its wave
        wait_until(lambda: closed, timeout=5)
        return {"task_index": i, "status": "completed", "summary": "x", "api_calls": 1, "duration_seconds": 0}

    tasks = [{"goal": "g0"}, {"goal": "g1"}]
    batch = SimpleNamespace(parent_agent=parent, task_list=tasks, children=[(0, tasks[0], kids[0]), (1, tasks[1], kids[1])],
                            live_deleg_id=None, max_children=2, unit_id=None, live_paths=[], run_child=run_child)
    t0 = time.monotonic()
    _run_children_parallel(batch, [], honor_parent_interrupt=True)
    wait_until(lambda: len(closed) == 1, timeout=10)
    assert len(ran) == 1 and closed[0] != ran[0]  # whichever child got the wave ran; the other never started
    assert time.monotonic() - t0 < 30
