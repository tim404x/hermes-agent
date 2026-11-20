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
# A title built from the message (no topic) stays short: about one tab's width.
_FALLBACK_MAX_CHARS = 48
# A side window idle this long is a finished conversation: the next DM on its key opens a new tab.
REUSE_MAX_IDLE_SECONDS = 6 * 60 * 60
# Numbered tabs tried before giving up on a free title ("<title> (2)" ... "<title> (N)").
_MAX_NUMBERED_TABS = 50
_NUMBERED_RE = re.compile(r" \((\d{1,2})\)$")

ROUTE_SIDE_WINDOW, ROUTE_AUTO, ROUTE_MAIN = "side_window", "auto", "main"
DEFAULT_DM_ROUTE = ROUTE_SIDE_WINDOW
_ROUTES = (ROUTE_SIDE_WINDOW, ROUTE_AUTO, ROUTE_MAIN)
_LEGACY_QUOTES_RE = re.compile(r"[\"'`\u2018\u2019\u201c\u201d]")
# Title cleaning: double quotes and backticks go everywhere; a single quote only where it wraps a word,
# so "Emmanuel's" and "Tim\u2019s" keep their apostrophe.
_DOUBLE_QUOTES_RE = re.compile(r"[\"`\u201c\u201d]")
_WRAPPING_SINGLE_QUOTE_RE = re.compile(r"(?<!\w)['\u2018\u2019]|['\u2018\u2019](?!\w)")
_MD_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\([^)\s]*\)")
_BRACKET_TAG_RE = re.compile(r"\[[^\[\]\n]{0,80}\]")
_MENTION_RE = re.compile(r"^\s*@[\w.-]+[,:]?\s+")
_GREETING_RE = re.compile(r"^\s*(?:hey|hi|hello|hiya|yo|ok|okay)\b(?:\s+@?[\w.-]+){0,2}?\s*[,:!.\u2014-]\s*",
                          re.IGNORECASE)
_INTRO_RE = re.compile(r"^\s*@?[\w.-]+\s+here\b\s*[:,.!\u2014-]?\s*", re.IGNORECASE)
# The first clause ends at sentence punctuation, a colon or semicolon (before a space or the end), a
# line break, or a spaced dash.
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;:](?=\s|$)|\n|\s[-\u2013\u2014]\s")
_TITLE_STOP_WORDS = frozenset({"the", "a", "an", "to", "your", "my", "our", "of", "for", "and", "or", "with",
                               "on", "in", "at", "by", "from", "is", "are"})
_TRAIL_PUNCT = " ,;:.!?-\u2013\u2014"

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


def unfinished_turn_at(store, session_id: str) -> float | None:
    """When *session_id*'s newest active row was written, if that row leaves a turn open: role user
    or tool, or an assistant row still carrying tool_calls. None when the session is empty or ends on
    a finished assistant reply. ``store`` is a storage-interface handle (``persistence_assembly``).
    Clock-free, so a caller may memoise it per store state."""
    from persistence.conversation.messages import TranscriptRead

    newest = store.messages.read_transcript(session_id, TranscriptRead(latest=True, limit=1))
    if not newest:
        return None
    last = newest[-1]
    if last.role == "assistant" and not last.tool_calls:
        return None
    return float(last.timestamp or 0)


def turn_is_fresh(started_at: float | None, now: float | None = None) -> bool:
    """An open turn older than ``BUSY_TURN_MAX_AGE_SECONDS`` is a crashed turn, not a busy one."""
    if started_at is None:
        return False
    return (time.time() if now is None else now) - started_at <= BUSY_TURN_MAX_AGE_SECONDS


def session_mid_turn(store, session_id: str, now: float | None = None) -> bool:
    """*session_id* is mid-turn right now: its newest row leaves a turn open, and it is fresh."""
    return turn_is_fresh(unfinished_turn_at(store, session_id), now)


