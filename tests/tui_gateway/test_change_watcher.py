"""The generalized change watcher (#73618): cheap on-disk signatures →
``pet.changed`` / ``cron.changed`` / ``sessions.changed`` global broadcasts.

Behavior contracts, exercised against a real temp HERMES_HOME (no mocks on the
filesystem path): first sighting seeds silently, a moved signature broadcasts
once, the sessions floor coalesces a write burst but keeps its trailing edge,
and the pet signature only moves for a *renderable* pet.
"""

import os
import sqlite3
import time

import pytest

from tui_gateway import server


@pytest.fixture()
def watcher_home(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("display: {}\n")
    (tmp_path / "cron").mkdir()

    monkeypatch.setattr(server, "_hermes_home", str(tmp_path))
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_change_sigs", {})
    monkeypatch.setattr(server, "_change_checked_at", {})
    monkeypatch.setattr(server, "_change_broadcast_at", {})
    monkeypatch.setattr(server, "_bot_relay_outbox_seen", 0)
    monkeypatch.setattr(server, "_pairing_roots_cache", None, raising=False)
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})

    events = []
    monkeypatch.setattr(
        server, "_broadcast_global_event", lambda ev, payload=None: events.append((ev, payload))
    )
    return tmp_path, events


def _write_session_change(db_path, title):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions "
        "(id TEXT PRIMARY KEY, title TEXT, last_activity_at REAL, message_count INTEGER)"
    )
    conn.execute(
        "INSERT INTO sessions(id, title, last_activity_at, message_count) VALUES ('s1', ?, 1, 1) "
        "ON CONFLICT(id) DO UPDATE SET title = excluded.title",
        (title,),
    )
    conn.commit()
    conn.close()


def test_first_sighting_seeds_without_broadcasting(watcher_home):
    home, events = watcher_home
    (home / "cron" / "jobs.json").write_text("[]")
    (home / "state.db").write_text("x")

    server._broadcast_watched_changes(now=0.0)

    assert events == []


def test_cron_jobs_file_move_broadcasts_cron_changed(watcher_home):
    home, events = watcher_home
    server._broadcast_watched_changes(now=0.0)

    (home / "cron" / "jobs.json").write_text("[]")
    server._broadcast_watched_changes(now=10.0)

    assert ("cron.changed", {}) in events


def test_state_db_move_broadcasts_sessions_changed(watcher_home):
    home, events = watcher_home
    server._broadcast_watched_changes(now=0.0)

    _write_session_change(home / "state.db", "created")
    server._broadcast_watched_changes(now=10.0)

    assert ("sessions.changed", {}) in events


def _seed_store(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL, "
        "message_count INTEGER, last_activity_at REAL, last_activity_description TEXT);"
        "CREATE TABLE gateway_heartbeats (backend_id TEXT PRIMARY KEY, last_heartbeat REAL);"
        "INSERT INTO sessions VALUES ('s1', 'hello', 1, 3, 1, NULL);"
        "INSERT INTO gateway_heartbeats VALUES ('backend-1', 1);"
    )
    conn.commit()
    conn.close()


