"""A bot switch after a dashboard start reads a warm Bot Chat page.

The first message page of a lineage in a process dedupes the whole lineage (``lineage_page.py``): 0.14 to
1.1 s of GIL-held CPU on a long Bot Chat, paid by the first switch to each bot after every update or restart.
``prewarm_bot_chat_page`` builds that projection off the request path; the Desktop's page read then reuses it.
"""
import asyncio
from pathlib import Path

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _turns(segment, count):
    return [message for n in range(count) for message in (
        {"role": "user", "content": f"ask {segment}.{n}"}, {"role": "assistant", "content": f"reply {segment}.{n}"})]


def _bot_chat(home, prefix, *, segments, turns):
    """A Bot Chat as it lives on Tim's install: a compression lineage (the page projection only serves those)."""
    from persistence.sqlite._holder import StoreHolder
    from persistence.sqlite.conversation._store.compression import publish_compression_child
    from persistence.sqlite.conversation._store.messages import append_messages_batch, get_messages
    from persistence.sqlite.conversation._store.sessions import create_session
    from persistence_assembly import open_persistence, profile_scope
    from tests.persistence_sqlite.core._lent_store import lent_store

    db = StoreHolder(home / "state.db")
    try:
        create_session(db, f"{prefix}0", source="desktop")
        append_messages_batch(db, f"{prefix}0", _turns(0, turns))
        for n in range(1, segments):
            parent, child = f"{prefix}{n - 1}", f"{prefix}{n}"
            coordination = lent_store(db).coordination
            assert coordination.try_acquire_compression_lock(session_id=parent, holder="fixture")
            try:
                publish_compression_child(
                    db, parent_session_id=parent, child_session_id=child, source="desktop",
                    compression_lock_holder="fixture",
                    messages=[{"role": "user", "content": f"[CONTEXT SUMMARY] of {parent}"}])
            finally:
                coordination.release_compression_lock(session_id=parent, holder="fixture")
            append_messages_batch(db, child, _turns(n, turns))
    finally:
        db.close()
    tip = f"{prefix}{segments - 1}"
    with open_persistence(profile_scope(home=home)) as store:
        store.sessions.set_title(tip, "Bot Chat")
    return tip


def test_prewarm_builds_each_bot_chat_page_and_the_switch_reuses_it(tmp_path, monkeypatch):
    from hermes_cli.profiles import list_profile_names
    from hermes_cli.web_routers.sessions import manage_router, prewarm_bot_chat_page
    from persistence.sqlite.conversation._store import lineage_page

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    apex_home = default_home / "profiles" / "apex"
    quiet_home = default_home / "profiles" / "quiet"
    for home in (default_home, apex_home, quiet_home):
        home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    neo = _bot_chat(default_home, "neo", segments=3, turns=40)
    apex = _bot_chat(apex_home, "apex", segments=2, turns=10)
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_session
    with open_persistence(profile_scope(home=quiet_home)) as store:
        seed_session(store, "side", "desktop", title="Not the bot chat")

    built = []
    publish = lineage_page._publish
    monkeypatch.setattr(lineage_page, "_publish", lambda key, projection: built.append(key) or publish(key, projection))

    assert {name: prewarm_bot_chat_page(name) for name in list_profile_names()} == {
        "default": True, "apex": True, "quiet": False}
    assert len(built) == 2

    app = FastAPI()
    app.include_router(manage_router)
    query = "limit=120&order=latest&include_compacted=true"
    with TestClient(app) as client:
        neo_page = client.get(f"/api/sessions/{neo}/messages?{query}").json()
        apex_page = client.get(f"/api/sessions/{apex}/messages?{query}&profile=apex").json()
    assert len(neo_page["messages"]) == 120
    assert neo_page["messages"][-1]["content"] == "reply 2.39"
    assert apex_page["messages"][-1]["content"] == "reply 1.9"
    assert len(built) == 2  # both pages came from the projections the prewarm built


@pytest.mark.asyncio
async def test_the_server_start_warms_every_profile_once_its_loop_is_serving(monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.web_routers import sessions
    from tests.waiting import async_wait_until

    warmed = []
    monkeypatch.setattr("hermes_cli.profiles.list_profile_names", lambda: ["default", "apex"])
    monkeypatch.setattr(sessions, "prewarm_bot_chat_page", lambda name: warmed.append(name) or True)
    web_server._start_bot_chat_prewarm(delay_s=0.01)
    await async_wait_until(lambda: warmed == ["default", "apex"])

