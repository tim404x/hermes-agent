"""Launch waves for subagents (delegation.launch_wave_size / delegation.launch_wave_seconds).

80 children whose first requests land in the same second trip a provider's ramp limit (Anthropic 429s),
so starts are paced: at most N per window, the rest wait for their wave in launch order. Each profile and
provider account keeps its own schedule, a waiter that is stopped never holds a window, and admission is
decided from ACTUAL starts (adversarial review, 19 Nov 2026: ghost waves, cross-profile coupling, late
waiters collapsing into one burst, lifecycle launches bypassing the pacer and the fan-out cap).
"""

import itertools
import threading
from types import SimpleNamespace

import pytest

from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from tests.waiting import wait_until
from tools import delegate_tool_launch_waves as waves
from tools.delegate_tool_child_run import _attach_child
from tools.delegate_tool_dispatch import _run_children_parallel


def _config(extra: str = "", home=None) -> None:
    ((home or get_hermes_home()) / "config.yaml").write_text("delegation:\n  max_concurrent_children: 80\n" + extra,
                                                             encoding="utf-8")


class _Clock:
    """An injected monotonic clock: tests move it, nothing sleeps on it."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _waiter(pacer, size, window_s, stopped=lambda: False):
    """Run ``wait_turn`` on a thread; ``(thread, outcome)``, outcome receives its bool."""
    outcome: list = []
    thread = threading.Thread(target=lambda: outcome.append(pacer.wait_turn(size, window_s, stopped)), daemon=True)
    thread.start()
    return thread, outcome


def test_admissions_fill_one_wave_per_window_in_launch_order():
    clock = _Clock(100.0)
    pacer = waves.LaunchPacer(clock=clock)
    assert pacer.wait_turn(2, 10, lambda: False) and pacer.wait_turn(2, 10, lambda: False)
    thread, outcome = _waiter(pacer, 2, 10)
    wait_until(lambda: pacer.waiting() == 1, timeout=5)
    assert outcome == []  # the wave is full: held, not admitted
    clock.now = 110.0
    thread.join(timeout=5)
    assert outcome == [True] and pacer.started() == 3 and pacer._starts[-1] == 110.0
    clock.now = 135.0  # every wave has gone: the next launch starts at once
    assert pacer.wait_turn(2, 10, lambda: False) and pacer._starts[-1] == 135.0


def test_waiters_stopped_at_once_never_hold_a_later_window():
    clock = _Clock()
    pacer = waves.LaunchPacer(clock=clock)
    assert pacer.wait_turn(1, 10, lambda: False)
    for _ in range(79):  # a cancelled 80-wide swarm
        assert pacer.wait_turn(1, 10, lambda: True) is False
    clock.now = 10.0
    assert pacer.wait_turn(1, 10, lambda: False) and list(pacer._starts) == [10.0]


def test_waiters_cancelled_while_queued_free_their_place():
    clock = _Clock()
    pacer = waves.LaunchPacer(clock=clock)
    assert pacer.wait_turn(1, 10, lambda: False)
    stop = threading.Event()
    queued = [_waiter(pacer, 1, 10, stop.is_set) for _ in range(5)]
    wait_until(lambda: pacer.waiting() == 5, timeout=5)
    stop.set()  # the swarm is stopped while five children wait for their waves
    for thread, outcome in queued:
        thread.join(timeout=5)
        assert outcome == [False]
    assert pacer.waiting() == 0
    clock.now = 10.0  # the next delegation is not pushed behind five cancelled waves (it was 60)
    assert pacer.wait_turn(1, 10, lambda: False) and pacer._starts[-1] == 10.0


def test_late_waiters_are_still_admitted_one_window_apart():
    clock = _Clock()
    pacer = waves.LaunchPacer(clock=clock)
    assert pacer.wait_turn(1, 10, lambda: False)
    late = [_waiter(pacer, 1, 10) for _ in range(2)]
    wait_until(lambda: pacer.waiting() == 2, timeout=5)
    clock.now = 20.0  # both waiters wake late (a process pause): only ONE may start now
    wait_until(lambda: pacer.waiting() == 1, timeout=5)
    assert sum(len(outcome) for _, outcome in late) == 1 and pacer._starts[-1] == 20.0
    clock.now = 30.0
    for thread, outcome in late:
        thread.join(timeout=5)
        assert outcome == [True]
    assert pacer._starts[-1] == 30.0


class _Child:
    def __init__(self, stop: "threading.Event | None" = None, **route) -> None:
        self._stop = stop or threading.Event()
        self.provider, self.base_url, self.api_key = "anthropic", "https://api.anthropic.com", "k-1"
        self.__dict__.update(route)

    @property
    def _interrupt_requested(self) -> bool:
        return self._stop.is_set()


def test_profiles_and_provider_accounts_keep_separate_schedules(tmp_path, monkeypatch):
    monkeypatch.setattr(waves, "_SCHEDULES", waves.LaunchSchedules())
    homes = []
    for name in ("profile-a", "profile-b"):
        home = tmp_path / name
        home.mkdir()
        _config("  launch_wave_size: 1\n  launch_wave_seconds: 300\n", home)
        homes.append(home)

    def admitted(home, child) -> bool:
        token = set_hermes_home_override(home)
        try:
            return waves.wait_for_launch_wave(child)
        finally:
            reset_hermes_home_override(token)

    assert admitted(homes[0], _Child())
    # Another profile on the same account: its own schedule, so it is not held 300 s behind profile A.
    assert admitted(homes[1], _Child())
    # Same profile, another provider account (another key or endpoint): its own schedule too.
    assert admitted(homes[0], _Child(api_key="k-2"))
    assert admitted(homes[0], _Child(base_url="https://openrouter.ai/api/v1"))
    # Same profile, same account: held for its 300 s window (released here by its own stop).
    stop = threading.Event()
    held = threading.Thread(target=lambda: outcome.append(admitted(homes[0], _Child(stop))), daemon=True)
    outcome: list = []
    held.start()
    wait_until(lambda: waves._SCHEDULES.waiting() == 1, timeout=5)
    stop.set()
    held.join(timeout=5)
    assert outcome == [False]


def test_waves_off_by_default_and_a_child_stopped_in_its_wave_wait_never_starts(monkeypatch):
    monkeypatch.setattr(waves, "_SCHEDULES", waves.LaunchSchedules())
    _config()
    child = SimpleNamespace(_interrupt_requested=False)
    assert waves.wait_for_launch_wave(child) is True  # unset = today's behaviour, no wait
    _config("  launch_wave_size: 1\n  launch_wave_seconds: 30\n")
    assert waves.wait_for_launch_wave(child) is True  # first slot of the wave
    stopped = SimpleNamespace(_interrupt_requested=True)
    assert waves.wait_for_launch_wave(stopped) is False  # its wave is 30 s out; the stop wins at once


class _RampLimitedProvider:
    """A fake provider with a ramp limit: a request is a 429 when more than ``burst`` started within ``window_s``
    on the injected clock."""

    def __init__(self, burst: int, window_s: float, clock: _Clock) -> None:
        self.burst, self.window_s, self.clock, self.lock, self.starts = burst, window_s, clock, threading.Lock(), []

    def request(self) -> int:
        with self.lock:
            now = self.clock()
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
    clock = _Clock()
    monkeypatch.setattr(waves, "_SCHEDULES", waves.LaunchSchedules(clock=clock))
    _config("  launch_wave_size: 4\n  launch_wave_seconds: 10\n" if waves_on else "")
    provider = _RampLimitedProvider(burst=4, window_s=5, clock=clock)
    if not waves_on:
        rejected = [r for r in _run_batch(12, provider) if r["summary"] == "429"]
        assert rejected  # 12 simultaneous first requests: the provider refuses the ramp
        return
    results: list = []
    runner = threading.Thread(target=lambda: results.extend(_run_batch(12, provider)), daemon=True)
    runner.start()
    # Move the injected clock only while the system is quiet: every admitted child has sent its request and
    # every other child waits for its wave. No assertion depends on real time.
    while len(provider.starts) < 12:
        wait_until(lambda: waves._SCHEDULES.waiting() + len(provider.starts) == 12, timeout=10)
        if len(provider.starts) < 12:
            clock.now += 10
    runner.join(timeout=10)
    assert sorted(r["summary"] for r in results) == ["200"] * 12
    starts = sorted(provider.starts)
    assert all(starts[i + 4] - starts[i] >= 10 for i in range(8))  # never more than 4 starts in one window


def test_a_foreground_child_waiting_for_its_wave_is_released_when_the_parent_stops(monkeypatch):
    monkeypatch.setattr(waves, "_SCHEDULES", waves.LaunchSchedules())
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
    _run_children_parallel(batch, [], honor_parent_interrupt=True)
    wait_until(lambda: len(closed) == 1, timeout=10)
    assert len(ran) == 1 and closed[0] != ran[0]  # whichever child got the wave ran; the other never started


# --- the public lifecycle API (ctx.subagent_lifecycle.launch) starts children through the same boundary ---

def _lifecycle(monkeypatch, run=None):
    """The real lifecycle service with only child construction and the model run stubbed."""
    from agent.subagent_lifecycle import SubagentLifecycleService
    from tests.agent.test_subagent_lifecycle import FakeChild
    from tools import delegate_tool

    class Child(FakeChild):
        _interrupt_requested = property(lambda self: self.interrupted)

        def __init__(self, ident):
            super().__init__(ident)
            self.closed = False

        def close(self):
            self.closed = True

    built, ids = [], itertools.count()

    def build(**_kw):
        built.append(Child(f"sa-lifecycle-{next(ids)}"))
        return built[-1]

    monkeypatch.setattr(delegate_tool, "_build_child_agent", build)
    monkeypatch.setattr(delegate_tool, "_run_single_child", run or (lambda *a, **k: {
        "task_index": 0, "status": "completed", "summary": "done", "api_calls": 1, "duration_seconds": 0}))
    return SubagentLifecycleService, built


def test_lifecycle_launches_are_paced_once_per_child(monkeypatch):
    from agent.subagent_lifecycle import SubagentLaunchRequest
    _config("  launch_wave_size: 1\n  launch_wave_seconds: 300\n")
    clock = _Clock()
    monkeypatch.setattr(waves, "_SCHEDULES", waves.LaunchSchedules(clock=clock))
    service_cls, _built = _lifecycle(monkeypatch)
    parent = SimpleNamespace(session_id="lifecycle-parent", enabled_toolsets=["file"])
    service = service_cls(lambda: parent)
    first = service.launch(SubagentLaunchRequest(goal="Fetch title"))
    assert service.wait(first, timeout_seconds=5).completed
    assert waves._SCHEDULES.started() == 1  # admitted exactly once
    second = service.launch(SubagentLaunchRequest(goal="Fetch another title"))
    wait_until(lambda: waves._SCHEDULES.waiting() == 1, timeout=5)  # held: the 300 s wave is full
    clock.now = 300.0
    assert service.wait(second, timeout_seconds=5).completed
    assert waves._SCHEDULES.started() == 2


def test_a_lifecycle_child_stopped_in_its_wave_wait_is_released_unstarted(monkeypatch):
    from agent.subagent_lifecycle import SubagentLaunchRequest
    _config("  launch_wave_size: 1\n  launch_wave_seconds: 300\n")
    monkeypatch.setattr(waves, "_SCHEDULES", waves.LaunchSchedules())
    ran = []
    service_cls, built = _lifecycle(monkeypatch, run=lambda *a, **k: ran.append(1) or {
        "task_index": 0, "status": "completed", "summary": "done", "api_calls": 1, "duration_seconds": 0})
    parent = SimpleNamespace(session_id="lifecycle-parent", enabled_toolsets=["file"])
    service = service_cls(lambda: parent)
    assert service.wait(service.launch(SubagentLaunchRequest(goal="Fetch title")), timeout_seconds=5).completed
    held = service.launch(SubagentLaunchRequest(goal="Fetch another title"))
    wait_until(lambda: waves._SCHEDULES.waiting() == 1, timeout=5)
    service.cancel(held, reason="user stop")
    assert service.wait(held, timeout_seconds=5).completed
    assert ran == [1] and built[1].closed  # the held child never ran, and its resources were released


def test_a_subagents_lifecycle_launches_obey_the_fan_out_cap(monkeypatch):
    from agent.subagent_lifecycle import SubagentLaunchRequest
    _config("  max_children_per_child: 1\n")
    release, ran = threading.Event(), []

    def run(*a, **k):
        ran.append(1)
        release.wait(5)
        return {"task_index": 0, "status": "completed", "summary": "done", "api_calls": 1, "duration_seconds": 0}

    service_cls, built = _lifecycle(monkeypatch, run=run)
    parent = SimpleNamespace(session_id="orchestrator-child", enabled_toolsets=["file"], _delegate_depth=1)
    service = service_cls(lambda: parent)
    first = service.launch(SubagentLaunchRequest(goal="Fetch title"))
    wait_until(lambda: ran, timeout=5)
    over = service.launch(SubagentLaunchRequest(goal="Fetch another title"))
    assert service.wait(over, timeout_seconds=5).completed
    assert ran == [1] and built[1].closed  # a subagent's second live child never started
    assert "max_children_per_child" in (service.result(over).error_message or "")
    release.set()
    assert service.wait(first, timeout_seconds=5).completed
    third = service.launch(SubagentLaunchRequest(goal="Fetch a third title"))  # the slot is free again
    assert service.wait(third, timeout_seconds=5).completed and ran == [1, 1]