def _write(db_path, sql, *params):
    conn = sqlite3.connect(db_path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def test_activity_heartbeats_do_not_broadcast_sessions_changed(watcher_home):
    """#98005: the gateway heartbeat and the session activity stamp rewrite state.db every
    minute with no session change. Each used to fire sessions.changed, so the Desktop
    Sessions panel refreshed on its own every heartbeat window."""
    home, events = watcher_home
    db = home / "state.db"
    _seed_store(db)
    server._broadcast_watched_changes(now=0.0)

    for tick in range(1, 4):
        time.sleep(0.02)
        _write(db, "UPDATE gateway_heartbeats SET last_heartbeat = ?", tick * 60.0)
        _write(db, "UPDATE sessions SET last_activity_at = ?, last_activity_description = ? "
                   "WHERE id = 's1'", tick * 60.0, f"tool {tick}")
        server._broadcast_watched_changes(now=tick * 10.0)

    assert ("sessions.changed", {}) not in events


@pytest.mark.parametrize("sql", [
    "INSERT INTO sessions VALUES ('s2', 'new', 2, 0, 2, NULL)",
    "UPDATE sessions SET title = 'renamed' WHERE id = 's1'",
    "UPDATE sessions SET message_count = 4 WHERE id = 's1'",
    "DELETE FROM sessions WHERE id = 's1'",
])
def test_session_row_changes_broadcast_sessions_changed(watcher_home, sql):
    home, events = watcher_home
    db = home / "state.db"
    _seed_store(db)
    server._broadcast_watched_changes(now=0.0)

    time.sleep(0.02)
    _write(db, sql)
    server._broadcast_watched_changes(now=10.0)

    assert ("sessions.changed", {}) in events


def _seed_mixed_store(db_path):
    """A desktop chat, a Telegram chat, a one-shot worker run, the worker's delegate run and the chat's delegate run,
    as the live stores hold them."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, model_config TEXT, "
        "title TEXT, message_count INTEGER, tool_call_count INTEGER, last_activity_at REAL);"
        "INSERT INTO sessions VALUES ('chat', 'desktop', NULL, NULL, 'Chat', 2, 0, 1);"
        "INSERT INTO sessions VALUES ('tg', 'telegram', NULL, NULL, 'Telegram', 4, 0, 1);"
        "INSERT INTO sessions VALUES ('job', 'oneshot', NULL, NULL, 'Job', 1, 0, 1);"
        "INSERT INTO sessions VALUES ('sub', 'subagent', 'chat', '{\"_delegate_from\": \"chat\"}', 'Sub', 1, 0, 1);"
        "INSERT INTO sessions VALUES ('jobsub', 'subagent', 'job', '{\"_delegate_from\": \"job\"}', 'J', 1, 0, 1);"
    )
    conn.commit()
    conn.close()


def _names(events):
    return [name for name, _payload in events]


@pytest.mark.parametrize("sql", [
    "UPDATE sessions SET message_count = message_count + 1, tool_call_count = 3 WHERE id = 'job'",
    "UPDATE sessions SET title = 'renamed' WHERE id = 'job'",
    "INSERT INTO sessions VALUES ('job2', 'oneshot', NULL, NULL, NULL, 0, 0, 2)",
    "UPDATE sessions SET message_count = 9 WHERE id = 'jobsub'",
    "INSERT INTO sessions VALUES ('sub2', 'subagent', 'job', NULL, NULL, 1, 0, 2)",
    "INSERT INTO sessions VALUES ('sub3', 'subagent', 'jobsub', NULL, NULL, 1, 0, 2)",
])
def test_worker_rows_never_refresh_a_session_list(watcher_home, sql):
    """One-shot runs and the delegate runs they start were ~80% of the row moves behind a sessions.changed every
    ~2.5 s; no window lists or watches them, so they sign jobs.changed (floored at 15 s) instead."""
    home, events = watcher_home
    _seed_mixed_store(home / "state.db")
    server._broadcast_watched_changes(now=0.0)

    time.sleep(0.02)
    _write(home / "state.db", sql)
    server._broadcast_watched_changes(now=20.0)

    assert "sessions.changed" not in _names(events) and "sessions.activity" not in _names(events)
    assert ("jobs.changed", {}) in events


def test_a_watch_pass_reads_each_store_once(watcher_home, monkeypatch):
    """sessions.changed, sessions.activity and jobs.changed sign the same per-store read: a pass reads a store once,
    also while its mtime is racy (a commit in the last 2 s is re-read every pass)."""
    home, events = watcher_home
    _seed_mixed_store(home / "state.db")
    reads = []
    real = server._session_rows_sig
    monkeypatch.setattr(server, "_session_rows_sig", lambda *a, **k: reads.append(1) or real(*a, **k))
    server._broadcast_watched_changes(now=0.0)
    assert len(reads) == 1
    reads.clear()
    _write(home / "state.db", "UPDATE sessions SET title = 'x' WHERE id = 'job'")
    server._broadcast_watched_changes(now=20.0)
    assert len(reads) == 1 and ("jobs.changed", {}) in events


def test_jobs_changed_is_floored_at_15_s_but_keeps_its_trailing_edge(watcher_home):
    home, events = watcher_home
    db = home / "state.db"
    _seed_mixed_store(db)
    server._broadcast_watched_changes(now=0.0)
    for step, now in enumerate((20.0, 25.0, 30.0, 36.0), start=1):
        time.sleep(0.02)
        _write(db, "UPDATE sessions SET message_count = ? WHERE id = 'job'", 10 + step)
        server._broadcast_watched_changes(now=now)
    # 20 fires; 25 and 30 are inside the floor; 36 is past it and carries the trailing change.
    assert _names(events).count("jobs.changed") == 2


@pytest.mark.parametrize("sql", [
    "UPDATE sessions SET message_count = 9 WHERE id = 'sub'",
    "INSERT INTO sessions VALUES ('sub2', 'subagent', 'chat', NULL, NULL, 1, 0, 2)",
])
def test_a_chats_hidden_delegate_run_still_refreshes_on_the_counters_floor(watcher_home, sql):
    """No list shows a chat's delegate run, but a window can watch its transcript: it signs sessions.changed at the
    10 s counters floor, never jobs.changed."""
    home, events = watcher_home
    _seed_mixed_store(home / "state.db")
    server._broadcast_watched_changes(now=0.0)

    time.sleep(0.02)
    _write(home / "state.db", sql)
    server._broadcast_watched_changes(now=5.0)
    assert _names(events) == ["sessions.changed"]
    time.sleep(0.02)
    _write(home / "state.db", "UPDATE sessions SET message_count = 11 WHERE id = 'sub'")
    server._broadcast_watched_changes(now=8.0)
    assert _names(events) == ["sessions.changed"]     # inside the 10 s counters floor: held
    server._broadcast_watched_changes(now=15.0)
    assert _names(events) == ["sessions.changed"] * 2


def test_a_subagent_run_is_listed_again_when_the_profile_shows_subagents(watcher_home):
    home, events = watcher_home
    (home / "config.yaml").write_text("display: {}\nsessions:\n  show_subagents: true\n")
    _seed_mixed_store(home / "state.db")
    server._broadcast_watched_changes(now=0.0)

    time.sleep(0.02)
    _write(home / "state.db", "UPDATE sessions SET title = 'renamed' WHERE id = 'sub'")
    server._broadcast_watched_changes(now=20.0)

    assert ("sessions.changed", {}) in events and "jobs.changed" not in _names(events)


def test_counter_moves_of_a_turn_running_here_ride_the_light_event(watcher_home, monkeypatch):
    """The desktop chat's turn runs in this process: every append already streams to the windows attached to it.
    Its counter moves sign sessions.activity at 2 s; sessions.changed carries them only once per 30 s (for the
    windows not attached), and the turn's start and end edges at the 10 s counters floor."""
    home, events = watcher_home
    db = home / "state.db"
    _seed_mixed_store(db)
    live = {}
    monkeypatch.setattr(server, "_sessions", live)
    server._broadcast_watched_changes(now=0.0)

    live["rt-chat"] = {"session_key": "chat", "running": True}  # turn starts
    server._broadcast_watched_changes(now=10.0)
    assert "sessions.changed" in _names(events)
    events.clear()

    for step, now in enumerate((20.0, 30.0), start=1):
        time.sleep(0.02)
        _write(db, "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = 'chat'", 2 + step, step)
        server._broadcast_watched_changes(now=now)
    assert _names(events) == ["sessions.activity"] * 2
    time.sleep(0.02)
    _write(db, "UPDATE sessions SET message_count = 9 WHERE id = 'chat'")
    server._broadcast_watched_changes(now=40.0)       # 30 s since the last sessions.changed: the slow refresh
    assert _names(events).count("sessions.activity") == 3 and _names(events).count("sessions.changed") == 1

    events.clear()
    live["rt-chat"]["running"] = False  # turn ends
    server._broadcast_watched_changes(now=50.0)
    assert "sessions.changed" in _names(events)


@pytest.mark.parametrize("sql", [
    "UPDATE sessions SET message_count = 5 WHERE id = 'tg'",
    "UPDATE sessions SET message_count = 3 WHERE id = 'chat'",
])
def test_a_counter_move_from_another_writer_still_refreshes_the_lists(watcher_home, sql):
    """A Telegram turn (the messaging gateway), or a delivery into an idle tab, never streams to this process's
    windows: sessions.changed is how they learn about it (open transcript, bot tile, unread dot, message count)."""
    home, events = watcher_home
    _seed_mixed_store(home / "state.db")
    server._broadcast_watched_changes(now=0.0)

    time.sleep(0.02)
    _write(home / "state.db", sql)
    server._broadcast_watched_changes(now=10.0)

    assert ("sessions.changed", {}) in events


def test_counter_only_moves_are_coalesced_to_the_desktops_list_cadence(watcher_home):
    """Another process appending to a listed row (a Telegram turn) still refreshes the lists, at most once per
    10 s with the trailing move kept; a structural move (a rename) still goes out within the 2 s floor."""
    home, events = watcher_home
    db = home / "state.db"
    _seed_mixed_store(db)
    server._broadcast_watched_changes(now=0.0)

    def counter(now, n):
        time.sleep(0.02)
        _write(db, "UPDATE sessions SET message_count = ? WHERE id = 'tg'", n)
        server._broadcast_watched_changes(now=now)

    counter(20.0, 5)                                  # fires: nothing went out in the last 10 s
    counter(22.5, 6)
    counter(25.0, 7)                                  # inside 10 s: held
    assert _names(events).count("sessions.changed") == 1
    time.sleep(0.02)
    _write(db, "UPDATE sessions SET title = 'Renamed' WHERE id = 'chat'")
    server._broadcast_watched_changes(now=26.0)       # structural: the 2 s floor (6 s since the last one)
    assert _names(events).count("sessions.changed") == 2
    counter(27.0, 8)
    server._broadcast_watched_changes(now=35.0)       # counters only, 9 s after the rename: held
    assert _names(events).count("sessions.changed") == 2
    server._broadcast_watched_changes(now=36.5)       # the trailing counter move goes out once the window opens
    assert _names(events).count("sessions.changed") == 3


def test_unreadable_store_keeps_last_digest(watcher_home):
    """A locked/unreadable moment after a good read must not flip to the mtime signature:
    digest -> mtime -> digest would broadcast twice for nothing."""
    home, events = watcher_home
    db = home / "state.db"
    _seed_store(db)
    server._broadcast_watched_changes(now=0.0)

    db.write_text("not-sqlite")
    server._broadcast_watched_changes(now=10.0)

    assert ("sessions.changed", {}) not in events


def test_projects_db_move_broadcasts_projects_changed(watcher_home):
    """#53046 / #56757: the CLI and other windows write projects.db directly, in
    processes that never touch this gateway's transports. Without a watch, a
    `hermes projects create` (or a set_primary / folder edit from another
    window) leaves the Desktop's project tree stale until an unrelated refresh."""
    home, events = watcher_home
    server._broadcast_watched_changes(now=0.0)

    (home / "projects.db").write_text("x")
    server._broadcast_watched_changes(now=10.0)

    assert ("projects.changed", {}) in events


def test_served_profile_projects_db_move_broadcasts_projects_changed(watcher_home, monkeypatch):
    """A backend serving a sibling profile watches that profile's projects.db too —
    the sibling-home half of the sessions.changed contract (#53046)."""
    home, events = watcher_home
    coder_home = home / "profiles" / "coder"
    coder_home.mkdir(parents=True)
    monkeypatch.setattr(server, "_served_profile_homes", set())
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: home / "profiles" / name)
    assert server._profile_home("coder") == coder_home
    server._broadcast_watched_changes(now=0.0)

    (coder_home / "projects.db").write_text("x")
    server._broadcast_watched_changes(now=10.0)

    assert ("projects.changed", {}) in events


