"""Per-process cap on running in-process subagents (delegation.max_in_process_children).

Every delegate_task child runs as threads of the process that runs its parent's turn; in the gateway that is the
process that also owns the event loop for every profile. On 24 Nov 2026 a Telegram orchestration chat's ~26
in-process children (depth 1 + 2) plus ~8 top-level turns starved the loop until the liveness watchdog exited the
gateway (~/.hermes/work/gateway-loop-stall/lane-subagents.md). The cap bounds RUNNING children per process across
every parent and depth; overflow children queue (bounded, stop-aware) and every result still returns; a parent
blocked on its own children lends its slot, so nested orchestration cannot deadlock the pool.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from hermes_constants import get_hermes_home
from tests.waiting import wait_until
from tools import delegate_tool_launch_waves as waves
from tools import delegate_tool_process_slots as slots
from tools.delegate_tool_child_run import _attach_child
from tools.delegate_tool_dispatch import _run_batch_children


def _config(extra: str = "") -> None:
    (get_hermes_home() / "config.yaml").write_text("delegation:\n  max_concurrent_children: 80\n" + extra, encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh_pools(monkeypatch):
    monkeypatch.setattr(slots, "_SLOTS", slots.InProcessChildSlots())
    monkeypatch.setattr(waves, "_SCHEDULES", waves.LaunchSchedules())


class _Load:
    """Counts children whose run() is executing at once (the in-process agent loops)."""

    def __init__(self) -> None:
        self.lock, self.now, self.peak, self.ran = threading.Lock(), 0, 0, []

    def run(self, i, hold_s=0.15):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        time.sleep(hold_s)
        with self.lock:
            self.now -= 1
            self.ran.append(i)


def _agent(depth=0, **kw):
    return SimpleNamespace(_interrupt_requested=False, _delegate_spinner=None, _delegate_depth=depth,
                           _delegate_role="leaf", provider="x", base_url="", model="m", api_mode="",
                           _use_prompt_caching=False, _active_children=[], _active_children_lock=threading.Lock(), **kw)


def _batch(parent, n, run_child):
    tasks = [{"goal": f"g{i}"} for i in range(n)]
    kids = [_agent(depth=getattr(parent, "_delegate_depth", 0) + 1) for _ in range(n)]
    return SimpleNamespace(parent_agent=parent, task_list=tasks, children=[(i, tasks[i], c) for i, c in enumerate(kids)],
                           live_deleg_id=None, max_children=n, unit_id=None, live_paths=[], run_child=run_child)


def _entry(i):
    return {"task_index": i, "status": "completed", "summary": f"r{i}", "api_calls": 1, "duration_seconds": 0}


def test_concurrent_batches_never_run_more_children_than_the_process_cap():
    """Two parents (e.g. two gateway turns) launch 7 + 5 children at once: at most 3 run in-process, all 12 return."""
    _config("  max_in_process_children: 3\n")
    load = _Load()

    def run_child(i, task, child):
        load.run(i)
        return _entry(i)

    out: dict = {}
    parents = [_agent(), _agent()]
    threads = [threading.Thread(target=lambda k=k, n=n: _run_batch_children(
        _batch(parents[k], n, run_child), out.setdefault(k, []), honor_parent_interrupt=True)) for k, n in ((0, 7), (1, 5))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert load.peak <= 3, f"{load.peak} children ran in-process at once over a cap of 3"
    assert sorted(r["summary"] for r in out[0]) == [f"r{i}" for i in range(7)]
    assert sorted(r["summary"] for r in out[1]) == [f"r{i}" for i in range(5)]
    assert all(r["status"] == "completed" for r in out[0] + out[1])
    assert slots._SLOTS.running() == 0 and slots._SLOTS.waiting() == 0


def test_zero_keeps_todays_uncapped_behaviour():
    _config("  max_in_process_children: 0\n")
    load = _Load()
    gate = threading.Barrier(6, timeout=10)

    def run_child(i, task, child):
        gate.wait()  # all 6 must be running at once, or the barrier breaks
        load.run(i, 0)
        return _entry(i)

    results: list = []
    _run_batch_children(_batch(_agent(), 6, run_child), results, honor_parent_interrupt=True)
    assert [r["status"] for r in results] == ["completed"] * 6 and load.peak >= 1


def test_nested_orchestrators_lend_their_slot_and_never_deadlock():
    """3 depth-1 orchestrators each join 3 depth-2 leaves under a cap of 2: without the lend the orchestrators hold
    both slots while their leaves queue (deadlock until the wait bound refuses them)."""
    _config("  max_in_process_children: 2\n  in_process_wait_seconds: 20\n")
    load = _Load()

    def leaf(i, task, child):
        load.run(i, 0.05)
        return _entry(i)

    def orchestrator(i, task, child):
        load.run(("orch", i), 0.02)  # its own model turn
        leaves: list = []
        _run_batch_children(_batch(child, 3, leaf), leaves, honor_parent_interrupt=True)
        load.run(("orch", i), 0.02)  # reads the results back, holding its slot again
        return {**_entry(i), "summary": ",".join(sorted(r["status"] for r in leaves))}

    results: list = []
    worker = threading.Thread(target=lambda: _run_batch_children(_batch(_agent(), 3, orchestrator), results,
                                                                  honor_parent_interrupt=True), daemon=True)
    worker.start()
    worker.join(timeout=15)
    assert not worker.is_alive(), "nested orchestration deadlocked on the in-process cap"
    assert [r["summary"] for r in results] == ["completed,completed,completed"] * 3
    assert load.peak <= 2
    assert slots._SLOTS.running() == 0 and slots._SLOTS.waiting() == 0


def test_a_queued_child_is_refused_after_the_wait_bound_with_a_reason_and_never_runs():
    _config("  max_in_process_children: 1\n  in_process_wait_seconds: 0.3\n")
    release = threading.Event()
    parent = _agent()
    closed, ran = [], []
    kids = [_agent(close=lambda n=n: closed.append(n)) for n in (0, 1)]
    for c in kids:
        _attach_child(parent, c)

    def run_child(i, task, child):
        ran.append(i)
        release.wait(5)  # the first child holds the only slot past the second one's wait bound
        return _entry(i)

    tasks = [{"goal": "g0"}, {"goal": "g1"}]
    batch = SimpleNamespace(parent_agent=parent, task_list=tasks, children=[(0, tasks[0], kids[0]), (1, tasks[1], kids[1])],
                            live_deleg_id=None, max_children=2, unit_id=None, live_paths=[], run_child=run_child)
    results: list = []
    runner = threading.Thread(target=lambda: _run_batch_children(batch, results, honor_parent_interrupt=True), daemon=True)
    runner.start()
    wait_until(lambda: len(closed) == 1, timeout=10)  # the refused child is released unstarted
    release.set()
    runner.join(timeout=10)
    assert len(ran) == 1
    refused = next(r for r in results if r["task_index"] == closed[0])
    assert refused["status"] == "failed" and "max_in_process_children=1" in refused["error"]
    assert slots._SLOTS.running() == 0


def test_a_child_queued_for_a_slot_is_released_when_the_parent_stops():
    _config("  max_in_process_children: 1\n")
    parent = _agent()
    closed, ran = [], []
    kids = [_agent(close=lambda n=n: closed.append(n)) for n in (0, 1)]
    for c in kids:
        _attach_child(parent, c)

    def run_child(i, task, child):
        ran.append(i)
        wait_until(lambda: slots._SLOTS.waiting() == 1, timeout=5)
        parent._interrupt_requested = True  # the user stops the turn while the other child waits for a slot
        wait_until(lambda: closed, timeout=5)
        return _entry(i)

    tasks = [{"goal": "g0"}, {"goal": "g1"}]
    batch = SimpleNamespace(parent_agent=parent, task_list=tasks, children=[(0, tasks[0], kids[0]), (1, tasks[1], kids[1])],
                            live_deleg_id=None, max_children=2, unit_id=None, live_paths=[], run_child=run_child)
    _run_batch_children(batch, [], honor_parent_interrupt=True)
    wait_until(lambda: len(closed) == 1, timeout=10)
    assert len(ran) == 1 and closed[0] != ran[0]  # the queued child never started
    wait_until(lambda: slots._SLOTS.running() == 0, timeout=10)


def test_a_lend_still_open_when_its_run_is_abandoned_never_takes_a_slot_back():
    """A child abandoned by its parent (interrupt / stale collection) can still be inside its own join: its slot is
    freed when its run ends, and the lend's exit must not take one again (a leak would shrink the pool forever)."""
    pool = slots._SLOTS
    child = _agent(depth=1)
    assert pool.admit(child, 2, 0, lambda: False) is True and pool.running() == 1
    pool.lend(child)
    assert pool.running() == 0
    pool.release(child)  # its parent gave up on it: run_child_start's finally
    pool.take_back(child, 2, lambda: False)
    assert pool.running() == 0 and pool.waiting() == 0


