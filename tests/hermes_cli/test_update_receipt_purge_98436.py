"""Regression tests for #98436 — update receipt lost when the stale-module purge
evicts hermes_cli.update_receipt mid-run (adopted from upstream PR #98454, ported to
the decomposed ``update_cmd_maint`` layout).

The receipt is a module-level singleton (``_current``) in hermes_cli.update_receipt.
The pending fleet-restart catch-up (``_run_pending_fleet_restart``) starts with
``_purge_stale_hermes_modules()``; without protection it evicted the module, so the
command-boundary safety net's ``from hermes_cli.update_receipt import
finalize_pending_update_receipt`` rebuilt a FRESH module with ``_current is None``,
finalize hit its documented no-op, and the run wrote NO receipt. On Linux that left
``latest.json`` on a stale *failed* receipt, which made the NEXT ``hermes update``
see a stale runtime and restart the whole fleet again — on every run, forever.
"""

from __future__ import annotations

import sys

import pytest

from hermes_cli import update_cmd_maint


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    snapshot = dict(sys.modules)
    yield
    for name, mod in snapshot.items():
        sys.modules[name] = mod
    for name in list(sys.modules):
        if name not in snapshot:
            del sys.modules[name]


def test_purge_protects_update_receipt_module():
    import hermes_cli.update_receipt as update_receipt

    update_cmd_maint._purge_stale_hermes_modules()

    assert sys.modules.get("hermes_cli.update_receipt") is update_receipt, (
        "hermes_cli.update_receipt was purged — the in-flight receipt singleton dies with it (#98436)"
    )


def test_boundary_from_import_finalizes_after_purge(tmp_path, monkeypatch):
    """Drive the exact failure shape: begin -> purge -> boundary from-import -> finalize.

    The finalize must write a receipt (path returned, latest.json present), not no-op.
    """
    import hermes_cli.update_receipt as update_receipt

    monkeypatch.setattr(update_receipt, "_receipt_dir", lambda: tmp_path)
    update_receipt.begin_update_receipt()
    assert update_receipt._current is not None

    update_cmd_maint._purge_stale_hermes_modules()

    from hermes_cli.update_receipt import finalize_pending_update_receipt

    path = finalize_pending_update_receipt(0, "completed at command boundary")
    assert path is not None, "finalize became a no-op — the receipt singleton was orphaned"
    assert (tmp_path / "latest.json").is_file()