def test_projects_sig_does_not_track_state_db_writes(watcher_home):
    """A state.db move must not fire projects.changed — the two stores are
    independent and their consumers refetch different surfaces."""
    home, events = watcher_home
    server._broadcast_watched_changes(now=0.0)

    (home / "state.db").write_text("x")
    server._broadcast_watched_changes(now=10.0)

    assert ("projects.changed", {}) not in events


def test_served_profile_store_move_broadcasts_sessions_changed(watcher_home, monkeypatch):
    """A backend serving a sibling profile must see that profile's state.db
    move too — otherwise a routed profile's Bot Chat never refreshes (#99333)."""
    home, events = watcher_home
    bot_home = home / "profiles" / "bot"
    bot_home.mkdir(parents=True)
    monkeypatch.setattr(server, "_served_profile_homes", set())
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: home / "profiles" / name)
    assert server._profile_home("bot") == bot_home
    server._broadcast_watched_changes(now=0.0)

    _write_session_change(bot_home / "state.db", "created")
    server._broadcast_watched_changes(now=10.0)

    assert ("sessions.changed", {}) in events


def test_gateway_state_move_broadcasts_platforms_changed(watcher_home):
    home, events = watcher_home
    server._broadcast_watched_changes(now=0.0)

    (home / "gateway_state.json").write_text('{"platforms": {}}')
    server._broadcast_watched_changes(now=10.0)

    assert ("platforms.changed", {}) in events


