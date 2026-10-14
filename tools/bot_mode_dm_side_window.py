"""``message_agent`` side windows: a teammate that is busy gets the DM in its own titled session.

When the target's canonical Bot Chat is mid-turn, queueing behind it (live mailbox or CLI turn
lock) makes the sender wait up to 30 minutes, and the DM lands in the middle of whatever the
teammate is doing with the user. Instead the DM opens a side window: ``hermes -p <bot> chat
-c <title> --create-if-missing``, a normal titled session that shows under that bot in the app.
Where a DM lands is ``bot_mode.dm_route`` (``dm_route``): ``side_window`` (the default) opens
every DM in a side window, busy or idle, one tab per conversation (``resolve_window``); ``auto``
opens one only while the Bot Chat is busy; ``main`` always uses the Bot Chat. Stdlib-only at
import: the delivery runner imports this after its own bootstrap.
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
# A side window idle this long is a finished conversation: the next DM on its key opens a new tab.
REUSE_MAX_IDLE_SECONDS = 6 * 60 * 60
# Numbered tabs tried before giving up on a free title ("<title> (2)" ... "<title> (N)").
_MAX_NUMBERED_TABS = 50
_NUMBERED_RE = re.compile(r" \((\d{1,2})\)$")

ROUTE_SIDE_WINDOW, ROUTE_AUTO, ROUTE_MAIN = "side_window", "auto", "main"
DEFAULT_DM_ROUTE = ROUTE_SIDE_WINDOW
_ROUTES = (ROUTE_SIDE_WINDOW, ROUTE_AUTO, ROUTE_MAIN)
_QUOTES_RE = re.compile(r"[\"'`\u2018\u2019\u201c\u201d]")

# A one-message ``route: "main"`` override is honoured only when the human's own words in the
# sender's CURRENT turn ask for the main chat: "handoff to Neo in main chat", "drop it in his Bot
# Chat", "mets-le dans le chat principal". A teammate's DM, a cron or a completion notice never
# counts, even when it quotes the user, so no bot can promote its own message into a main chat.
MAIN_CHAT_ASK_RE = re.compile(
    r"\bmain\s+(?:bot\s+)?(?:chat|thread|session|window|tab|convo|conversation)s?\b"
    r"|\bbot\s+chat\b"
    r"|\b(?:chat|conversation|fil|discussion)\s+principale?\b",
    re.IGNORECASE,
)
BOT_DM_PREFIX = "Message from \U0001f916"
_TURN_TEXTS_ATTR = "_turn_human_texts"


def _plain_text(content) -> str:
    """The text of a user message's content: a string, or the text parts of a multimodal list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content
                         if isinstance(part, dict) and part.get("type") in ("text", "input_text"))
    return ""


def record_turn_human_text(agent, content, *, new_turn: bool = False, synthesized: bool = False) -> None:
    """Remember what the human said this turn (the turn's user message, then any mid-turn steer)
    so ``message_agent`` can check a ``route: "main"`` request against it. ``new_turn`` clears the
    previous turn's texts. Synthesized turns and teammate DMs are not the human. Never raises."""
    try:
        texts = [] if new_turn else list(getattr(agent, _TURN_TEXTS_ATTR, None) or [])
        text = _plain_text(content).strip()
        if text and not synthesized and not text.startswith(BOT_DM_PREFIX):
            texts.append(text)
        setattr(agent, _TURN_TEXTS_ATTR, texts)
    except Exception:
        logger.debug("message_agent: could not record the turn's human text", exc_info=True)


def human_asked_main_chat(agent) -> bool:
    """True when the human, in the sender's current turn, explicitly asked for a main chat."""
    try:
        return any(MAIN_CHAT_ASK_RE.search(t) for t in (getattr(agent, _TURN_TEXTS_ATTR, None) or []))
    except Exception:
        return False


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


def dm_route(profile_home: Path | str) -> str:
    """``bot_mode.dm_route`` for a DM into ``profile_home``: the receiver's own config.yaml, else the
    machine-root config.yaml, else ``DEFAULT_DM_ROUTE``. A missing, unreadable or unknown value
    falls through to the next source. Read once per delivery; never raises."""
    try:
        from tools.bot_mode_probe import _hermes_root

        home = Path(profile_home)
        sources = [home / "config.yaml", _hermes_root(home) / "config.yaml"]
    except Exception:
        logger.debug("message_agent: dm_route sources unresolvable for %s", profile_home, exc_info=True)
        return DEFAULT_DM_ROUTE
    for path in dict.fromkeys(sources):
        route = _configured_route(path)
        if route:
            return route
    return DEFAULT_DM_ROUTE