def _mid_turn(home: Path) -> str:
    """The Bot Chat tip is mid-turn (``session_mid_turn``)."""
    if not (home / "state.db").is_file():
        return ""
    from persistence_assembly import open_persistence_reader
    from tools.bot_mode_probe import BOT_CHAT_TITLE

    with open_persistence_reader(home) as store:
        row = store.sessions.get_by_title(BOT_CHAT_TITLE)
        tip = store.lineage.tip(row.session_id) if row else None
        busy = bool(tip) and session_mid_turn(store, tip)
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
    norm = _key_norm(topic)
    tail = f"topic:{norm}" if norm else f"session:{origin_session or '-'}"
    return f"{sender_id}>{target}|{tail}"


def resolve_window(profile_home: Path | str, profile: str, key: str, title: str,
                   now: float | None = None) -> tuple[str, bool]:
    """``(title, reused)`` for a DM on conversation ``key`` into ``profile``'s side windows.

    Reuses the newest window stamped with ``key`` that was active within ``REUSE_MAX_IDLE_SECONDS``,
    busy or not: one conversation keeps one tab. A follow-up into a busy window (mid-turn, or its
    window turn lock held, ``window_busy``) is queued into it through the window's live owner by the
    delivery runner, never opened as a ``"<title> (2)"`` sibling (Tim, 19 Nov 2026). Otherwise a free
    title: ``title``, or, when another session already holds it, the next free ``"<title> (N)"``, so a
    new conversation never lands in an old tab. Raises on a store error: the caller falls back to the
    ``auto`` route."""
    now = time.time() if now is None else now
    home = Path(profile_home)
    if not (home / "state.db").is_file():
        return title, False
    from persistence_assembly import open_persistence_reader

    with open_persistence_reader(home) as store:
        for window in store.sessions.find_side_windows(key):
            if window.title and now - window.last_active <= REUSE_MAX_IDLE_SECONDS:
                return window.title, True
        return _free_title(store, title, numbered=False), False


def window_busy(profile_home: Path | str, profile: str, title: str, now: float | None = None) -> bool:
    """The titled side window is busy right now: its live tip is mid-turn, or its window turn lock is
    held (a delivery runner is in it). Never raises; unknown is not busy."""
    try:
        from persistence_assembly import open_persistence_reader
        from tools.bot_mode_probe import _hermes_root
        from tools.bot_relay import turn_lock_held

        home = Path(profile_home)
        if turn_lock_held(_hermes_root(home), side_window_lock_name(profile, title)):
            return True
        if not (home / "state.db").is_file():
            return False
        with open_persistence_reader(home) as store:
            session_id = _resolve_window(store, title)
            return bool(session_id) and session_mid_turn(store, session_id, now)
    except Exception:
        logger.debug("message_agent: could not read side window %r in %s", title, profile_home, exc_info=True)
        return False


def _free_title(store, title: str, *, numbered: bool) -> str:
    """``title`` when no session holds it (``numbered``: start at ``(2)``), else the next free
    ``"<stem> (N)"``."""
    match = _NUMBERED_RE.search(title) if numbered else None
    stem = title[:match.start()] if match else title
    first = int(match.group(1)) + 1 if match else 2
    candidates = ([] if numbered else [title]) + [f"{stem} ({n})" for n in range(first, _MAX_NUMBERED_TABS + 1)]
    for candidate in candidates:
        if store.sessions.resolve_by_title(candidate) is None:
            return candidate
    return candidates[-1]


def side_window_title(topic: str, sender: str, message: str) -> str:
    """The window title. A ``topic`` is used as typed: one line, wrapping quotes and backticks dropped,
    apostrophes kept, cut on a word boundary with a single ``…`` past ``TITLE_MAX_CHARS``. Without one,
    the message's first clause, past any bracket tag (``[no reply needed]``), greeting or self-intro
    (``Hey Kepler,``, ``Neo here:``), cut on a word boundary at ``_FALLBACK_MAX_CHARS``, never ending
    on a stop word, sentence case. No sender prefix: the tab already sits under that bot. Never
    option-looking, never the canonical Bot Chat."""
    title = _cut(_clean(topic), TITLE_MAX_CHARS) or _fallback_title(sender, message)
    from tools.bot_mode_probe import BOT_CHAT_TITLE

    if title.casefold() == BOT_CHAT_TITLE.casefold():
        title = f"{title} (side window)"
    return title