def test_pending_pairing_request_broadcasts_pairing_changed(watcher_home):
    """A new pending request must reach the Messaging page on its own signal.

    The messaging gateway writes the pending code from a different process, and
    it moves nothing in gateway_state.json — so platforms.changed cannot stand
    in for this. Without a dedicated signal the badge stays invisible until an
    unrelated connect/disconnect happens to fire.
    """
    home, events = watcher_home
    store = home / "platforms" / "pairing"
    store.mkdir(parents=True)
    server._broadcast_watched_changes(now=0.0)

    (store / "telegram-pending.json").write_text('{"abc": {"user_id": "1"}}')
    server._broadcast_watched_changes(now=10.0)

    assert ("pairing.changed", {}) in events
    assert ("platforms.changed", {}) not in events


def test_pairing_signal_follows_a_profile_store(watcher_home):
    """Each profile keeps its own whitelist, and the page can be scoped to any."""
    home, events = watcher_home
    store = home / "profiles" / "work" / "platforms" / "pairing"
    store.mkdir(parents=True)
    (home / "profiles" / "work" / "config.yaml").write_text("{}\n")  # identity marker: a bare dir is not a profile
    server._broadcast_watched_changes(now=0.0)

    (store / "telegram-approved.json").write_text('{"u1": {"user_id": "u1"}}')
    server._broadcast_watched_changes(now=10.0)

    assert ("pairing.changed", {}) in events


