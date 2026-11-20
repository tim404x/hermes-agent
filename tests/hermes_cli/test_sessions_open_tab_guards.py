"""``hermes sessions open-tab`` guards: a taken title fails cleanly, and the canonical Bot Chat is never a tab.

Regression (R4, 20 Nov 2026): ``open-tab <id> --title <a title another session holds>`` died on a
``TitleConflict`` traceback, and ``open-tab`` stamped the canonical "Bot Chat" as a Desktop task tab.
"""

import json
import sqlite3

from hermes_cli.sessions_cmd import cmd_sessions
from tests.hermes_cli.test_sessions_open_tab import _parse, _seed, _windows


def _cells(home, sid):
    with sqlite3.connect(home / "state.db") as conn:
        row = conn.execute("SELECT title, side_window_json, origin_json FROM sessions WHERE id = ?", (sid,)).fetchone()
    return row[0], row[1], (json.loads(row[2]) if row[2] else None)


def test_open_tab_with_a_taken_title_fails_cleanly_and_stamps_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path, "local-a", title="Taken")
    _seed(tmp_path, "local-b")

    assert cmd_sessions(_parse("open-tab", "local-b", "--title", "Taken")) == 1

    assert capsys.readouterr().out.startswith("Error: ")
    assert _windows(tmp_path) == []
    assert _cells(tmp_path, "local-b") == (None, None, None)


def test_open_tab_refuses_the_canonical_bot_chat(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path, "local-botchat", title="Bot Chat")

    assert cmd_sessions(_parse("open-tab", "local-botchat", "--by", "neo")) == 1

    assert "Error: " in capsys.readouterr().out
    assert _windows(tmp_path) == []
    assert _cells(tmp_path, "local-botchat") == ("Bot Chat", None, None)


def test_open_tab_refuses_to_retitle_a_chat_as_bot_chat(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path, "local-c", title="Scratch")

    assert cmd_sessions(_parse("open-tab", "local-c", "--title", "  bot   CHAT ")) == 1

    assert "Error: " in capsys.readouterr().out
    assert _windows(tmp_path) == []
    assert _cells(tmp_path, "local-c") == ("Scratch", None, None)
