"""The embedded dispatcher applies concurrency cap edits without a restart."""

import logging
from types import SimpleNamespace

import pytest

from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher
from hermes_cli import config, kanban_db, kanban_db_connect, kanban_db_dispatch


@pytest.fixture
def dispatcher(monkeypatch):
    settings = _DispatcherSettings(
        interval=30.0, max_spawn=3, max_in_progress=8, failure_limit=5,
        stale_timeout_seconds=90, reconcile_orphans=False, default_assignee="worker",
        max_in_progress_per_profile=None,
    )
    dispatcher = _KanbanDispatcher(kanban_db, settings)
    monkeypatch.setattr(dispatcher, "board_db_fingerprint", lambda slug: (slug, None, None))
    monkeypatch.setattr(kanban_db_connect, "connect", lambda board: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(kanban_db_dispatch, "dispatch_once", lambda conn, **kwargs: kwargs)
    return dispatcher


def test_live_caps_follow_edits_and_survive_read_error(dispatcher, monkeypatch, caplog):
    kanban = {"max_in_progress": 12, "max_spawn": 3, "failure_limit": 5, "dispatch_stale_timeout_seconds": 90,
              "reconcile_orphans": False, "default_assignee": "worker", "dispatch_interval_seconds": 30}
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": kanban})
    caplog.set_level(logging.INFO, logger="gateway.run")

    expected = {
        "board": "default", "max_in_progress": 12, "max_in_progress_per_profile": None,
        "max_spawn": 3, "failure_limit": 5, "stale_timeout_seconds": 90,
        "reconcile_orphans": False, "default_assignee": "worker",
    }
    assert dispatcher.tick_once_for_board("default") == expected

    kanban.update(max_in_progress_per_profile=8, failure_limit=1)
    expected["max_in_progress_per_profile"] = 8
    expected["failure_limit"] = 1
    assert dispatcher.tick_once_for_board("default") == expected
    assert dispatcher.tick_once_for_board("default") == expected

    def unreadable():
        raise OSError("config temporarily unavailable")

    monkeypatch.setattr(config, "load_config_readonly", unreadable)
    assert dispatcher.tick_once_for_board("default") == expected
    changes = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert sum("max_in_progress changed 8 -> 12" in message for message in changes) == 1
    assert sum("max_in_progress_per_profile changed unset -> 8" in message for message in changes) == 1
    assert sum("failure_limit changed 5 -> 1" in message for message in changes) == 1


def test_unset_global_cap_uses_memory_default(dispatcher, monkeypatch):
    kanban = {"max_in_progress": 12}
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": kanban})
    monkeypatch.setattr(kanban_db_dispatch, "derive_default_max_in_progress", lambda: 8)

    assert dispatcher.tick_once_for_board("default")["max_in_progress"] == 12
    del kanban["max_in_progress"]
    assert dispatcher.tick_once_for_board("default")["max_in_progress"] == 8


def _prime_12_8(dispatcher, monkeypatch):
    kanban = {"max_in_progress": 12, "max_in_progress_per_profile": 8}
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": kanban})
    out = dispatcher.tick_once_for_board("default")
    assert (out["max_in_progress"], out["max_in_progress_per_profile"]) == (12, 8)


def test_failed_config_read_fallback_keeps_caps(dispatcher, monkeypatch):
    """A fail-open fallback mapping (no caps in it) must not loosen or drop live caps."""
    from hermes_cli.config_read_errors import FailedConfigRead

    _prime_12_8(dispatcher, monkeypatch)
    monkeypatch.setattr(kanban_db_dispatch, "derive_default_max_in_progress", lambda: 8)
    monkeypatch.setattr(config, "load_config_readonly",
                        lambda: FailedConfigRead({"kanban": {}}, error=OSError("EMFILE")))
    out = dispatcher.tick_once_for_board("default")
    assert (out["max_in_progress"], out["max_in_progress_per_profile"]) == (12, 8)


def test_config_backend_outage_keeps_caps_and_still_dispatches(dispatcher, monkeypatch):
    """ConfigBackendUnavailable is a SystemExit: it must not abort the tick."""
    from hermes_cli.config_backend import ConfigBackendUnavailable

    _prime_12_8(dispatcher, monkeypatch)

    def outage():
        raise ConfigBackendUnavailable("config plane down")

    monkeypatch.setattr(config, "load_config_readonly", outage)
    out = dispatcher.tick_once_for_board("default")
    assert (out["max_in_progress"], out["max_in_progress_per_profile"]) == (12, 8)


@pytest.mark.parametrize("partial", [{}, {"kanban": None}, {"kanban": []}])
def test_partial_config_keeps_last_settings(dispatcher, monkeypatch, partial):
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": {"max_in_progress": 12, "failure_limit": 9}})
    before = dispatcher.tick_once_for_board("default")
    monkeypatch.setattr(config, "load_config_readonly", lambda: partial)
    assert dispatcher.tick_once_for_board("default") == before


def test_boards_share_one_snapshot_and_invalid_edit_warns_once(dispatcher, monkeypatch, caplog):
    kanban = {"max_spawn": 4}
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": kanban})
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["first", "second"])

    def edit_while_dispatching(conn, **kwargs):
        kanban.update(max_spawn=6, failure_limit="invalid")
        return kwargs["max_spawn"], kwargs["failure_limit"]

    monkeypatch.setattr(kanban_db_dispatch, "dispatch_once", edit_while_dispatching)
    for cap in (4, 6, 6):
        assert dispatcher.tick_once() == [(slug, (cap, kanban_db.DEFAULT_FAILURE_LIMIT)) for slug in ("first", "second")]
    assert sum("invalid kanban.failure_limit=" in msg for msg in caplog.messages) == 1