def test_pairing_probe_reuses_live_profile_roots_until_the_profile_set_moves(watcher_home, monkeypatch):
    """The per-profile liveness probe (~14 stats each) runs once per profiles/ mtime + TTL, not
    on every 2 s tick (#114041 §2); ledger writes under known roots are still seen each tick,
    and a newly created profile is picked up because creating it bumps the parent's mtime."""
    import hermes_constants

    home, events = watcher_home
    live_calls = []
    real_live = hermes_constants.named_profile_is_live
    monkeypatch.setattr(hermes_constants, "named_profile_is_live",
                        lambda p: live_calls.append(p.name) or real_live(p))

    def _profile(name):
        (home / "profiles" / name / "platforms" / "pairing").mkdir(parents=True)
        (home / "profiles" / name / "config.yaml").write_text("{}\n", encoding="utf-8")

    _profile("work")
    server._broadcast_watched_changes(now=0.0)
    (home / "profiles" / "work" / "platforms" / "pairing" / "telegram-pending.json").write_text("{}", encoding="utf-8")
    server._broadcast_watched_changes(now=10.0)
    assert events == [("pairing.changed", {})]
    assert live_calls == ["work"]  # second tick reused the cached roots, still saw the ledger

    _profile("play")
    os.utime(home / "profiles", ns=(0, 10**18))  # deterministic parent-mtime bump
    server._broadcast_watched_changes(now=20.0)
    (home / "profiles" / "play" / "platforms" / "pairing" / "discord-approved.json").write_text("{}", encoding="utf-8")
    server._broadcast_watched_changes(now=30.0)
    assert events == [("pairing.changed", {})] * 2
    assert sorted(live_calls) == ["play", "work", "work"]