def test_a_returning_parent_is_served_before_new_launches():
    pool = slots._SLOTS
    holder, parent, newcomer = _agent(depth=1), _agent(depth=1), _agent(depth=1)
    assert pool.admit(parent, 1, 0, lambda: False)
    pool.lend(parent)
    assert pool.admit(holder, 1, 0, lambda: False)  # its child took the lent slot
    order: list = []
    t_new = threading.Thread(target=lambda: (pool.admit(newcomer, 1, 0, lambda: False), order.append("new")))
    t_new.start()
    wait_until(lambda: pool.waiting() == 1, timeout=5)
    t_back = threading.Thread(target=lambda: (pool.take_back(parent, 1, lambda: False), order.append("parent")))
    t_back.start()
    wait_until(lambda: pool.waiting() == 2, timeout=5)
    pool.release(holder)
    wait_until(lambda: order, timeout=5)
    assert order == ["parent"]
    pool.release(parent)
    t_new.join(timeout=5)
    t_back.join(timeout=5)
    assert order == ["parent", "new"]
    pool.release(newcomer)
    assert pool.running() == 0


def test_a_background_unit_queued_for_a_slot_is_not_read_as_stalled():
    """The async stale monitor interrupts a unit whose progress token freezes for 450 s; a child queued for an
    in-process slot must keep the token moving, or a busy gateway would cancel every queued background unit."""
    from tools.delegate_tool_dispatch import _batch_progress_token

    class _Clock:
        now = 1000.0

        def __call__(self):
            return self.now

    clock = _Clock()
    pool = slots.InProcessChildSlots(clock=clock)
    holder = _agent(depth=1)
    queued = _agent(depth=1, get_activity_summary=lambda: {"api_call_count": 0, "current_tool": None,
                                                             "last_activity_ts": None})
    assert pool.admit(holder, 1, 0, lambda: False)
    t = threading.Thread(target=lambda: pool.admit(queued, 1, 0, lambda: False), daemon=True)
    t.start()
    wait_until(lambda: getattr(queued, "_delegate_process_slot_waiting", None) == 1000, timeout=5)
    before = _batch_progress_token([queued])
    clock.now = 1460.0
    wait_until(lambda: getattr(queued, "_delegate_process_slot_waiting", None) == 1460, timeout=5)
    assert _batch_progress_token([queued]) != before
    pool.release(holder)
    t.join(timeout=5)
    assert getattr(queued, "_delegate_process_slot_waiting", None) is None and pool.running() == 1


def test_the_defaults_are_a_bounded_cap_and_wait():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from tools.delegate_tool_config import _get_in_process_children_limits
    _config()
    assert _get_in_process_children_limits() == (10, 1800.0)
    assert DEFAULT_CONFIG["delegation"]["max_in_process_children"] == 10
    assert DEFAULT_CONFIG["delegation"]["in_process_wait_seconds"] == 1800
