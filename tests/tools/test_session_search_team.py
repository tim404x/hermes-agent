"""session_search team scope: profile='all' (or config session_search.default_scope: all) searches every
profile's state.db read-only, merges hits, tags each with its profile, and keeps own-store behaviour intact."""
import json
import time

import pytest

from hermes_cli import profiles as profiles_mod
from hermes_state import SessionDB
import tools.session_search_tool as sst
from tools.session_search_tool import session_search


def _seed(db, sid, texts, t0):
    db.create_session(sid, source="cli")
    ids = []
    for i, (role, text) in enumerate(texts):
        ids.append(db.append_message(sid, role=role, content=text))
        db._conn.execute("UPDATE messages SET timestamp=? WHERE id=?", (t0 + i, ids[-1]))
    db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, sid))
    db._conn.commit()
    return ids


@pytest.fixture
def team(tmp_path, monkeypatch):
    """default (Neo) + apex + kepler profiles, each with its own state.db; active = apex."""
    root = tmp_path / "hermes"
    homes = {"default": root, "apex": root / "profiles" / "apex", "kepler": root / "profiles" / "kepler"}
    dbs = {}
    for name, home in homes.items():
        home.mkdir(parents=True, exist_ok=True)
        dbs[name] = SessionDB(home / "state.db")
    now = time.time()
    _seed(dbs["default"], "neo_old", [("user", "the quasar invoice numbering rule is date style"),
                                      ("assistant", "noted, quasar invoices use date style numbers")], now - 30 * 86400)
    _seed(dbs["default"], "neo_new", [("user", "quasar invoice rule changed: no suffixes ever"),
                                      ("assistant", "updated the quasar invoice rule")], now - 86400)
    _seed(dbs["kepler"], "kep_1", [("user", "ship the quasar pipeline export"),
                                    ("assistant", "quasar export shipped")], now - 3600)
    _seed(dbs["apex"], "apex_1", [("user", "draft the quasar SOW"), ("assistant", "quasar SOW drafted")], now - 7200)
    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: root)
    monkeypatch.setattr(profiles_mod, "_iter_named_profile_dirs",
                        lambda **kw: [homes["apex"], homes["kepler"]])
    monkeypatch.setattr(profiles_mod, "get_active_profile_name", lambda: "apex")
    monkeypatch.setattr(sst, "_search_config", dict)
    yield dbs
    for d in dbs.values():
        d.close()


def _profiles(res):
    return [(r.get("profile"), r["session_id"]) for r in res["results"]]


def test_default_scope_is_own_profile(team):
    res = json.loads(session_search("quasar", db=team["apex"], limit=10))
    assert res["success"] and {r["session_id"] for r in res["results"]} == {"apex_1"}
    assert "scope" not in res


def test_profile_all_searches_every_store_and_tags_results(team):
    res = json.loads(session_search("quasar", db=team["apex"], limit=10, profile="all"))
    assert res["success"] and res["scope"] == "all"
    assert set(res["profiles_searched"]) == {"apex", "default", "kepler"}
    got = _profiles(res)
    assert set(got) == {("apex", "apex_1"), ("default", "neo_old"), ("default", "neo_new"), ("kepler", "kep_1")}
    assert got[0] == ("apex", "apex_1")                       # own store leads the round-robin
    links = {r["session_id"]: r["link"] for r in res["results"]}
    assert links["neo_new"] == "@session:default/neo_new" and links["kep_1"] == "@session:kepler/kep_1"
    # adaptive: only the merged top result keeps its full window
    assert res["results"][0]["detail"] == "full"
    assert all(r["detail"] == "compact" and len(r["messages"]) == 1 for r in res["results"][1:])


def test_sort_newest_merges_by_match_time(team):
    res = json.loads(session_search("quasar", db=team["apex"], limit=5, profile="all", sort="newest"))
    sids = [sid for _, sid in _profiles(res)]
    assert sids == ["kep_1", "apex_1", "neo_new", "neo_old"]       # 1h, 2h, 1d, 30d ago across 3 stores
    res = json.loads(session_search("quasar", db=team["apex"], limit=5, profile="all", sort="oldest"))
    assert next(sid for _, sid in _profiles(res)) == "neo_old"


def test_limit_applies_after_merge(team):
    res = json.loads(session_search("quasar", db=team["apex"], limit=2, profile="all"))
    assert res["count"] == 2


def test_config_default_scope_all(team, monkeypatch):
    monkeypatch.setattr(sst, "_search_config", lambda: {"default_scope": "all"})
    res = json.loads(session_search("quasar", db=team["apex"], limit=10))
    assert res["scope"] == "all" and len(res["results"]) == 4
    # an explicit own-profile name still narrows to that one store
    res = json.loads(session_search("quasar", db=team["apex"], limit=10, profile="kepler"))
    assert {r["session_id"] for r in res["results"]} == {"kep_1"}


def test_exclude_profiles(team, monkeypatch):
    monkeypatch.setattr(sst, "_search_config", lambda: {"default_scope": "all", "exclude_profiles": ["kepler"]})
    res = json.loads(session_search("quasar", db=team["apex"], limit=10))
    assert "kepler" not in res["profiles_searched"] and all(r.get("profile") != "kepler" for r in res["results"])


def test_read_and_browse_stay_scoped(team, monkeypatch):
    monkeypatch.setattr(sst, "_search_config", lambda: {"default_scope": "all"})
    # read a teammate's hit through its link
    res = json.loads(session_search(db=team["apex"], session_id="default/neo_new"))
    assert res["success"] and res["mode"] == "read" and res["session_id"] == "neo_new"
    # profile='all' cannot read/scroll
    res = json.loads(session_search(db=team["apex"], session_id="neo_new", profile="all"))
    assert res["success"] is False
    # browse (no query) stays on the own store
    res = json.loads(session_search(db=team["apex"]))
    assert {r["session_id"] for r in res["results"]} == {"apex_1"}


def test_other_stores_are_closed(team, monkeypatch):
    closed = []
    real = SessionDB.close
    monkeypatch.setattr(SessionDB, "close", lambda self: (closed.append(self.db_path), real(self)))
    session_search("quasar", db=team["apex"], limit=10, profile="all")
    assert len(closed) >= 2                                   # default + kepler opened read-only, then closed


def test_unnamed_active_profile_falls_back_to_own(team, monkeypatch):
    monkeypatch.setattr(profiles_mod, "get_active_profile_name", lambda: "custom")
    res = json.loads(session_search("quasar", db=team["apex"], limit=10, profile="all"))
    assert {r["session_id"] for r in res["results"]} == {"apex_1"}
