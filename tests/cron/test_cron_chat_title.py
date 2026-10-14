"""``cron.bot_chat_title``: bot-chat cron output can land in a dedicated chat, not the Bot Chat.

Every lane of a bot-chat delivery (live-owner mailbox, deferred replay, CLI turn) must target the
same configured title, and the desktop mailbox poller must let that chat's live owner claim it.
"""
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

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


@pytest.mark.parametrize("profile", ["beta", ""])
def test_cli_lane_resumes_the_configured_chat(root, monkeypatch, profile):
    _write_title(root, "Crons")
    titles = []

    def live_owner(home, title="Bot Chat"):
        titles.append(title)

    seen = []

    def run(argv, env, report_path, timeout):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(mailbox, "find_canonical_live_owner", live_owner)
    monkeypatch.setattr(delivery, "_run_bot_chat_turn", run)
    assert delivery._deliver_to_bot_chat({"id": "job"}, "output", profile) is None
    argv = seen[0]
    assert argv[argv.index("-c") + 1] == "Crons"
    assert "Bot Chat" not in argv
    assert titles and set(titles) == {"Crons"}


def test_cli_lane_default_still_targets_bot_chat(root, monkeypatch):
    seen = []
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home: None)
    monkeypatch.setattr(delivery, "_run_bot_chat_turn",
                        lambda argv, env, report_path, timeout: seen.append(argv)
                        or subprocess.CompletedProcess(argv, 0, "", ""))
    assert delivery._deliver_to_bot_chat({"id": "job"}, "output", "beta") is None
    assert seen[0][seen[0].index("-c") + 1] == "Bot Chat"


def test_live_lane_pins_the_cron_chat_owner_not_the_bot_chat(root, monkeypatch):
    _write_title(root, "Crons")
    home = (root / "profiles" / "beta").resolve()
    bot_chat = dict(profile_home=str(home), session_id="main", lease_id="l-main", live_session_id="live-main")
    crons = dict(profile_home=str(home), session_id="crons", lease_id="l-crons", live_session_id="live-crons")
    monkeypatch.setattr(mailbox, "find_canonical_live_owner",
                        lambda h, title="Bot Chat": crons if title == "Crons" else bot_chat)
    monkeypatch.setattr(delivery, "_run_bot_chat_turn", Mock(side_effect=AssertionError("CLI")))
    pending = delivery._deliver_to_bot_chat(dict(id="digest", name="Digest", execution_id="run"), "payload", "beta")
    assert pending and "queued" in pending
    assert mailbox.claim_pending_delivery(home, bot_chat) is None
    record = mailbox.claim_pending_delivery(home, crons)
    assert record is not None and record["message"].endswith("payload")


def test_poller_lets_the_cron_chat_owner_claim(tmp_path, monkeypatch):
    from tui_gateway import session_notifications
    from tui_gateway.method_ctx import rebind
    from tui_gateway.session_lifecycle import _session_turn_admission

    _write_title(tmp_path, "Crons")
    mailbox._cron_chat_owner_cache.clear()
    bot_chat = {"lease_id": "l-main", "live_session_id": "live-main", "session_id": "main"}
    crons = {"lease_id": "l-crons", "live_session_id": "live-crons", "session_id": "crons"}
    monkeypatch.setattr(mailbox, "find_canonical_live_owner",
                        lambda home, title="Bot Chat": crons if title == "Crons" else bot_chat)
    claims = []
    monkeypatch.setattr(mailbox, "claim_pending_delivery",
                        lambda home, owner, chat_title=None: claims.append((owner, chat_title))
                        or {"id": "receipt", "message": "cron out"})
    monkeypatch.setattr(mailbox, "complete_delivery", lambda *a, **k: None)
    submitted = []

    def submit(rid, sid, session, text, **kwargs):
        submitted.append((sid, text))
        kwargs["terminal_callback"]({"status": "settled", "text": "ok"})
        return True

    poll = rebind(session_notifications._poll_bot_live_delivery_once, {
        "_session_home": lambda session: tmp_path,
        "_session_turn_admission": _session_turn_admission,
        "_run_prompt_submit": submit,
        "_notif_release_turn": lambda session: session.update(running=False),
    })
    mailbox._root(tmp_path).mkdir(parents=True)

    def session(key, lease):
        return {"history_lock": threading.RLock(), "agent": object(), "session_key": key,
                "active_session_lease": SimpleNamespace(lease_id=lease, released=False)}

    assert poll("live-other", session("other", "l-other")) is False
    assert not claims
    assert poll("live-crons", session("crons", "l-crons")) is True
    assert claims == [(crons, "Crons")] and submitted == [("live-crons", "cron out")]


