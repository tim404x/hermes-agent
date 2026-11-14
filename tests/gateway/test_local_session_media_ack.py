"""A local route acknowledges a file "send" instead of logging a false delivery failure.

``LocalSessionAdapter.send()`` only acknowledges: a turn's output rides the canonical event
stream and a viewer replays the durable transcript. The base adapter still post-processes every
reply (``extract_local_files`` / ``MEDIA:``) and calls ``send_document`` & co, whose default
logs ``[Local] send_document fallback: native file send unavailable for <path>`` at WARNING and
posts a "couldn't send" notice through that same acknowledging ``send()``. Nothing was lost and
nothing was shown, but every Outbox / oneshot worker reply that named its result file left a
warning in errors.log that read like a dropped attachment.
"""
import logging
from types import SimpleNamespace

import pytest

from gateway.session_local import LocalSessionAdapter


def _adapter():
    return LocalSessionAdapter(SimpleNamespace(runner=None, sessions={}))


@pytest.mark.asyncio
@pytest.mark.parametrize("method, args", [
    ("send_document", ("/tmp/outbox/jobs/draft-1/done.json",)),
    ("send_video", ("/tmp/clip.mp4",)),
    ("send_voice", ("/tmp/note.ogg",)),
])
async def test_local_route_acknowledges_media_without_a_false_failure(method, args, caplog):
    adapter = _adapter()
    sent = []

    async def _recording_send(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)
        return SimpleNamespace(success=True, message_id="x")

    adapter.send = _recording_send
    with caplog.at_level(logging.DEBUG):
        result = await getattr(adapter, method)("local-chat", *args, caption="see file")
    assert result.success is True and result.message_id
    # No "couldn't send" notice enters the chat, and no warning claims a failed send.
    assert sent == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # The path is still traceable at debug level.
    assert any(args[0] in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
