"""``cron.bot_chat_title``: bot-chat cron output can land in a dedicated chat, not the Bot Chat.

Producer side of the authority door (``gateway/session_bot.py``): the title resolves per delivery
(job ``bot_chat_title`` > receiving profile's ``cron.bot_chat_title`` > root config > "Bot Chat"),
the owner lookup and the door request carry it, a parked delivery (``cron.bot_chat_legacy``) keeps
the job's pin and re-resolves it when it drains, and the door only accepts titles the operator
configured. The door's own admission into the titled chat: tests/gateway/test_bot_door_cron_chat_title.py.
"""
import json

import pytest

from cron import scheduler_delivery as delivery
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import bot_live_delivery as mailbox


def _write_title(home, title):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(f"cron:\n  bot_chat_title: {title!r}\n", encoding="utf-8")


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "custom"
    (root / "profiles" / "beta").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))
    token = set_hermes_home_override(str(root))
    yield root
    reset_hermes_home_override(token)


def test_title_defaults_to_bot_chat(root):
    assert mailbox.cron_chat_title(root) == "Bot Chat"
    assert mailbox.cron_chat_title(root / "profiles" / "beta") == "Bot Chat"


def test_root_title_applies_to_every_profile_and_profile_can_override(root):
    _write_title(root, "Crons")
    beta = root / "profiles" / "beta"
    assert mailbox.cron_chat_title(root) == "Crons"
    assert mailbox.cron_chat_title(beta) == "Crons"
    # A migrated empty default in the profile inherits instead of masking the root value.
    _write_title(beta, "")
    assert mailbox.cron_chat_title(beta) == "Crons"
    _write_title(beta, "Beta crons")
    assert mailbox.cron_chat_title(beta) == "Beta crons"


def test_job_override_beats_profile_and_root_settings(root):
    _write_title(root, "Crons")
    beta = root / "profiles" / "beta"
    _write_title(beta, "Beta crons")
    assert mailbox.cron_chat_title(beta, "Reports") == "Reports"
    assert mailbox.cron_chat_title(root, "  Morning   reports ") == "Morning reports"
    # The canonical title, however it is spelled, is the canonical main chat.
    assert mailbox.cron_chat_title(beta, "bot  chat") == "Bot Chat"
    # An empty / cleared job field falls through to the profile, then the root setting.
    assert mailbox.cron_chat_title(beta, "") == "Beta crons"
    assert mailbox.cron_chat_title(beta, None) == "Beta crons"
    assert mailbox.cron_chat_title(root, "   ") == "Crons"


# ── Producer: owner lookup + door request carry the title ───────────────────────────────────────


def _owner(home, name):
    return dict(profile_home=str(home), session_id=name, lease_id=f"l-{name}", live_session_id=name, canonical=True)


@pytest.fixture
def door(root, monkeypatch):
    """Fake authority door: records each ``bot_relay.deliver`` request and settles it."""
    calls = []

    def deliver(home, params):
        calls.append(params)
        return dict(status="settled", delivery_id=params["id"], profile_home=str(home),
                    session_id=params.get("session_id", ""), message=params["message"], reply="ok",
                    **({"notification_category": params["notification_category"]}
                       if "notification_category" in params else {}))

    monkeypatch.setattr(mailbox, "authority_delivery", deliver)
    return calls


def _titled_lookup(home, lookups):
    def live_owner(h, *args):
        lookups.append(args)  # () = the historical canonical call shape
        title = args[0] if args else "Bot Chat"
        owner = _owner(home, "crons" if title == "Crons" else "main")
        return owner | ({"title": title} if args else {})
    return live_owner


def test_live_lane_targets_the_configured_cron_chat(root, door, monkeypatch):
    _write_title(root, "Crons")
    home = (root / "profiles" / "beta").resolve()
    lookups = []
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", _titled_lookup(home, lookups))
    assert delivery._deliver_to_bot_chat(dict(id="digest", name="Digest", execution_id="run"),
                                         "payload", "beta") is None
    assert lookups == [("Crons",)]
    [params] = door
    assert params["title"] == "Crons" and params["session_id"] == "crons"
    assert params["message"].endswith("payload") and params["profile"] == "beta"


def test_default_config_keeps_the_historical_bot_chat_request(root, door, monkeypatch):
    home = (root / "profiles" / "beta").resolve()
    lookups = []
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", _titled_lookup(home, lookups))
    assert delivery._deliver_to_bot_chat(dict(id="digest", execution_id="run"), "payload", "beta") is None
    assert lookups == [()]
    [params] = door
    assert "title" not in params and params["session_id"] == "main"


def test_job_pinned_bot_chat_beats_a_crons_profile(root, door, monkeypatch):
    """The morning brief pins "Bot Chat": it reaches the main chat while the rest go to Crons."""
    _write_title(root, "Crons")
    home = (root / "profiles" / "beta").resolve()
    lookups = []
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", _titled_lookup(home, lookups))
    job = dict(id="brief", name="Morning brief", execution_id="run", bot_chat_title="Bot Chat")
    assert delivery._deliver_to_bot_chat(job, "payload", "beta") is None
    assert lookups == [()]
    assert "title" not in door[0]