def _fallback_title(sender: str, message: str) -> str:
    """The first clause of ``message`` as a short title (see ``side_window_title``)."""
    text = _MD_LINK_RE.sub(r"\1", str(message or ""))
    text = _BRACKET_TAG_RE.sub(" ", text).strip()
    for _ in range(3):  # "Hey Kepler, Neo here: ..." peels in two passes
        peeled = _INTRO_RE.sub("", _GREETING_RE.sub("", _MENTION_RE.sub("", text, count=1), count=1), count=1)
        if peeled == text:
            break
        text = peeled.strip()
    parts = _CLAUSE_SPLIT_RE.split(text, maxsplit=1)
    clause = _clean(parts[0])
    rest = _CLAUSE_SPLIT_RE.split(text[len(parts[0]):].lstrip(":").strip(), maxsplit=1)[0] if len(parts) > 1 else ""
    if len(clause.split()) < 3 and text[len(parts[0]):].startswith(":") and _clean(rest):
        clause = f"{clause}: {_clean(rest)}"  # "Update: the heatmap is live" reads better whole
    title = _cut(clause, _FALLBACK_MAX_CHARS, strip_stop_words=True)
    if not title:
        short = re.split(r"\s+[|\u00b7\u2022\u2014\u2013]\s+", str(sender or "").strip())[0] or str(sender or "")
        return _clean(f"Message from {short}") if short.strip() else "Message"
    return title[:1].upper() + title[1:]


def _cut(text: str, limit: int, *, strip_stop_words: bool = False) -> str:
    """``text`` cut on a word boundary to at most ``limit`` characters with one ``…`` appended when
    anything was dropped. A cut text, or any text with ``strip_stop_words``, also loses trailing stop
    words ("the", "your", ...)."""
    words, cut = text.split(), False
    if len(text) > limit:
        cut = True
        head = text[:limit - 1]
        boundary = head.rfind(" ")
        if text[limit - 1:limit] == " ":
            boundary = len(head)
        words = (head[:boundary] if boundary > 0 else head).split()
    # A cut title never ends on a stop word ("... for the…"); an uncut topic stays as typed.
    while (strip_stop_words or cut) and len(words) > 1 and words[-1].strip(_TRAIL_PUNCT).casefold() in _TITLE_STOP_WORDS:
        words, cut = words[:-1], True
    out = " ".join(words).rstrip(_TRAIL_PUNCT) if cut else " ".join(words)
    return f"{out}\u2026" if cut and out else out


def _clean(text: str) -> str:
    text = _DOUBLE_QUOTES_RE.sub("", str(text or ""))
    text = _WRAPPING_SINGLE_QUOTE_RE.sub("", text)
    return " ".join(text.split()).lstrip("-").strip()