def test_second_ledger_with_the_same_mtime_still_broadcasts_pairing_changed(watcher_home):
    """Coarse-timestamp filesystems give two ledgers written in one tick the same mtime; a
    newest-mtime signature cannot see the second one, so the badge for it never appears."""
    home, events = watcher_home
    store = home / "platforms" / "pairing"
    store.mkdir(parents=True)
    first = store / "telegram-pending.json"
    first.write_text("{}", encoding="utf-8")
    server._broadcast_watched_changes(now=0.0)

    second = store / "discord-pending.json"
    second.write_text("{}", encoding="utf-8")
    pinned = first.stat().st_mtime_ns
    os.utime(second, ns=(pinned, pinned))
    server._broadcast_watched_changes(now=10.0)

    assert events == [("pairing.changed", {})]


def test_sibling_projects_db_write_in_the_same_mtime_tick_broadcasts(watcher_home, monkeypatch):
    """One served profile's projects.db write that lands in the same timestamp tick as
    another's must not be masked by a newest-mtime-across-homes signature."""
    home, events = watcher_home
    coder_home = home / "profiles" / "coder"
    coder_home.mkdir(parents=True)
    monkeypatch.setattr(server, "_served_profile_homes", {coder_home})
    (home / "projects.db").write_text("x", encoding="utf-8")
    server._broadcast_watched_changes(now=0.0)

    (coder_home / "projects.db").write_text("x", encoding="utf-8")
    pinned = (home / "projects.db").stat().st_mtime_ns
    os.utime(coder_home / "projects.db", ns=(pinned, pinned))
    server._broadcast_watched_changes(now=10.0)

    assert ("projects.changed", {}) in events


def test_rate_limit_churn_does_not_broadcast_pairing_changed(watcher_home):
    """_rate_limits.json moves on every unauthorized DM, including ones that
    produce no new row — signalling on it would refetch for nothing."""
    home, events = watcher_home
    store = home / "platforms" / "pairing"
    store.mkdir(parents=True)
    (store / "telegram-pending.json").write_text("{}")
    server._broadcast_watched_changes(now=0.0)

    (store / "_rate_limits.json").write_text('{"telegram:1": 123}')
    server._broadcast_watched_changes(now=10.0)

    assert ("pairing.changed", {}) not in events


def test_sessions_floor_coalesces_burst_but_keeps_trailing_edge(watcher_home):
    home, events = watcher_home
    server._broadcast_watched_changes(now=0.0)

    _write_session_change(home / "state.db", "first")
    server._broadcast_watched_changes(now=10.0)
    events.clear()

    # A second write lands inside the 2s floor: no broadcast yet…
    time.sleep(0.02)
    _write_session_change(home / "state.db", "second")
    server._broadcast_watched_changes(now=11.0)
    assert events == []

    # …but the change is not lost — it fires once the window opens.
    server._broadcast_watched_changes(now=13.0)
    assert ("sessions.changed", {}) in events