def _configured_route(path: Path) -> str:
    try:
        from tools.bot_mode_probe import _read_yaml_dict

        section = (_read_yaml_dict(path, "dm_route") or {}).get("bot_mode")
        value = str((section if isinstance(section, dict) else {}).get("dm_route") or "")
    except Exception:
        logger.debug("message_agent: could not read bot_mode.dm_route from %s", path, exc_info=True)
        return ""
    value = value.strip().lower().replace("-", "_")
    if value and value not in _ROUTES:
        logger.warning("message_agent: ignoring unknown bot_mode.dm_route %r in %s", value, path)
    return value if value in _ROUTES else ""


def side_window_key(sender_id: str, target: str, topic: str, origin_session: str) -> str:
    """The conversation a side window belongs to: the sender, the target, and the normalized topic,
    or (without a topic) the sender's originating session, so one back-and-forth stays in one tab."""
    norm = _clean(topic).casefold()
    tail = f"topic:{norm}" if norm else f"session:{origin_session or '-'}"
    return f"{sender_id}>{target}|{tail}"


def resolve_window(profile_home: Path | str, profile: str, key: str, title: str,
                   now: float | None = None) -> tuple[str, bool]:
    """``(title, reused)`` for a DM on conversation ``key`` into ``profile``'s side windows.

    Reuses the newest window stamped with ``key`` that was active within ``REUSE_MAX_IDLE_SECONDS``
    and is not busy (mid-turn, or its window turn lock held). Otherwise a free title: ``title``,
    or, when that conversation's live tab is busy, ``"<its title> (2)"``; a title another session
    already holds gets the next free number, so a new conversation never lands in an old tab.
    Raises on a store error: the caller falls back to the ``auto`` route."""
    now = time.time() if now is None else now
    home = Path(profile_home)
    if not (home / "state.db").is_file():
        return title, False
    from hermes_state import SessionDB
    from tools.bot_mode_probe import _hermes_root
    from tools.bot_relay import turn_lock_held

    root = _hermes_root(home)
    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        busy_tab = ""
        for window in db.find_side_windows(key):
            if not window["title"] or now - window["last_active"] > REUSE_MAX_IDLE_SECONDS:
                continue
            if session_mid_turn(db, window["id"], now) or turn_lock_held(
                    root, side_window_lock_name(profile, window["title"])):
                busy_tab = busy_tab or window["title"]
                continue
            return window["title"], True
        return _free_title(db, busy_tab or title, numbered=bool(busy_tab)), False
    finally:
        db.close()


def _free_title(db, title: str, *, numbered: bool) -> str:
    """``title`` when no session holds it (``numbered``: start at ``(2)``), else the next free
    ``"<stem> (N)"``."""
    match = _NUMBERED_RE.search(title) if numbered else None
    stem = title[:match.start()] if match else title
    first = int(match.group(1)) + 1 if match else 2
    candidates = ([] if numbered else [title]) + [f"{stem} ({n})" for n in range(first, _MAX_NUMBERED_TABS + 1)]
    for candidate in candidates:
        if db.resolve_session_by_title(candidate) is None:
            return candidate
    return candidates[-1]


def side_window_title(topic: str, sender: str, message: str) -> str:
    """The window title: the sender's ``topic``, else ``"<sender>: <first words>"``. One line,
    no quotes, at most ``TITLE_MAX_CHARS``, never an option-looking or the canonical Bot Chat."""
    # "Neo | Chief of Staff" -> "Neo": a bot title is "Name | Role"; the role only bloats a tab title.
    short = re.split(r"\s+[|\u00b7\u2022\u2014\u2013]\s+", str(sender or "").strip())[0] or str(sender or "")
    # Drop a leading self-introduction ("Neo here:") so the title starts with the subject.
    body = re.sub(rf"^\s*{re.escape(short)}\s+here\s*[:,.\u2014-]?\s*", "", str(message or ""), flags=re.IGNORECASE)
    words = " ".join(body.split()[:_FALLBACK_WORDS])
    title = _clean(topic) or _clean(f"{short}: {words}")
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


def stamp_side_window(profile_home: Path | str, title: str, *, author: dict | None = None,
                      key: str | None = None) -> str | None:
    """Mark the session a side-window turn is about to run, so the desktop opens it as a tab.

    Resolves ``title`` exactly as the transport's ``chat -c <title>`` will (id, then title with its
    ``#N`` continuation, then the compression tip) and creates the titled CLI session when it is
    new, the same row ``--create-if-missing`` would mint, so the tab can appear before the turn
    starts. ``key`` records the conversation (``side_window_key``) so its next DM finds this tab.
    Runs inside the window's turn lock. Returns the stamped id; a failure is logged and
    answers None, because a missing tab must never cost the DM."""
    db = None
    try:
        from hermes_state_registry import acquire

        db = acquire(Path(profile_home) / "state.db")
        session_id = _resolve_window(db, title) or _create_window(db, title)
        if session_id and db.mark_side_window(session_id, by=str((author or {}).get("id") or ""), key=key):
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