# ── Per-job override (job record ``bot_chat_title``) ─────────────────────────────────────────────
# A job can pin its own chat: the job field beats the receiving profile's and the root's
# ``cron.bot_chat_title``; ``"Bot Chat"`` means the canonical main chat with the historical lookup.


def _owner(home, name):
    return dict(profile_home=str(home), session_id=name, lease_id=f"l-{name}", live_session_id=f"live-{name}")


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


def test_bot_chat_override_routes_to_the_canonical_owner_even_when_profile_says_crons(root, monkeypatch):
    _write_title(root, "Crons")
    home = (root / "profiles" / "beta").resolve()
    bot_chat, crons = _owner(home, "main"), _owner(home, "crons")
    calls = []

    def live_owner(h, *args):
        calls.append(args)  # () = the historical canonical call shape
        return crons if args == ("Crons",) else bot_chat if not args else None

    monkeypatch.setattr(mailbox, "find_canonical_live_owner", live_owner)
    monkeypatch.setattr(delivery, "_run_bot_chat_turn", Mock(side_effect=AssertionError("CLI")))
    job = dict(id="brief", name="Morning brief", execution_id="run", bot_chat_title="Bot Chat")
    pending = delivery._deliver_to_bot_chat(job, "payload", "beta")
    assert pending and "queued" in pending
    assert calls and set(calls) == {()}
    # The envelope carries its target title; the Crons owner never claims it.
    assert mailbox.claim_pending_delivery(home, crons, chat_title="Crons") is None
    record = mailbox.claim_pending_delivery(home, bot_chat, chat_title="Bot Chat")
    assert record is not None and record["chat_title"] == "Bot Chat" and record["message"].endswith("payload")


def test_bot_chat_override_cli_lane_resumes_the_main_chat(root, monkeypatch):
    _write_title(root, "Crons")
    seen = []
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home, title="Bot Chat": None)
    monkeypatch.setattr(delivery, "_run_bot_chat_turn",
                        lambda argv, env, report_path, timeout: seen.append(argv)
                        or subprocess.CompletedProcess(argv, 0, "", ""))
    assert delivery._deliver_to_bot_chat({"id": "job", "bot_chat_title": "Bot Chat"}, "output", "beta") is None
    assert seen[0][seen[0].index("-c") + 1] == "Bot Chat"


def test_job_without_the_field_still_goes_to_the_profile_chat(root, monkeypatch):
    _write_title(root, "Crons")
    home = (root / "profiles" / "beta").resolve()
    bot_chat, crons = _owner(home, "main"), _owner(home, "crons")
    monkeypatch.setattr(mailbox, "find_canonical_live_owner",
                        lambda h, title="Bot Chat": crons if title == "Crons" else bot_chat)
    monkeypatch.setattr(delivery, "_run_bot_chat_turn", Mock(side_effect=AssertionError("CLI")))
    for index, job in enumerate(({}, {"bot_chat_title": ""}, {"bot_chat_title": None})):
        job.update(id=f"digest{index}", name="Digest", execution_id="run")
        assert "queued" in delivery._deliver_to_bot_chat(job, f"payload {index}", "beta")
        assert mailbox.claim_pending_delivery(home, bot_chat, chat_title="Bot Chat") is None
        record = mailbox.claim_pending_delivery(home, crons, chat_title="Crons")
        assert record is not None and record["chat_title"] == "Crons"


def test_deferred_lane_honours_the_job_override(root, monkeypatch):
    from cron import bot_chat_delivery as queue
    from hermes_state import SessionDB

    _write_title(root, "Crons")
    home = (root / "profiles" / "beta").resolve()
    SessionDB(db_path=home / "state.db").close()
    bot_chat, crons = _owner(home, "main"), _owner(home, "crons")
    bot_chat_live = False
    lookups = []

    def owner(h, *args):
        lookups.append(args)
        return {"held": "by a CLI"}

    def live_owner(h, *args):
        lookups.append(args)
        if args == ("Crons",):
            return crons
        return bot_chat if not args and bot_chat_live else None

    monkeypatch.setattr(mailbox, "find_canonical_owner", owner)
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", live_owner)
    monkeypatch.setattr(delivery, "_run_bot_chat_turn", Mock(side_effect=AssertionError("CLI")))
    key = "c" * 64
    job = dict(id="brief", name="Morning brief", execution_id="run", bot_chat_title="Bot Chat")
    queue.defer(key, job, "payload", "beta", home)

    # The main chat is held by a non-consumer: the record waits for IT, not for the Crons owner.
    queue.drain()
    assert queue.read_pending(key)["status"] == "queued"
    assert lookups and set(lookups) == {()}

    bot_chat_live = True
    queue.drain()
    assert queue.read_pending(key)["status"] == "transferred"
    assert mailbox.claim_pending_delivery(home, crons, chat_title="Crons") is None
    record = mailbox.claim_pending_delivery(home, bot_chat, chat_title="Bot Chat")
    assert record is not None and record["chat_title"] == "Bot Chat"


