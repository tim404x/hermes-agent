"""``message_agent`` side windows: a teammate that is busy gets the DM in its own titled session.

When the target's canonical Bot Chat is mid-turn, queueing behind it (live mailbox or CLI turn
lock) makes the sender wait up to 30 minutes, and the DM lands in the middle of whatever the
teammate is doing with the user. Instead the DM opens a side window: ``hermes -p <bot> chat
-c <title> --create-if-missing``, a normal titled session that shows under that bot in the app.
An idle teammate keeps the main Bot Chat. Stdlib-only at import: the delivery runner imports
this after its own bootstrap.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

# An unfinished Bot Chat tip older than this is a crashed turn, not a busy one.
BUSY_TURN_MAX_AGE_SECONDS = 15 * 60
TITLE_MAX_CHARS = 60
# The second try when the titled window is open live on another surface (SESSION_NOT_OWNED).
RETRY_SUFFIX = " (2)"
_FALLBACK_WORDS = 6
_QUOTES_RE = re.compile(r"[\"'`\u2018\u2019\u201c\u201d]")


def bot_chat_busy(profile_home: Path | str) -> tuple[bool, str]:
    """``(busy, why)`` for a local teammate's canonical Bot Chat. Busy when its tip is mid-turn,
    its live mailbox already holds a pending delivery, or a CLI delivery turn holds its turn
    lock. Any read error answers not busy (today's path) and never raises."""
    try:
        home = Path(profile_home)
        why = _mid_turn(home) or _mailbox_waiting(home) or _turn_locked(home)
    except Exception:
        logger.debug("message_agent: Bot Chat busy probe failed for %s", profile_home, exc_info=True)
        return False, ""
    return bool(why), why


def _mid_turn(home: Path) -> str:
    """The tip's newest row is not a finished assistant reply, and it is fresh."""
    if not (home / "state.db").is_file():
        return ""
    from hermes_state import SessionDB
    from tools.bot_mode_probe import BOT_CHAT_TITLE

    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        row = db.get_session_by_title(BOT_CHAT_TITLE)
        tip = db.get_compression_tip(row["id"]) if row else None
        newest = db.get_messages(tip, latest=True, limit=1) if tip else []
    finally:
        db.close()
    if not newest:
        return ""
    last = newest[-1]
    if last.get("role") == "assistant" and not last.get("tool_calls"):
        return ""
    if time.time() - float(last.get("timestamp") or 0) > BUSY_TURN_MAX_AGE_SECONDS:
        return ""
    return "mid-turn in its Bot Chat"


def _mailbox_waiting(home: Path) -> str:
    """A queued or claimed live delivery that a runner may still be waiting on."""
    from tools.bot_live_delivery import pending_deliveries
    from tools.bot_mode_dm import _LIVE_WAIT_MAX_SECONDS

    # Claims never expire; past the runner's wait budget nobody is waiting on the receipt.
    cutoff = time.time_ns() - _LIVE_WAIT_MAX_SECONDS * 1_000_000_000
    if any(record["created_at"] > cutoff for record in pending_deliveries(home)):
        return "a message is already waiting on its Bot Chat"
    return ""


def _turn_locked(home: Path) -> str:
    from tools.bot_mode_probe import _hermes_root, _profile_name
    from tools.bot_relay import turn_lock_held

    if turn_lock_held(_hermes_root(home), _profile_name(home)):
        return "a delivery turn is running in its Bot Chat"
    return ""


def side_window_title(topic: str, sender: str, message: str) -> str:
    """The window title: the sender's ``topic``, else ``"<sender>: <first words>"``. One line,
    no quotes, at most ``TITLE_MAX_CHARS``, never an option-looking or the canonical Bot Chat."""
    words = " ".join(str(message or "").split()[:_FALLBACK_WORDS])
    title = _clean(topic) or _clean(f"{sender}: {words}")
    from tools.bot_mode_probe import BOT_CHAT_TITLE

    if title.casefold() == BOT_CHAT_TITLE.casefold():
        title = f"{title} (side window)"
    return title


def _clean(text: str) -> str:
    text = " ".join(_QUOTES_RE.sub("", str(text or "")).split()).lstrip("-").strip()
    return text[:TITLE_MAX_CHARS].rstrip()


def window_title(argv: Sequence[str]) -> str:
    """The session title of a ``hermes -p <bot> chat ... -c <title>`` transport argv."""
    return argv[list(argv).index("-c") + 1]


def retitled(argv: Sequence[str]) -> list[str]:
    """The same transport argv aimed at ``<title> (2)``."""
    out = list(argv)
    at = out.index("-c") + 1
    out[at] = out[at] + RETRY_SUFFIX
    return out


def side_window_lock_name(profile: str, title: str) -> str:
    """Turn-lock key for one (profile, title) window: same title serializes, others run in
    parallel, and none waits behind the profile's own Bot Chat lock (keyed by the bare name)."""
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
    return f"{profile[:40]}-sw-{digest}"
