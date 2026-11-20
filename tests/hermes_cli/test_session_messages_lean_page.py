"""Lean transcript pages: ``image_refs=true`` and ``replay_fields=false`` on the messages routes.

Tim pastes screenshots into his Bot Chats; every page read re-shipped each one as base64 (a
439 KB PNG on Kepler's page, 615 KB gzip per bot switch). The Desktop renders the row's
``@image:<path>`` reference itself, so it opts in to pages without the inline copy, and without
the opaque provider replay blobs it never reads. Other clients (the iPhone app) keep the
verbatim rows by default.
"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

DATA_URI = "data:image/png;base64," + "a" * 256
SIGNATURE = "SIG" + "x" * 200
REDACTED = "RED" + "y" * 200
ENCRYPTED = "ENC" + "z" * 200


def _user(text, images=1):
    return {"role": "user", "content": [{"type": "text", "text": text},
                                        *({"type": "image_url", "image_url": {"url": DATA_URI}}
                                          for _ in range(images))]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    (home / "images").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _client(home, rows):
    from persistence.conversation.messages import NewMessage
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_session

    with open_persistence(profile_scope(home=home)) as store:
        seed_session(store, "img-chat", "desktop")
        store.messages.append_batch("img-chat", [NewMessage.from_live(m) for m in rows])

    from hermes_cli.web_routers.sessions import manage_router

    app = FastAPI()
    app.include_router(manage_router)
    return TestClient(app)


def _page(client, query=""):
    return client.get(f"/api/sessions/img-chat/messages?limit=20&order=oldest{query}").json()["messages"]


def _user_rows(rows):
    return [m for m in rows if m["role"] == "user"]


def _png(home, name):
    path = home / "images" / name
    path.write_bytes(b"\x89PNG fake")
    return str(path)


def test_default_page_is_verbatim(home):
    shot = _png(home, "a.png")
    with _client(home, [_user(f"look\n@image:{shot}"), {"role": "assistant", "content": "ok"}]) as client:
        assert DATA_URI in str(_user_rows(_page(client))[0]["content"])


def test_image_refs_drops_inline_copy_of_a_referenced_file(home):
    shot = _png(home, "a.png")
    with _client(home, [_user(f"look\n@image:{shot}"), {"role": "assistant", "content": "ok"}]) as client:
        row = _user_rows(_page(client, "&image_refs=true"))[0]
    assert DATA_URI not in str(row["content"])
    assert row["content"] == [{"type": "text", "text": f"look\n@image:{shot}"}]


def test_image_refs_covers_quoted_refs_and_attachment_hints(home):
    one, two = _png(home, "one two.png"), _png(home, "b.png")
    text = f'look\n@image:"{one}"\n[Image attached at: {two}]'
    with _client(home, [_user(text, images=2)]) as client:
        row = _user_rows(_page(client, "&image_refs=true"))[0]
    assert DATA_URI not in str(row["content"])


def test_image_refs_keeps_bytes_when_the_file_is_gone(home):
    with _client(home, [_user(f"look\n@image:{home / 'images' / 'deleted.png'}")]) as client:
        assert DATA_URI in str(_user_rows(_page(client, "&image_refs=true"))[0]["content"])


def test_image_refs_keeps_bytes_without_a_reference_per_image(home):
    shot = _png(home, "a.png")
    with _client(home, [_user("no ref at all"), _user(f"two images\n@image:{shot}", images=2)]) as client:
        rows = _user_rows(_page(client, "&image_refs=true"))
    assert all(DATA_URI in str(row["content"]) for row in rows)


def test_image_refs_never_touches_tool_rows(home):
    tool_row = {"role": "tool", "tool_call_id": "c1", "content": [
        {"type": "text", "text": f"@image:{_png(home, 'a.png')}"},
        {"type": "image_url", "image_url": {"url": DATA_URI}}]}
    call = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "vision_analyze", "arguments": "{}"}}]}
    with _client(home, [{"role": "user", "content": "hi"}, call, tool_row]) as client:
        rows = _page(client, "&image_refs=true")
    assert DATA_URI in str(next(m for m in rows if m["role"] == "tool")["content"])


def _assistant_with_replay():
    return {"role": "assistant", "content": "answer", "reasoning": "thought it through",
            "reasoning_details": [
                {"type": "thinking", "thinking": "thought it through", "signature": SIGNATURE},
                {"type": "redacted_thinking", "data": REDACTED}],
            "codex_reasoning_items": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "s"}],
                                       "encrypted_content": ENCRYPTED}]}


def test_replay_fields_false_strips_opaque_blobs_keeps_reasoning_text(home):
    with _client(home, [{"role": "user", "content": "q"}, _assistant_with_replay()]) as client:
        default = str(_page(client))
        lean = next(m for m in _page(client, "&replay_fields=false") if m["role"] == "assistant")
    assert SIGNATURE in default and REDACTED in default and ENCRYPTED in default
    assert not any(blob in str(lean) for blob in (SIGNATURE, REDACTED, ENCRYPTED))
    # The store ships replay columns as JSON TEXT; the lean page keeps that wire type.
    details, items = (json.loads(lean[k]) if isinstance(lean[k], str) else lean[k]
                      for k in ("reasoning_details", "codex_reasoning_items"))
    assert details == [{"type": "thinking", "thinking": "thought it through"}, {"type": "redacted_thinking"}]
    assert items[0]["summary"] == [{"type": "summary_text", "text": "s"}]
    assert lean["reasoning"] == "thought it through"


def test_around_page_honors_both_flags(home):
    shot = _png(home, "a.png")
    with _client(home, [_user(f"look\n@image:{shot}"), _assistant_with_replay()]) as client:
        first = _page(client)[0]["id"]
        verbatim = client.get(f"/api/sessions/img-chat/messages/around?row_id={first}").json()["messages"]
        lean = client.get(f"/api/sessions/img-chat/messages/around?row_id={first}"
                          "&image_refs=true&replay_fields=false").json()["messages"]
    assert DATA_URI in str(verbatim) and SIGNATURE in str(verbatim)
    assert DATA_URI not in str(lean) and SIGNATURE not in str(lean)