def test_claim_matches_the_envelope_title_exactly(tmp_path):
    home = tmp_path.resolve()
    me = _owner(home, "me")
    mailbox.deliver_to_live_owner(home, me, "for the main chat", delivery_id="a" * 32, chat_title="Bot Chat")
    # Same pin, different title: a session renamed after admission must not take it under its new title.
    assert mailbox.claim_pending_delivery(home, me, chat_title="Crons") is None
    assert mailbox.claim_pending_delivery(home, me, chat_title="Bot Chat")["id"] == "a" * 32
    # Envelopes without a title (message_agent, pre-title admissions) keep the pin-only contract.
    mailbox.deliver_to_live_owner(home, me, "legacy", delivery_id="b" * 32)
    assert mailbox.claim_pending_delivery(home, me, chat_title="Crons")["id"] == "b" * 32
    # Idempotent retry must name the same title.
    with pytest.raises(ValueError):
        mailbox.deliver_to_live_owner(home, me, "for the main chat", delivery_id="a" * 32, chat_title="Crons")


def _poller(home):
    from tui_gateway import session_notifications
    from tui_gateway.method_ctx import rebind
    from tui_gateway.session_lifecycle import _session_turn_admission

    submitted = []

    def submit(rid, sid, session, text, **kwargs):
        submitted.append((sid, text))
        kwargs["terminal_callback"]({"status": "settled", "text": "ok"})
        return True

    poll = rebind(session_notifications._poll_bot_live_delivery_once, {
        "_session_home": lambda session: home,
        "_session_turn_admission": _session_turn_admission,
        "_run_prompt_submit": submit,
        "_notif_release_turn": lambda session: session.update(running=False),
    })

    def session(owner):
        return {"history_lock": threading.RLock(), "agent": object(), "session_key": owner["session_id"],
                "active_session_lease": SimpleNamespace(lease_id=owner["lease_id"], released=False)}

    return (lambda owner: poll(owner["live_session_id"], session(owner))), submitted


def test_poller_canonical_owner_claims_a_bot_chat_pinned_delivery_and_crons_owner_does_not(tmp_path, monkeypatch):
    home = tmp_path.resolve()
    _write_title(home, "Crons")
    bot_chat, crons = _owner(home, "main"), _owner(home, "crons")
    monkeypatch.setattr(mailbox, "find_canonical_live_owner",
                        lambda h, title="Bot Chat": crons if title == "Crons" else bot_chat)
    mailbox.deliver_to_live_owner(home, bot_chat, "brief", delivery_id="d" * 32, chat_title="Bot Chat")
    poll, submitted = _poller(home)
    assert poll(crons) is False
    assert poll(bot_chat) is True
    assert submitted == [("live-main", "brief")]
    assert mailbox.read_delivery_result(home, "d" * 32)["status"] == "settled"


def test_poller_lets_the_owner_of_a_job_pinned_title_claim(tmp_path, monkeypatch):
    home = tmp_path.resolve()
    _write_title(home, "Crons")
    bot_chat, crons, reports = _owner(home, "main"), _owner(home, "crons"), _owner(home, "reports")
    titled = {"Crons": crons, "Reports": reports}
    monkeypatch.setattr(mailbox, "find_canonical_live_owner",
                        lambda h, title="Bot Chat": titled.get(title, bot_chat))
    mailbox.deliver_to_live_owner(home, reports, "weekly", delivery_id="e" * 32, chat_title="Reports")
    poll, submitted = _poller(home)
    assert poll(crons) is False
    assert poll(bot_chat) is False
    assert poll(reports) is True
    assert submitted == [("live-reports", "weekly")]


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
