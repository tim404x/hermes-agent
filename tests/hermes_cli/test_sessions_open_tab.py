"""``hermes sessions open-tab``: the supported way for a bot to make a chat a Desktop task tab.

Regression (P3c, 19 Nov 2026): Neo stamped its own task tabs by hand-editing ``state.db`` because no
command existed. The command writes the same side-window stamp ``message_agent`` writes
(``SessionRepository.mark_side_window``), for any chat, a gateway-created one included.
"""

import argparse
import json

from hermes_cli.sessions_cmd import cmd_sessions
from hermes_cli.subcommands.sessions import build_sessions_parser


def _parse(*argv):
    parser = argparse.ArgumentParser()
    build_sessions_parser(parser.add_subparsers(dest="command"), cmd_sessions=cmd_sessions)
    return parser.parse_args(["sessions", *argv])


def _store(home):
    from persistence_assembly import open_persistence, profile_scope

    return open_persistence(profile_scope(home=home), eager=True)


def _seed(home, session_id, *, title=None, origin=None):
    from persistence import SessionId
    from persistence.conversation.sessions import NewSession

    with _store(home) as store:
        store.sessions.create(NewSession(session_id=SessionId(session_id), source="local", origin=origin))
        if title:
            store.sessions.set_title(SessionId(session_id), title)


def _windows(home):
    with _store(home) as store:
        return [(w.id, w.title, w.by) for w in store.sessions.list_side_windows()]


def test_open_tab_stamps_a_gateway_created_chat_and_keeps_its_origin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    origin = {"platform": "local", "chat_id": "local-abc", "user_id": "uid:1"}
    _seed(tmp_path, "local-abc", title="Sales | Deal heatmap", origin=origin)

    assert cmd_sessions(_parse("open-tab", "local-abc", "--by", "neo")) in (None, 0)

    assert _windows(tmp_path) == [("local-abc", "Sales | Deal heatmap", "bot:neo")]
    with _store(tmp_path) as store:
        assert json.loads(store.sessions.get("local-abc").origin_json) == origin
    assert "local-abc" in capsys.readouterr().out


def test_open_tab_can_set_the_tab_title(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path, "local-xyz")

    assert cmd_sessions(_parse("open-tab", "local-xyz", "--title", "Lab | Emmanuel's compute model")) in (None, 0)

    assert _windows(tmp_path) == [("local-xyz", "Lab | Emmanuel's compute model", "")]


def test_open_tab_on_an_unknown_session_fails_and_stamps_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path, "local-abc", title="kept")

    assert cmd_sessions(_parse("open-tab", "nope")) == 1
    assert _windows(tmp_path) == []