def test_job_pinned_title_beats_the_profile_setting(root, door, monkeypatch):
    _write_title(root, "Crons")
    home = (root / "profiles" / "beta").resolve()
    lookups = []
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", _titled_lookup(home, lookups))
    job = dict(id="weekly", name="Weekly", execution_id="run", bot_chat_title="Reports")
    assert delivery._deliver_to_bot_chat(job, "payload", "beta") is None
    assert lookups == [("Reports",)]
    assert door[0]["title"] == "Reports"


def test_parked_delivery_keeps_the_job_pin_and_drains_into_its_chat(root, door, monkeypatch):
    """Target gateway down: the output is parked under its id with the job's pin, and the drain
    hands it to the same titled chat once the owner is up."""
    from cron import bot_chat_legacy
    from hermes_state import SessionDB

    _write_title(root, "Crons")
    home = (root / "profiles" / "beta").resolve()
    SessionDB(db_path=home / "state.db").close()
    up = False
    lookups = []
    titled = _titled_lookup(home, lookups)

    def live_owner(h, *args):
        if not up:
            raise ValueError("profile authority is not ready")
        return titled(h, *args)

    monkeypatch.setattr(mailbox, "find_canonical_live_owner", live_owner)
    for job_id, pin in (("brief", "Bot Chat"), ("digest", None)):
        job = dict(id=job_id, name=job_id, execution_id="run", **({"bot_chat_title": pin} if pin else {}))
        assert "queued" in delivery._deliver_to_bot_chat(job, f"{job_id} payload", "beta")
    pending = sorted((root / "cron" / "bot_chat_pending").glob("*.json"))
    jobs = {json.loads(p.read_text())["job"]["id"]: json.loads(p.read_text())["job"] for p in pending}
    assert jobs["brief"]["bot_chat_title"] == "Bot Chat" and "bot_chat_title" not in jobs["digest"]

    up = True
    bot_chat_legacy.drain_legacy_pending()
    by_job = {params["message"].split("\n\n", 1)[1]: params for params in door}
    assert "title" not in by_job["brief payload"]
    assert by_job["digest payload"]["title"] == "Crons"
    assert sorted(lookups) == [(), ("Crons",)]
    assert {json.loads(p.read_text())["status"] for p in pending} == {"transferred"}


# ── Door guard: which titles a producer may name ─────────────────────────────────────────────────


def test_door_accepts_only_operator_configured_titles(root):
    beta = root / "profiles" / "beta"
    assert mailbox.door_accepts_title(beta, "Bot Chat")
    assert not mailbox.door_accepts_title(beta, "Crons")
    _write_title(root, "Crons")
    assert mailbox.door_accepts_title(beta, "Crons")
    assert not mailbox.door_accepts_title(beta, "Tim's private chat")
    assert not mailbox.door_accepts_title(beta, None)
    # A job pinning its own chat for a bot-chat delivery into beta makes that title admissible
    # there (and only there).
    (root / "cron").mkdir(exist_ok=True)
    (root / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        dict(id="w", deliver="bot-chat:beta", bot_chat_title="Weekly reports"),
        dict(id="x", deliver="local", bot_chat_title="Not a bot-chat job"),
    ]}), encoding="utf-8")
    assert mailbox.door_accepts_title(beta, "Weekly reports")
    assert not mailbox.door_accepts_title(root, "Weekly reports")
    assert not mailbox.door_accepts_title(beta, "Not a bot-chat job")


# ── CLI: hermes cron create/edit --bot-chat-title, shown in hermes cron list ───────────────────


@pytest.fixture
def cron_store(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _cli(*argv):
    import argparse

    from hermes_cli.cron import cron_command
    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=cron_command)
    return cron_command(parser.parse_args(["cron", *argv]))


def _chat_rows(capsys):
    capsys.readouterr()  # drop create/edit output: only the listing counts here
    _cli("list")
    return [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip().startswith("Chat:")]


def test_cli_create_edit_list_round_trip(cron_store, capsys):
    from cron.jobs import get_job, list_jobs

    assert _cli("create", "every 1h", "Morning brief", "--deliver", "local", "--bot-chat-title", "Bot Chat") in (0, None)
    job = list_jobs()[0]
    assert job["bot_chat_title"] == "Bot Chat"
    assert "Chat: Bot Chat" in capsys.readouterr().out
    assert _chat_rows(capsys) == ["Chat:      Bot Chat"]

    assert _cli("edit", job["id"], "--bot-chat-title", "  Weekly   reports ") in (0, None)
    assert get_job(job["id"])["bot_chat_title"] == "Weekly reports"
    assert _chat_rows(capsys) == ["Chat:      Weekly reports"]

    # Editing something else leaves the override alone; an empty value clears it.
    _cli("edit", job["id"], "--name", "Brief")
    assert get_job(job["id"])["bot_chat_title"] == "Weekly reports"
    assert _cli("edit", job["id"], "--bot-chat-title", "") in (0, None)
    assert not get_job(job["id"]).get("bot_chat_title")
    assert _chat_rows(capsys) == []


def test_cli_create_without_the_flag_stores_no_override(cron_store, capsys):
    from cron.jobs import load_jobs

    _cli("create", "every 1h", "Digest", "--deliver", "local")
    assert "bot_chat_title" not in load_jobs()[0]
    assert _chat_rows(capsys) == []
