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


def unfinished_turn_at(db, session_id: str) -> float | None:
    """When *session_id*'s newest row was written, if that row leaves a turn open: role user or
    tool, or an assistant row still carrying tool_calls. None when the session is empty or ends on
    a finished assistant reply. Clock-free, so a caller may memoise it per store state."""
    newest = db.get_messages(session_id, latest=True, limit=1)
    if not newest:
        return None
    last = newest[-1]
    if last.get("role") == "assistant" and not last.get("tool_calls"):
        return None
    return float(last.get("timestamp") or 0)


def turn_is_fresh(started_at: float | None, now: float | None = None) -> bool:
    """An open turn older than ``BUSY_TURN_MAX_AGE_SECONDS`` is a crashed turn, not a busy one."""
    if started_at is None:
        return False
    return (time.time() if now is None else now) - started_at <= BUSY_TURN_MAX_AGE_SECONDS


def session_mid_turn(db, session_id: str, now: float | None = None) -> bool:
    """*session_id* is mid-turn right now: its newest row leaves a turn open, and it is fresh."""
    return turn_is_fresh(unfinished_turn_at(db, session_id), now)


def _mid_turn(home: Path) -> str:
    """The Bot Chat tip is mid-turn (``session_mid_turn``)."""
    if not (home / "state.db").is_file():
        return ""
    from hermes_state import SessionDB
    from tools.bot_mode_probe import BOT_CHAT_TITLE

    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        row = db.get_session_by_title(BOT_CHAT_TITLE)
        tip = db.get_compression_tip(row["id"]) if row else None
        busy = bool(tip) and session_mid_turn(db, tip)
    finally:
        db.close()
    return "mid-turn in its Bot Chat" if busy else ""


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


def stamp_side_window(profile_home: Path | str, title: str, *, author: dict | None = None) -> str | None:
    """Mark the session a side-window turn is about to run, so the desktop opens it as a tab.

    Resolves ``title`` exactly as the transport's ``chat -c <title>`` will (id, then title with its
    ``#N`` continuation, then the compression tip) and creates the titled CLI session when it is
    new, the same row ``--create-if-missing`` would mint, so the tab can appear before the turn
    starts. Runs inside the window's turn lock. Returns the stamped id; a failure is logged and
    answers None, because a missing tab must never cost the DM."""
    db = None
    try:
        from hermes_state_registry import acquire

        db = acquire(Path(profile_home) / "state.db")
        session_id = _resolve_window(db, title) or _create_window(db, title)
        if session_id and db.mark_side_window(session_id, by=str((author or {}).get("id") or "")):
            return session_id
        return None
    except Exception:
        logger.warning("message_agent: could not stamp side window %r in %s", title, profile_home, exc_info=True)
        return None
    finally:
        if db is not None:
            from hermes_state_registry import release_or_close

            release_or_close(db)


def _resolve_window(db, title: str) -> str | None:
    """``hermes_cli.main._resolve_session_by_name_or_id`` against the target's own store."""
    row = db.get_session(title)
    session_id = row["id"] if row else db.resolve_session_by_title(title)
    return (db.get_compression_tip(session_id) or session_id) if session_id else None


def _create_window(db, title: str) -> str | None:
    from hermes_state_ids import new_session_id

    session_id = new_session_id()
    db.create_session(session_id, source="cli")
    try:
        db.set_session_title(session_id, title)
    except ValueError:
        # Another writer took the title first: drop our empty row and use theirs.
        db.delete_session_if_empty(session_id)
        return _resolve_window(db, title)
    return session_id


def side_window_lock_name(profile: str, title: str) -> str:
    """Turn-lock key for one (profile, title) window: same title serializes, others run in
    parallel, and none waits behind the profile's own Bot Chat lock (keyed by the bare name)."""
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
    return f"{profile[:40]}-sw-{digest}"