def test_pet_sig_stays_off_without_a_renderable_pet(watcher_home):
    home, events = watcher_home
    server._broadcast_watched_changes(now=0.0)

    # Config flips enabled but no pet exists on disk → signature stays ("off",).
    (home / "config.yaml").write_text("display:\n  pet:\n    enabled: true\n    slug: boba\n")
    server._cfg_cache = None
    server._broadcast_watched_changes(now=10.0)

    assert not [e for e in events if e[0] == "pet.changed"]


def test_renderable_pet_broadcasts_meta_payload(watcher_home, monkeypatch):
    home, events = watcher_home
    (home / "config.yaml").write_text("display:\n  pet:\n    enabled: true\n    slug: boba\n")
    server._cfg_cache = None
    server._broadcast_watched_changes(now=0.0)

    sheet = home / "sheet.png"
    sheet.write_text("png")

    class FakePet:
        slug = "boba"
        display_name = "Boba"
        exists = True
        spritesheet = sheet

    monkeypatch.setattr(server, "_pet_active_selection", lambda: (True, FakePet(), 0.33))
    server._broadcast_watched_changes(now=10.0)

    pet_events = [e for e in events if e[0] == "pet.changed"]
    assert pet_events
    payload = pet_events[0][1]
    assert payload["enabled"] is True
    assert payload["slug"] == "boba"
    assert payload["spritesheetRevision"]


def test_enqueued_envelope_broadcasts_outbox_pending(watcher_home):
    """A cross-connection envelope written by the agent process must reach the
    Desktop's push-triggered drain on its own signal (#93091) — the drain poll
    is the backstop, not the transport."""
    home, events = watcher_home
    outbox = home / "bot_relay" / "outbox"
    outbox.mkdir(parents=True)
    server._broadcast_watched_changes(now=0.0)

    (outbox / ("a" * 32 + ".json")).write_text('{"id": "' + "a" * 32 + '"}')
    server._broadcast_watched_changes(now=10.0)

    assert ("bot_relay.outbox.pending", {}) in events


def test_drained_outbox_does_not_rebroadcast_pending(watcher_home):
    """Signature is monotone: a drain empties outbox/ (rename → claimed/), and
    that emptying must NOT look like a change — only new envelopes fire."""
    home, events = watcher_home
    outbox = home / "bot_relay" / "outbox"
    outbox.mkdir(parents=True)
    envelope = outbox / ("b" * 32 + ".json")
    envelope.write_text("{}")
    server._broadcast_watched_changes(now=0.0)

    envelope.unlink()  # the Desktop drained it
    server._broadcast_watched_changes(now=10.0)
    server._broadcast_watched_changes(now=20.0)

    assert not [e for e in events if e[0] == "bot_relay.outbox.pending"]


def test_new_envelope_after_drain_fires_pending_again(watcher_home):
    """The other half of the monotone contract: the watermark must not eat
    GENUINELY new envelopes. write → drain → write-newer fires twice."""
    home, events = watcher_home
    outbox = home / "bot_relay" / "outbox"
    outbox.mkdir(parents=True)
    first = outbox / ("c" * 32 + ".json")
    first.write_text("{}")
    server._broadcast_watched_changes(now=0.0)
    first.write_text("{}")  # make the first sighting a change, not a seed
    bump_ns = first.stat().st_mtime_ns + 1_000_000
    os.utime(first, ns=(bump_ns, bump_ns))  # strictly newer, FS-independent
    server._broadcast_watched_changes(now=10.0)

    first.unlink()  # the Desktop drained it
    server._broadcast_watched_changes(now=20.0)

    second = outbox / ("d" * 32 + ".json")
    second.write_text("{}")
    newer_ns = bump_ns + 1_000_000  # strictly beyond the watermark
    os.utime(second, ns=(newer_ns, newer_ns))
    server._broadcast_watched_changes(now=30.0)

    assert [e for e in events if e[0] == "bot_relay.outbox.pending"] == [
        ("bot_relay.outbox.pending", {}),
        ("bot_relay.outbox.pending", {}),
    ]


