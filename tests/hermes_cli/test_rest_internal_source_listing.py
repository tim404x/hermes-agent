"""REST session lists hide internal worker runs (kanban/tool/oneshot) unless the caller scopes (fork carry).

The official iOS app lists sessions through ``/api/sessions`` and ``/api/profiles/sessions`` with no source
params, so every ``source='oneshot'`` run (Outbox worker, detached ``chat -Q --source oneshot`` jobs) flooded
its list while the desktop's TUI listing hid them (``INTERNAL_LISTING_SOURCES``). Cron stays listed.
"""

import pytest

from hermes_cli.session_listing import with_internal_listing_excludes


@pytest.fixture(autouse=True)
def _uncached_sidebar_endpoints(monkeypatch):
    from hermes_cli.web_routers import profiles as profiles_routes

    monkeypatch.setattr(profiles_routes, "_SIDEBAR_CACHE_TTL_SECONDS", 0.0)


@pytest.fixture
def profiles_on_disk(tmp_path, monkeypatch, _isolate_hermes_home):
    from hermes_cli import profiles
    from hermes_constants import get_hermes_home

    default_home = get_hermes_home()
    profiles_root = default_home / "profiles"
    worker_home = profiles_root / "worker"
    for home in (default_home, worker_home):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    return {"default": default_home, "worker": worker_home}


@pytest.fixture
def client(profiles_on_disk):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def _seed(home, session_id, source):
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_messages, seed_session

    with open_persistence(profile_scope(home=home)) as db:
        seed_session(db, session_id, source)
        seed_messages(db, session_id, {"role": "user", "content": "hi"})


@pytest.fixture
def seeded(profiles_on_disk):
    for home_name, prefix in (("default", "d"), ("worker", "w")):
        home = profiles_on_disk[home_name]
        for source in ("cli", "telegram", "cron", "oneshot", "kanban", "tool"):
            _seed(home, f"{prefix}-{source}", source)
    return profiles_on_disk


def _sources(rows):
    return sorted(r["source"] for r in rows)


def test_helper_only_defaults_unscoped_callers():
    assert with_internal_listing_excludes(None) == ["kanban", "tool", "oneshot"]
    assert with_internal_listing_excludes(None, source="oneshot") is None
    assert with_internal_listing_excludes(None, sources=["oneshot", "cron"]) is None
    # An explicit exclusion list is the caller's own scope (web dashboard Automation tab).
    assert with_internal_listing_excludes(["cli", "telegram"]) == ["cli", "telegram"]


def test_api_sessions_unscoped_hides_internal_runs_keeps_cron(client, seeded):
    body = client.get("/api/sessions?limit=50&order=recent").json()
    assert _sources(body["sessions"]) == ["cli", "cron", "telegram"]
    assert body["total"] == len(body["sessions"]) == 3


def test_api_sessions_explicit_source_still_returns_internal_runs(client, seeded):
    body = client.get("/api/sessions?limit=50&source=oneshot").json()
    assert _sources(body["sessions"]) == ["oneshot"] and body["total"] == 1
    body = client.get("/api/sessions?limit=50&sources=oneshot,kanban").json()
    assert _sources(body["sessions"]) == ["kanban", "oneshot"] and body["total"] == 2
    body = client.get("/api/sessions?limit=50&source=cron").json()
    assert _sources(body["sessions"]) == ["cron"] and body["total"] == 1


def test_api_sessions_caller_exclusions_are_kept_verbatim(client, seeded):
    body = client.get("/api/sessions?limit=50&exclude_sources=cli,telegram").json()
    assert _sources(body["sessions"]) == ["cron", "kanban", "oneshot", "tool"]
    assert body["total"] == 4


def test_profiles_sessions_unscoped_hides_internal_runs_and_totals_match(client, seeded):
    body = client.get("/api/profiles/sessions?limit=100").json()
    assert _sources(body["sessions"]) == ["cli", "cli", "cron", "cron", "telegram", "telegram"]
    assert body["total"] == 6
    assert body["profile_totals"] == {"default": 3, "worker": 3}


def test_profiles_sessions_explicit_source_still_returns_internal_runs(client, seeded):
    body = client.get("/api/profiles/sessions?limit=100&source=oneshot").json()
    assert _sources(body["sessions"]) == ["oneshot", "oneshot"]
    assert body["profile_totals"] == {"default": 1, "worker": 1}
    body = client.get("/api/profiles/sessions?limit=100&source=cron").json()
    assert _sources(body["sessions"]) == ["cron", "cron"]


def test_sidebar_unscoped_slices_hide_internal_runs_cron_slice_unchanged(client, seeded):
    body = client.get("/api/profiles/sessions/sidebar").json()
    assert "oneshot" not in _sources(body["recents"]["sessions"])
    assert not {"oneshot", "kanban", "tool"} & set(_sources(body["messaging"]["sessions"]))
    assert _sources(body["cron"]["sessions"]) == ["cron", "cron"]
