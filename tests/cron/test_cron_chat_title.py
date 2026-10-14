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
        return None

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
                        lambda home, owner: claims.append(owner) or {"id": "receipt", "message": "cron out"})
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
    assert claims == [crons] and submitted == [("live-crons", "cron out")]