def test_no_outbox_dir_never_fires_pending(watcher_home):
    _home, events = watcher_home
    server._broadcast_watched_changes(now=0.0)
    server._broadcast_watched_changes(now=10.0)

    assert not [e for e in events if e[0] == "bot_relay.outbox.pending"]


def test_broken_probe_never_kills_the_pass(watcher_home, monkeypatch):
    home, events = watcher_home
    server._broadcast_watched_changes(now=0.0)

    monkeypatch.setitem(
        server._CHANGE_WATCHES,
        "cron.changed",
        (1.0, lambda: (_ for _ in ()).throw(RuntimeError("boom")), dict),
    )
    _write_session_change(home / "state.db", "created")
    server._broadcast_watched_changes(now=10.0)

    # The broken cron probe is skipped; sessions still broadcasts.
    assert ("sessions.changed", {}) in events


def test_skill_installed_from_another_terminal_broadcasts_skills_changed(watcher_home, monkeypatch):
    """CFG-8: the gateway notices a new skill dir and tells attached clients to refetch
    ``commands.catalog``; clients never rescan disk themselves."""
    home, events = watcher_home
    import tools.skills_tool as st
    skills = home / "skills"
    skills.mkdir()
    monkeypatch.setattr(st, "SKILLS_DIR", skills)
    server._broadcast_watched_changes(now=0.0)
    assert not [e for e in events if e[0] == "skills.changed"]

    skill = skills / "new-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: new-skill\ndescription: d\n---\nbody\n")
    os.utime(skills, (time.time() + 5, time.time() + 5))
    server._broadcast_watched_changes(now=10.0)

    assert ("skills.changed", {}) in events


def test_a_skill_installed_in_another_sessions_repo_broadcasts_skills_changed(watcher_home, monkeypatch, tmp_path):
    """The gateway runs in repo A; a session's catalog resolved under repo B (its workspace, via the
    session cwd). Installing a skill in B's ``.hermes/skills`` must still notify viewers: the watcher
    signs every scope a session resolved commands in, not only the launch repo."""
    import json
    import agent.skill_commands as sc
    import tools.skills_tool as st
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd
    home, events = watcher_home
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "skills").mkdir()
    monkeypatch.setattr(st, "SKILLS_DIR", home / "skills")
    monkeypatch.setattr(sc, "_skill_commands_by_key", {})
    monkeypatch.setattr(sc, "_skill_commands_sig_by_key", {})
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    for repo in (repo_a, repo_b):
        (repo / ".git").mkdir(parents=True)
        (repo / ".hermes" / "skills").mkdir(parents=True)
    (home / "config.yaml").write_text(json.dumps({"skills": {"trusted_project_dirs": [str(repo_a), str(repo_b)]}}))
    monkeypatch.chdir(repo_a)
    st.clear_skills_cache()
    token = set_session_cwd(str(repo_b))
    try:
        assert "/workspace-only" not in sc.get_skill_commands()  # B's session catalog: publishes B's scope
    finally:
        reset_session_cwd(token)
    server._broadcast_watched_changes(now=0.0)
    assert not [e for e in events if e[0] == "skills.changed"]

    skill = repo_b / ".hermes" / "skills" / "workspace-only"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: workspace-only\ndescription: d\n---\nbody\n")
    server._broadcast_watched_changes(now=10.0)
    assert ("skills.changed", {}) in events
    token = set_session_cwd(str(repo_b))
    try:
        assert "/workspace-only" in sc.get_skill_commands()
    finally:
        reset_session_cwd(token)