def _key_norm(topic: str) -> str:
    """The topic part of a conversation key. Frozen to the original normalization (every quote mark
    dropped, first ``TITLE_MAX_CHARS``) so keys stamped before the title rules changed still match."""
    text = " ".join(_LEGACY_QUOTES_RE.sub("", str(topic or "")).split()).lstrip("-").strip()
    return text[:TITLE_MAX_CHARS].rstrip().casefold()


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
    ``#N`` continuation, then the compression tip) and stamps that row through the target's own
    store (``sessions.mark_side_window``). It never writes a window row: a new window is minted by
    the gateway through the transport's ``--create-if-missing`` and stamped right after that turn
    (Tim, 4 Nov 2026), and a target without a store has no window yet. ``key`` records the
    conversation (``side_window_key``) so its next DM finds this tab. Runs inside the window's turn
    lock. Returns the stamped id; a failure is logged and answers None, because a missing tab must
    never cost the DM."""
    store = None
    try:
        from persistence_assembly import open_persistence, profile_scope, store_exists

        scope = profile_scope(home=Path(profile_home))
        if not store_exists(scope):
            return None
        store = open_persistence(scope, eager=True)
        session_id = _resolve_window(store, title)
        if session_id and _is_stale_premint(store, session_id):
            # An empty CLI row carrying only our stamp is a pre-mint left by an older runner (not
            # gateway-owned, so resuming it fails with permission_denied). Drop it so the transport's
            # --create-if-missing mints a gateway row (Tim, 4 Nov 2026).
            try:
                _drop_premint(store, Path(profile_home), session_id)
            except Exception:
                logger.warning("message_agent: could not drop pre-minted window %s", session_id, exc_info=True)
            return None
        if session_id and store.sessions.mark_side_window(
                session_id, by=str((author or {}).get("id") or ""), key=key):
            return session_id
        return None
    except Exception:
        logger.warning("message_agent: could not stamp side window %r in %s", title, profile_home, exc_info=True)
        return None
    finally:
        if store is not None:
            store.close()


def is_conversation_window(profile_home: Path | str, title: str, key: str | None) -> bool:
    """Whether ``title`` already names this conversation's side window: the chat the transport resolves
    carries the side-window stamp with ``key`` (any stamp when the route has no key, the ``auto``
    route). A titled chat with no stamp, or another conversation's, is an unrelated title collision.
    Read before the turn's own restamp; never raises."""
    try:
        from persistence.conversation.sessions import record_side_window_stamp
        from persistence_assembly import open_persistence_reader

        home = Path(profile_home)
        if not (home / "state.db").is_file():
            return False
        with open_persistence_reader(home) as store:
            session_id = _resolve_window(store, title)
            stamp = record_side_window_stamp(store.sessions.get(session_id)) if session_id else None
        return stamp is not None and (not key or stamp.get("key") in (None, key))
    except Exception:
        logger.debug("message_agent: could not read side window %r in %s", title, profile_home, exc_info=True)
        return False


def _resolve_window(store, title: str) -> str | None:
    """``hermes_cli.main._resolve_session_by_name_or_id`` against the target's own store."""
    record = store.sessions.get(title)
    session_id = record.session_id if record else store.sessions.resolve_by_title(title)
    if not session_id and title.startswith("local-"):
        session_id = store.sessions.resolve_id(title)
    return (store.lineage.tip(session_id) or session_id) if session_id else None


def _is_stale_premint(store, session_id: str) -> bool:
    """An empty ``cli`` row whose origin is empty or only our own side-window stamp: what the old
    runner minted directly. A gateway routing origin (telegram, local, ...) is never touched."""
    from persistence.conversation.messages import TranscriptRead, TranscriptView
    from persistence.conversation.sessions import origin_is_side_window_only

    record = store.sessions.get(session_id)
    if record is None or record.source != "cli" or record.message_count:
        return False
    if store.messages.read_transcript(session_id, TranscriptRead(view=TranscriptView.ALL, limit=1)):
        return False
    return not record.origin_json or origin_is_side_window_only(record.origin_json)


def _drop_premint(store, profile_home: Path, session_id: str) -> None:
    """Delete a stale pre-minted window row, then run the delete's post-commit work
    (``session_retention``) for the target profile."""
    from persistence import SessionId

    store.sessions.set_title(SessionId(session_id), "")  # empty clears it; the delete needs an untitled row
    removed = store.sessions.delete_if_empty(SessionId(session_id), media_root=_media_root(profile_home))
    if removed:
        from session_retention import after_sessions_removed

        after_sessions_removed(store, store.scope.profile_dir, removed, reason="delete", primary=session_id,
                               sessions_dir=profile_home / "sessions")


def _media_root(profile_home: Path) -> str:
    """The target profile's retained native-input root, as that profile's own processes resolve it."""
    from gateway.session_ingress_media import native_inputs_root
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(profile_home)
    try:
        return str(native_inputs_root())
    finally:
        reset_hermes_home_override(token)


def side_window_lock_name(profile: str, title: str) -> str:
    """Turn-lock key for one (profile, title) window: same title serializes, others run in
    parallel, and none waits behind the profile's own Bot Chat lock (keyed by the bare name)."""
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
    return f"{profile[:40]}-sw-{digest}"
