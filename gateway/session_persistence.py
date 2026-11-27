"""SessionStore storage plumbing: per-profile storage interface handle resolution and the routing-index
load/save paths (state.db gateway_routing primary, sessions.json legacy mirror). Mixin split out of
``gateway/session.py``; bound onto ``SessionStore`` via the MRO."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import logging
import json
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional
from uuid import uuid4
from persistence.core.errors import DeclarativeDrift, PersistenceError, StoreContention
from utils import atomic_json_write

if TYPE_CHECKING:
    from gateway.session import SessionEntry, SessionSource

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.session")

# "Nothing pinned" sentinel: lets the resolvers tell "resolve from the active scope" from a deliberate
# ``store._db = x`` (a store handle the readers use as is, or None for the JSONL fallback).
_DB_UNPINNED = object()

# Self-documenting sentinel written first into sessions.json; "_" keys are skipped on load.
_SESSIONS_JSON_README = (
    "LEGACY MIRROR of the gateway routing index (the primary copy lives in the gateway_routing "
    "table in ~/.hermes/state.db). Maps messaging session keys (agent:main:<platform>:...) to "
    "active session IDs. This is NOT the session list. ALL sessions (CLI, TUI, and gateway) live "
    "in ~/.hermes/state.db and are shown by `hermes sessions list` and `/sessions`. Disable this "
    "file with `gateway.write_sessions_json: false` in config.yaml."
)


def authority_persistence(authority):
    """*authority*'s storage interface handle, the one it was built with."""
    return authority.persistence


def owner_rule():
    """The host data the store's owner rule reads (``OwnerRule``): this machine's owner subject, the dashboard
    sign-in providers whose stamps name that owner (a plugin-registered provider counts too), and every shipped
    messaging platform's label."""
    from gateway.owner_identity import _BUNDLED_LOGINS, host_owner_subject
    from hermes_cli.dashboard_auth.registry import list_session_providers
    from persistence.authority.vocabulary import OwnerRule
    return OwnerRule(host_subject=host_owner_subject(),
                     login_providers=_BUNDLED_LOGINS | {provider.name for provider in list_session_providers()},
                     messaging_labels=_messaging_labels())


@functools.lru_cache(maxsize=1)
def _messaging_labels():
    """Every platform Hermes ships: the enum and the bundled platform plugins. A runtime-registered catalog plugin
    (hermes-mobile's ``mobile``) is not in it: its rows are routed, which excludes them."""
    from gateway.config import Platform
    bundled, aliases = Platform._scan_bundled_plugin_platforms()
    return frozenset({platform.value for platform in Platform} | set(bundled) | set(aliases))


def _is_live_system_guard(exc: BaseException) -> bool:
    """Test-isolation guard: must stay a loud failure and is never cached."""
    return isinstance(exc, RuntimeError) and "live-system guard" in str(exc)


def _fails_only_on_platform(data: dict) -> bool:
    """*data* names a platform ``Platform()`` cannot resolve yet and parses once that name is a known one."""
    from gateway.config import Platform
    from gateway.session import SessionEntry
    origin = data.get("origin")
    name = origin.get("platform") if isinstance(origin, dict) else None
    try:
        Platform(name)
        return False  # resolvable (or missing): the row is broken some other way
    except (ValueError, TypeError):
        pass
    try:
        SessionEntry.from_dict(_with_platform(data, "local"))
    except (ValueError, KeyError, TypeError):
        return False
    return True


def _with_platform(data: dict, name: str) -> dict:
    """*data* (a routing row) with its platform fields set to *name*."""
    return {**data, "platform": name, "origin": {**data["origin"], "platform": name}}


def _held_row_entry(data: dict):
    """A held row as a ``SessionEntry`` under a stand-in platform, for platform-independent checks."""
    from gateway.session import SessionEntry
    return SessionEntry.from_dict(_with_platform(data, "local"))


def _apply_to_held_row(data: dict, mutate) -> Optional[dict]:
    """Run a platform-independent whole-index verdict (turn-marker recovery or discard) on a held row now,
    through a stand-in platform, so it reaches the row on disk even if the process exits before the
    plugin registers. The new row, or None when *mutate* left it unchanged."""
    entry = _held_row_entry(data)
    if not mutate(entry):
        return None
    return _with_platform({**data, **entry.to_dict()}, data["origin"]["platform"])


@dataclass(frozen=True)
class _Mirror:
    digest: str
    owner: str | None
    entries: dict
    pending: dict
    allow_missing: bool


class _RoutingJournalError(RuntimeError):
    """A durable fallback cannot safely be replayed or replaced."""


def _read_mirror(directory, scope):
    try:
        raw = (directory / 'sessions.json').read_bytes()
    except FileNotFoundError:
        return None
    data = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('expected a routing mirror object')
    journal = data.get('_routing_journal', {})
    if not isinstance(journal, dict):
        raise _RoutingJournalError('invalid routing journal')
    pending = journal.get('edits', {}) if journal.get('scope') == scope else {}
    if not isinstance(pending, dict) or not all(not key.startswith('_') and isinstance(pair, list) and len(pair) == 2
            and all(value is None or isinstance(value, str) for value in pair) for key, pair in pending.items()):
        raise _RoutingJournalError('invalid pending routing edits')
    return _Mirror(hashlib.sha256(raw).hexdigest(), data.get('_routing_db'),
                   {key: value for key, value in data.items() if not key.startswith('_') and isinstance(value, dict)},
                   pending, not journal.get('primary') or bool(data.get('_routing_db')))


def _mirror_projection(rows, backup):
    entries = {}
    for key, text in rows.items():
        try:
            entry = json.loads(text)
        except (ValueError, TypeError):
            entry = backup.get(key)
        if isinstance(entry, dict):
            entries[key] = entry
    return entries


class SessionPersistenceMixin:
    """SessionStore storage plumbing: storage interface handle resolution and routing-index load/save."""

    def retire_runtime_sessions(self, session_ids):
        """Publish committed retirement; durable fences reject delayed saves."""
        with self._lock:
            targets = set(session_ids)
            self._drop_routes_locked(self._route_keys_for_sessions_locked(targets))
            for sid in targets:
                self._session_owner_hints.pop(sid, None)

    def _route_keys_for_sessions_locked(self, session_ids) -> list[str]:
        """Every key routing to one of *session_ids*, live or held (platform not registered yet), once
        each. Lock held."""
        held = getattr(self, "_unparsed_routes", None) or {}
        live = [key for key, entry in self._entries.items() if entry.session_id in session_ids]
        return list(dict.fromkeys(live + [key for key, data in held.items() if data["session_id"] in session_ids]))

    def _drop_routes_locked(self, keys) -> None:
        """Drop *keys* from the live index AND the held rows: a held copy left behind is written back by
        the next full save, so the route outlives its removal. Lock held."""
        held = getattr(self, "_unparsed_routes", None) or {}
        for key in keys:
            self._entries.pop(key, None)
            held.pop(key, None)

    def fence_retired_routes(self, session_ids, *, namespace, expiring=()) -> None:
        """Durably retire *session_ids* in the routing index's own database (fences + route
        drop in one transaction). A delete committed in another profile's store fences only that
        store; the shared index lives in the launch home, so its saves need the fence here too,
        keyed by the deleting profile's routing *namespace* (``retire_gateway_routes``).
        Raises when the routing store cannot take the fence: the delete must not report success."""
        store = self._routing_persistence
        if store is None:
            raise RuntimeError("routing store unavailable for deletion fence")
        store.routing.fence_retired_routes(session_ids, namespace=namespace, expiring=expiring)

    def strip_retired_mirror_routes(self, session_ids=None) -> None:
        """Prevent an existing mirror from replaying retired routes after their fences expire.
        With writes disabled, consume a legacy import in SQLite without modifying the backup."""
        store = self._routing_persistence
        sessions_file = self.sessions_dir / "sessions.json"
        if store is None or not sessions_file.exists():
            return
        targets = None if session_ids is None else set(session_ids)
        with self._lazy("_save_lock", threading.Lock):  # never interleaves with a full save's write
            if not getattr(self, '_write_sessions_json', True):
                mirror = _read_mirror(self.sessions_dir, self._routing_scope())
                if mirror:
                    self._reconcile_mirror(store.routing, {}, mirror)
                return
            with open(sessions_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            if '_routing_journal' in data:
                mirror = _read_mirror(self.sessions_dir, self._routing_scope())
                rows = store.routing.load_routing_entries(routing_scope=self._routing_scope(), fresh=True)
                self._reconcile_mirror(store.routing, rows, mirror)
                _, owner = store.routing.save_routing_edits({}, set(), {}, routing_scope=self._routing_scope(), mirror=True)
                data = {'_routing_db': owner, **_mirror_projection(rows, mirror.entries)}
                self._pending_routing_edits = {}
                self._save_sessions_json(data)
                return
            kept = {}
            for key, entry in data.items():
                sid = entry.get("session_id") if isinstance(entry, dict) else None
                if (key.startswith("_") or not isinstance(sid, str) or (targets is not None and sid not in targets)
                        or not store.routing.route_retired(key, json.dumps({"session_id": sid}))):
                    kept[key] = entry
            if len(kept) < len(data):
                atomic_json_write(sessions_file, kept, mode=0o600)

    def _open_persistence_for_active_scope(self, home: Optional[Path] = None):
        """Storage interface handle for the active profile scope's store. ``home`` pins the profile;
        otherwise the scope follows the context-local HERMES_HOME (resolved per call so multiplexed
        profiles reach their own store). Handles are cached per profile folder; failed opens enter
        a bounded backoff during which callers get None and keep using the JSONL fallback.

        Resolving here rather than once in ``__init__`` is the whole fix for #88532: it lets the scoping
        that the multiplexed inbound path already performs actually reach session storage.
        """
        from hermes_constants import get_hermes_home
        from persistence_assembly import open_persistence, profile_scope

        scope = profile_scope(home=Path(home) if home is not None else Path(get_hermes_home()))

        def _open():
            try:
                return open_persistence(scope, eager=True)
            except Exception as e:
                if not _is_live_system_guard(e):
                    print(f"[gateway] Warning: SQLite session store unavailable, falling back to JSONL: {e}")
                raise

        return self._db_handle_cache.get(scope.profile_dir, _open, non_cacheable=_is_live_system_guard)

    def _pinned_db(self):
        """Return the explicitly pinned DB (``store._db = x``), else ``_DB_UNPINNED``."""
        return getattr(self, "_db_pinned", _DB_UNPINNED)

    @property
    def _db(self):
        """The store pinned with ``store._db = x``, else None. A pin holds for every later read (tests install
        one, or disable storage with ``store._db = None``): the readers use a pinned store handle as is, and any
        other pin gives them no handle; unpinned it is None, and readers resolve the scope's handle per call so a
        multiplexed profile's writes reach its own store."""
        pinned = self._pinned_db()
        return None if pinned is _DB_UNPINNED else pinned

    @_db.setter
    def _db(self, value) -> None:
        self._db_pinned = value

    def _scoped_persistence(self):
        """The storage interface handle for the active scope: a pinned store's own, else the cached handle on
        the scope's ``state.db`` (None while its open fails)."""
        pinned = self._pinned_db()
        if pinned is not _DB_UNPINNED:
            return self._persistence_beside(pinned)
        return self._open_persistence_for_active_scope()

    @property
    def _routing_persistence(self):
        """The storage interface handle on the one store that owns the routing index, whatever scope is
        active. ``_entries`` is one flat dict holding every profile's keys, so it must persist to ONE file
        (``_routing_home``), not whichever profile is scoped — otherwise a mid-turn rewrite and the unscoped
        startup load see different copies and crash markers under a secondary profile go unrecovered. A
        pinned store still wins; bare test instances lacking the handle cache report none.

        Resolving it from the active scope made that file whichever profile happened to be scoped at the
        time: a whole-index rewrite during one profile's turn copied every other profile's routing rows into
        that profile's store, and startup — which runs unscoped — then loaded a different copy than the one
        the last writer produced. See #66887.
        """
        pinned = self._pinned_db()
        if pinned is not _DB_UNPINNED:
            return self._persistence_beside(pinned)
        home = getattr(self, "_routing_home", None)
        try:
            if home is None:
                return self._open_persistence_for_active_scope()
            return self._open_persistence_for_active_scope(home=home)
        except (AttributeError, RuntimeError):  # a bare instance without the cache, or the live-system guard
            return None

    def _named_profile_for_key(self, session_key: Optional[str]) -> Optional[str]:
        """The non-default profile that owns *session_key*, or None (ambient store is authoritative:
        multiplexing off, or legacy ``agent:main``). Deliberately does NOT cover "that profile has
        no directory" — ownership and resolvability are separate questions for ``_persistence_for_key``."""
        if not getattr(self.config, "multiplex_profiles", False):
            return None
        profile = self._profile_from_session_key(session_key)
        return None if not profile or profile == "default" else profile

    def _profile_home_for_key(self, session_key: Optional[str]) -> Optional[Path]:
        """HERMES_HOME of the profile owning *session_key*, or None (no named owner or
        unresolvable)."""
        profile = self._named_profile_for_key(session_key)
        if profile is None:
            return None
        cache = self._profile_home_cache
        if profile in cache:
            return cache[profile]
        home: Optional[Path] = None
        try:
            from hermes_cli.profiles import get_profile_dir, profile_exists
            if profile_exists(profile):
                home = Path(get_profile_dir(profile))
        except Exception as exc:
            logger.debug("Could not resolve profile home for %r: %s", session_key, exc)
            home = None
        # Only hits are memoized: a profile directory can be provisioned *after* startup (enrollment
        # bridge), and a cached miss would pin that profile's rows to the ambient store for life.
        if home is not None:
            cache[profile] = home
        return home

    def _persistence_for_key(self, session_key: Optional[str]):
        """The storage interface handle on the store holding *session_key*'s rows, whatever scope is
        active (the owning profile is encoded in the key). The active scope follows the ambient
        HERMES_HOME that only the inbound message path installs; unscoped background work (expiry
        watcher) would otherwise write profile rows into the ROOT store until the stale-route self-heal
        drops a live conversation.

        Background work runs unscoped while operating on every profile's keys out of the single process-wide
        ``_entries`` dict — ``_session_housekeeping_watcher`` is the clearest case — so it reads and writes the
        ROOT store for rows that actually live under ``profiles/<name>/state.db``. The two writers then
        drift apart on the same logical session until the routing index disagrees with the row and the
        #54878 self-heal drops a live conversation (#66887).
        """
        pinned = self._pinned_db()
        if pinned is not _DB_UNPINNED:
            return self._persistence_beside(pinned)
        profile = self._named_profile_for_key(session_key)
        if profile is None:
            # Default-profile (``agent:main``) rows belong to the launch home, not to whichever
            # profile's scope happens to be active: a scoped drain tick or cron mirror touching a
            # default chat used to write its rows into the secondary's store (#102157's picture).
            routing_home = getattr(self, "_routing_home", None)
            if routing_home is None or not getattr(self.config, "multiplex_profiles", False):
                return self._open_persistence_for_active_scope()
            try:
                return self._open_persistence_for_active_scope(home=routing_home)
            except (AttributeError, RuntimeError):
                return None
        home = self._profile_home_for_key(session_key)
        if home is None:
            # Falling back to the ambient store would split ONE session identity across two
            # physical stores — fail closed; callers already handle a missing store.
            logger.warning(
                "gateway.session: profile %r has no resolvable home (key %r); refusing to fall "
                "back to the ambient store", profile, session_key)
            return None
        try:
            return self._open_persistence_for_active_scope(home=home)
        except (AttributeError, RuntimeError):
            return None  # degrades to the JSONL fallback, as a failed open does

    def _owner_key_for_session_id(self, session_id: Optional[str]) -> Optional[str]:
        """The routing key that owns *session_id*, or None. The published index is authoritative;
        ``_session_owner_hints`` covers the window where ownership is proven but routing not yet
        published. Deliberately lock-free: several callers already hold ``_lock``."""
        if not session_id:
            return None
        try:
            for entry in list(self._entries.values()):
                if entry.session_id == session_id:
                    return entry.session_key
        except Exception:
            pass  # bare stores / foreign entry objects in suites
        return (getattr(self, "_session_owner_hints", None) or {}).get(session_id)

    def _persistence_for_session_id(self, session_id: Optional[str]):
        """The storage interface handle on the store holding *session_id*'s row (owner from the index or a
        pre-published hint; unknown ids fall back to the ambient store)."""
        if not session_id:
            return self._scoped_persistence()
        return self._persistence_for_key(self._owner_key_for_session_id(session_id))

    @property
    def persistence(self):
        """The storage interface handle for the active profile scope, one per profile, opened on first
        use and closed by ``close_all_db_handles``; None while ``_db`` is pinned or while the handle cannot open
        (the cache's backoff decides when the next read tries again)."""
        if self._pinned_db() is not _DB_UNPINNED:
            return None
        return self._open_persistence_for_active_scope()

    def _persistence_beside(self, db):
        """The storage interface handle of *db* (a pinned store): *db* itself when it is one, else None."""
        from persistence_assembly import Persistence
        return db if isinstance(db, Persistence) else None

    def close_all_db_handles(self) -> None:
        """Close every storage interface handle this store opened (one per profile). Closing only the active scope's
        would strand secondary profiles' stores with their WAL lock held ('database is locked' on restart). A pinned
        store is the pinner's."""

        def _close(handle) -> None:
            try:
                handle.close()
            except Exception as exc:
                logger.debug("Storage handle close error during handle sweep: %s", exc)

        self._db_handle_cache.close_all(_close)

    def _ensure_loaded(self) -> None:
        """Load sessions index from disk if not already loaded."""
        with self._lock:
            self._ensure_loaded_locked()

    def _entry_locked(self, session_key: str) -> Optional[SessionEntry]:
        """Load the index and return the entry for *session_key*. Lock held."""
        self._ensure_loaded_locked()
        return self._entries.get(session_key)

    def _routing_scope(self) -> str:
        """Namespace for this store's gateway_routing rows: the resolved sessions_dir, so stores
        with different dirs never share entries."""
        try:
            return str(Path(self.sessions_dir).resolve())
        except Exception:
            return str(self.sessions_dir)

    @staticmethod
    def _chat_labels_meta_key(platform, scope_id, chat_id) -> str:
        return f"gateway_chat_labels:{getattr(platform, 'value', platform)}:{scope_id or ''}:{chat_id}"

    def record_chat_labels(self, source: SessionSource) -> bool:
        """Persist *source*'s ``chat_name`` / ``chat_topic`` as the chat's current labels, at the
        moment they are observed; True once written. A session origin cannot answer "what is this
        chat called now": it is written when its session is created, which can be after a newer
        observation (a delayed first turn), and a reset inherits it under a fresh ``created_at``.

        False when there is no database handle right now (open failed, backoff, JSONL fallback):
        nothing was written, so the caller must not treat these labels as recorded."""
        store = self._routing_persistence
        if store is None:
            return False
        store.state_meta.set(self._chat_labels_meta_key(source.platform, source.scope_id, source.chat_id),
                             json.dumps([source.chat_name, source.chat_topic]))
        return True

    def chat_labels(self, platform, scope_id, chat_id) -> Optional[tuple]:
        """The ``(chat_name, chat_topic)`` last recorded for a chat, or None when none was. Raises
        when the store cannot be read, including when there is no database handle right now:
        "unreadable now" is not "never labelled"."""
        store = self._routing_persistence
        if store is None:
            raise LookupError("session database unavailable")
        raw = store.state_meta.get(self._chat_labels_meta_key(platform, scope_id, chat_id))
        if not raw:
            return None
        chat_name, chat_topic = json.loads(raw)
        return chat_name, chat_topic

    def _install_routing_rows(self, rows):
        self._entries.clear()
        self._unparsed_routes = {}
        for key, text in rows.items():
            entry = self._routing_entry_from_json(key, text)
            if entry is not None:
                self._entries[key] = entry
            else:
                self._keep_unparsed_route(key, text)

    def _keep_unparsed_route(self, key: str, entry_json: str) -> None:
        """Hold a row that fails to parse ONLY because its platform's plugin is not registered (installed
        after boot, a secondary profile's, not installed yet), so the next full save writes it back instead
        of deleting it and a later start that has the plugin loads it. A row broken any other way is not
        held: it stays skipped and a valid sessions.json copy can still replace it. Lock held."""
        with contextlib.suppress(ValueError):
            data = json.loads(entry_json)
            if isinstance(data, dict) and data.get("session_id") and _fails_only_on_platform(data):
                self._lazy("_unparsed_routes", dict)[key] = data

    def _promote_held_routes_locked(self) -> None:
        """Move a held row into the live index once its platform resolves (a multiplexed profile's plugin
        registers after the index loaded), so its own state (/stop's ``suspended``, ``resume_pending``) applies
        on the next lookup instead of a recovered entry replacing it; drop a held copy whose key the live
        index already owns, so a later prune or delete of that entry cannot restore it. A promoted row already
        carries every whole-index verdict given while it was held (``_update_all_entries_locked``). Lock held."""
        from gateway.config import Platform
        held = getattr(self, "_unparsed_routes", None) or {}
        for key, data in list(held.items()):
            if key not in self._entries:
                if data["origin"].get("platform") not in Platform._value2member_map_:
                    continue  # still unregistered: no registry lookup per call
                entry = self._routing_entry_from_json(key, json.dumps(data))
                if entry is None:
                    continue
                self._entries[key] = entry
            del held[key]

    @staticmethod
    def _routing_entry_from_json(key: str, entry_json: str) -> Optional[SessionEntry]:
        """Parse one gateway_routing row; None (with a warning) when invalid."""
        from gateway.session import SessionEntry
        try:
            entry_data = json.loads(entry_json)
            if isinstance(entry_data, dict):
                return SessionEntry.from_dict(entry_data)
        except (ValueError, KeyError, TypeError) as e:
            logger.warning("Skipping invalid routing entry %r: %s", key, e)
        return None

    def _ensure_loaded_locked(self) -> None:
        """Load primary routes and transactionally consume any legacy import or fallback edits."""
        if self._loaded:
            self._reconcile_recovered_routing_locked()
            self._promote_held_routes_locked()
            return
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        live = dict(self._entries)
        mirror = self._startup_mirror()
        with self._lazy('_save_lock', threading.Lock):
            if mirror is not None:
                self._install_routing_rows({key: json.dumps(value) for key, value in mirror.entries.items()})
            if not self._load_primary(mirror):
                if mirror is not None:
                    self._pending_routing_edits = mirror.pending
                    self._mirror_owner = mirror.owner
                self._heal_missing_origins(self._entries.values())
                self._routing_baseline = self._routing_data_locked()
                self._routing_db_loaded = False
            for key, entry in live.items():
                if key not in getattr(self, '_unparsed_routes', {}):
                    self._entries.setdefault(key, entry)
        self._loaded = True
        # A hard crash skips graceful shutdown and leaves sessions.json pointing at ended sessions.
        self._prune_stale_sessions_locked()

    def _heal_missing_origins(self, entries) -> None:
        for entry in entries:
            if entry.origin is None:
                entry.origin = self._stored_origin(entry)

    def _stored_origin(self, entry: SessionEntry) -> Optional[SessionSource]:
        """*entry*'s messaging source rebuilt from its conversation row, if it still routes to the
        entry's key. A routing entry persisted without an origin (#146696) otherwise leaves restart
        resume, route recovery and wakes nothing to route by until the chat speaks again. Local and
        API routes are left alone: their receipts, not the row, name their owner."""
        import sqlite3

        from gateway.session_route_recovery import _messaging_platform, _row_source
        from persistence.core.errors import STORE_FAILURES
        store = self._persistence_for_key(entry.session_key)
        try:
            row = store.sessions.get(entry.session_id) if store is not None else None
        except (sqlite3.Error, *STORE_FAILURES):
            return None
        platform = row and _messaging_platform(entry.platform.value if entry.platform else row.source)
        source = _row_source(row, platform, self._named_profile_for_key(entry.session_key)) if platform else None
        if source is None or source.platform != platform:
            return None
        return source if self._generate_session_key(source) == entry.session_key else None

    def _startup_mirror(self):
        try:
            return _read_mirror(self.sessions_dir, self._routing_scope())
        except (ValueError, OSError) as exc:
            if isinstance(exc, OSError) and getattr(self, '_write_sessions_json', True):
                raise
            logger.warning('gateway.session: cannot read sessions.json: %s', exc)
            return None

    def _reconcile_mirror(self, routing, rows, mirror):
        missing = {}
        for key in mirror.entries.keys() - rows.keys() if mirror.allow_missing else ():
            data = mirror.entries[key]
            text = json.dumps(data)
            if self._routing_entry_from_json(key, text) is not None or _fails_only_on_platform(data):
                missing[key] = text
        resolved = routing.reconcile_routing_mirror(missing, mirror.pending, digest=mirror.digest,
                                                    owner=mirror.owner, routing_scope=self._routing_scope())
        for key, text in resolved.items():
            if text is None:
                rows.pop(key, None)
            else:
                rows[key] = text
        return any(value is None for value in resolved.values())

    def _load_primary(self, mirror):
        store = self._routing_persistence
        if store is None:
            return False
        try:
            rows = store.routing.load_routing_entries(routing_scope=self._routing_scope(), fresh=True)
        except (StoreContention, DeclarativeDrift):
            raise
        except (OSError, PersistenceError) as exc:
            logger.warning('gateway.session: state.db routing load failed: %s', exc)
            return False
        try:
            strip = mirror is not None and self._reconcile_mirror(store.routing, rows, mirror)
        except (StoreContention, DeclarativeDrift):
            raise
        except (OSError, PersistenceError) as exc:
            if not self._loaded:
                self._install_routing_rows({**{key: json.dumps(value) for key, value in mirror.entries.items()}, **rows})
            logger.warning('gateway.session: routing mirror reconciliation failed: %s', exc)
            return False
        self._install_routing_rows(rows)
        self._routing_baseline = self._routing_data_locked()
        self._routing_original = {key: text for key, text in rows.items() if key in self._routing_baseline and
                                  json.loads(text) != self._routing_baseline[key]}
        if mirror:
            self._mirror_owner = mirror.owner
            for key in (rows.keys() - self._routing_baseline.keys()) & mirror.entries.keys():
                text = json.dumps(mirror.entries[key])
                entry = self._routing_entry_from_json(key, text)
                if entry and not store.routing.route_retired(key, text):
                    self._entries[key] = entry
                    self._lazy('_routing_repairs', dict)[key] = rows[key]
        self._routing_db_loaded = True
        self._pending_routing_edits = {}
        self._heal_missing_origins(self._entries.values())
        if strip and self._write_sessions_json:
            try:
                _, owner = store.routing.save_routing_edits({}, set(), {}, routing_scope=self._routing_scope(), mirror=True)
                self._mirror_owner = owner
                self._save_sessions_json({**self._routing_data_locked(), '_routing_db': owner})
            except OSError as exc:
                logger.warning('gateway.session: could not refresh consumed mirror: %s', exc)
        return True

    def _prune_stale_sessions_locked(self) -> None:
        """Remove routing entries whose session has ended in state.db (startup, lock held). Stale ==
        ``end_reason IS NOT NULL``; rows absent from the DB are kept; a ``None`` DB handle is a
        no-op; DB errors are non-fatal."""
        if not self._entries:
            return
        stale_keys: list = []
        recovered_keys = 0
        try:
            for key, entry in self._entries.items():
                # Ask the store that owns the key, not the ambient handle, or a live
                # secondary-profile session gets pruned on the root copy.
                store = self._persistence_for_key(key)
                if store is None:
                    continue
                row = store.sessions.get(entry.session_id)
                if row is None or row.end_reason is None:
                    continue
                verdict = self._stale_entry_verdict(key, entry, row)
                if verdict == "prune":
                    stale_keys.append(key)
                elif verdict is not None:
                    self._entries[key] = verdict
                    recovered_keys += 1
        except Exception as exc:
            logger.warning("gateway.session: stale-entry pruning skipped due to DB error: %s", exc)
            return
        self._drop_routes_locked(stale_keys)
        if stale_keys or recovered_keys:
            self._save()

    def _stale_entry_verdict(self, key: str, entry, row):
        """For a routing entry whose row has ended: ``"prune"``, a replacement entry (repoint), or
        None (keep as-is)."""
        from gateway.session_lifecycle import _now
        local_target = self._local_receipt_target(entry)
        if local_target is not None:
            # The authority's receipt names the route's target (a compression child after a
            # split); its first admitted turn reopens a stamped row. A peer-row recovery here
            # adopted an empty, never-admitted replacement instead (#140642).
            if local_target == entry.session_id:
                return None
            import copy
            repointed = copy.copy(entry)
            repointed.session_id = local_target
            logger.warning(
                "gateway.session: repointing stale sessions.json entry %r from ended %s "
                "(end_reason=%r) to its local receipt target %s", key, entry.session_id,
                row.end_reason, local_target)
            return repointed
        recovered_entry = None
        if entry.origin is not None:
            try:
                recovered_entry = self._recover_session_from_db(
                    session_key=key, source=entry.origin, now=_now(), raise_on_lookup_error=True)
            except Exception as exc:
                # Indeterminate: keep the only routing handle.
                logger.debug(
                    "gateway.session: recovery lookup failed for stale sessions.json entry %r -> "
                    "%s: %s", key, entry.session_id, exc)
                return None
        # Compression-ended parent with a newer live child for the same peer: repoint instead of
        # dropping, or queued/resume-pending work vanishes until the next message.
        if recovered_entry is not None and recovered_entry.session_id != entry.session_id:
            logger.warning(
                "gateway.session: repointing stale sessions.json entry %r from ended %s "
                "(end_reason=%r) to recovered %s", key, entry.session_id, row.end_reason,
                recovered_entry.session_id)
            if entry.prompt_pin and (
                self._compression_tip_for_session_id(entry.session_id) == recovered_entry.session_id
            ):
                # The compression child continues this conversation: its internal turns keep the
                # pinned system bytes (the rebuilt entry is minimal and would drop them).
                recovered_entry.prompt_pin = entry.prompt_pin
            return recovered_entry
        # Same-id recovery == successful resume: keep the ORIGINAL entry object (the recovered one
        # is rebuilt minimal and would drop counters, model_override, resume markers, metadata).
        # A non-None recovery with the SAME session id is a successful resume (all recovery gates passed,
        # row reopened): keep the routing entry — it is proven valid, not a dead route (#95957). Nothing in
        # sessions.json changes, so no save is needed for this branch.
        if recovered_entry is not None:
            logger.info(
                "gateway.session: reopened ended session %s for sessions.json entry %r "
                "(end_reason=%r); keeping route", entry.session_id, key, row.end_reason)
            return None
        logger.warning(
            "gateway.session: pruning stale sessions.json entry %r -> %s (end_reason=%r); left by "
            "a crashed gateway", key, entry.session_id, row.end_reason)
        return "prune"

    def _entries_as_dicts(self) -> dict[str, Any]:
        """Reuse unchanged wire rows. Even off the SQLite lock, re-encoding every history pin
        starves threads trying to get the GIL back to finish their commit. The comparison copy
        must not alias in-place edits to origin, policy or nested metadata. Session lock held."""
        previous = self._lazy('_entry_snapshots', dict)
        for key in previous.keys() - self._entries.keys():
            del previous[key]
        rows = {}
        for key, entry in self._entries.items():
            fields = vars(entry)
            cached = previous.get(key)
            metadata = json.dumps(entry.metadata) if entry.metadata else ''
            if cached is None or cached[0] != fields or cached[1] != metadata:
                cached = previous[key] = (deepcopy(fields), metadata, entry.to_dict())
            rows[key] = cached[2]
        return rows

    def _save(self) -> None:
        """Persist the routing index while the caller holds ``_lock``."""
        self._persist_routing_data(*self._snapshot_routing_locked())

    def _next_routing_generation_locked(self) -> int:
        """Bump and return the shared routing counter (lock held). Full snapshots AND single-entry
        fast saves MUST allocate from this one counter: the stale-write protection is a total order
        over serialization times and silently breaks otherwise."""
        self._routing_generation = getattr(self, "_routing_generation", 0) + 1
        if getattr(self, '_routing_db_loaded', None) is False:
            self._last_unreconciled_generation = self._routing_generation
        return self._routing_generation

    def _reconcile_recovered_routing_locked(self) -> None:
        """Merge authoritative rows after a fallback-only startup load."""
        if getattr(self, '_routing_db_loaded', None) is not False:
            return
        with self._lazy('_save_lock', threading.Lock):
            baseline, current = self._routing_baseline, self._routing_data_locked()
            changed = {key for key, value in current.items() if baseline.get(key) != value}
            live = {key: self._entries[key] for key in changed if key in self._entries}
            held = {key: current[key] for key in changed - live.keys()}
            if not self._load_primary(self._startup_mirror()):
                return
            self._drop_routes_locked((baseline.keys() - current.keys()) | changed)
            self._entries.update(live)
            self._unparsed_routes.update(held)
            self._recovered_fallback = (getattr(self, '_last_unreconciled_generation', 0), baseline)

    def _snapshot_routing_locked(self) -> tuple[dict[str, Any], int]:
        """Capture immutable routing data and a monotonic generation."""
        self._reconcile_recovered_routing_locked()
        return self._routing_data_locked(), self._next_routing_generation_locked()

    def _routing_data_locked(self) -> dict[str, Any]:
        """Every row a full write persists: held unparsed rows, then live entries (a live entry under the
        same key wins). Lock held."""
        return {**deepcopy(getattr(self, "_unparsed_routes", None) or {}), **self._entries_as_dicts()}

    def _persist_routing_data(self, data: dict[str, Any], generation: int) -> None:
        """Serialize all whole-index writers through one durable write lock."""
        with self._lazy("_save_lock", threading.Lock):
            if generation <= getattr(self, "_persisted_routing_generation", 0):
                return
            recovery = getattr(self, '_recovered_fallback', None)
            if recovery and generation <= recovery[0]:
                fallback = recovery[1]
                merged = dict(self._routing_baseline)
                for key in fallback.keys() - data.keys():
                    merged.pop(key, None)
                merged.update((key, value) for key, value in data.items() if fallback.get(key) != value)
                data = merged
            # Fold in fast upserts numbered above this snapshot: they were serialized after us and
            # a delayed full rewrite must not regress them.
            fast_persisted = getattr(self, "_fast_persisted_entries", None)
            if fast_persisted:
                for key, (revision, entry_json) in fast_persisted.items():
                    if revision > generation:
                        data[key] = json.loads(entry_json)
            baseline = getattr(self, '_routing_baseline', {})
            puts = {key: text for key, value in data.items() if value is not baseline.get(key)
                    and (text := json.dumps(value)) != json.dumps(baseline.get(key))}
            deletes = baseline.keys() - data.keys()
            self._save_generation(data, puts, deletes, baseline)
            self._routing_baseline = data
            self._persisted_routing_generation = generation
            if recovery and generation >= recovery[0]:
                self._recovered_fallback = None
            # This rewrite supersedes fast records at or below its generation; newer ones stay for
            # the next delayed full writer.
            if fast_persisted:
                for key in [k for k, (rev, _) in fast_persisted.items() if rev <= generation]:
                    del fast_persisted[key]

    def _save_generation(self, data, puts, deletes, baseline):
        write_mirror = getattr(self, '_write_sessions_json', True)
        pending = getattr(self, '_pending_routing_edits', {})
        scope = self._routing_scope()
        try:
            store = self._routing_persistence
            if store is None or getattr(self, '_routing_db_loaded', None) is False:
                raise OSError('state.db routing unavailable')
            if pending:
                mirror = _read_mirror(self.sessions_dir, scope)
                if mirror is None or mirror.pending != pending:
                    raise _RoutingJournalError('pending routing journal changed before replay')
                store.routing.reconcile_routing_mirror({}, pending, digest=mirror.digest, owner=mirror.owner, routing_scope=scope)
                for key in pending:
                    getattr(self, '_routing_original', {}).pop(key, None)
                self._pending_routing_edits = pending = {}
            originals = getattr(self, '_routing_original', {})
            prior = {key: originals.get(key) or json.dumps(baseline[key]) for key in puts if key in baseline}
            retired, owner = store.routing.save_routing_edits(puts, deletes,
                {key: value['session_id'] for key, value in data.items()}, routing_scope=scope, mirror=write_mirror,
                repairs=getattr(self, '_routing_repairs', {}), prior=prior)
        except (StoreContention, DeclarativeDrift, _RoutingJournalError):
            raise
        except (OSError, PersistenceError) as exc:
            if not write_mirror:
                raise
            logger.warning('gateway.session: state.db routing save failed; journaling gateway edits: %s', exc)
            journal = dict(pending)
            for key in puts.keys() | deletes:
                before = getattr(self, '_routing_original', {}).get(key) or getattr(self, '_routing_repairs', {}).get(key)
                if key in journal:
                    before = journal[key][0]
                elif before is None and key in baseline:
                    before = json.dumps(baseline[key])
                journal[key] = [before, puts.get(key)]
            self._save_sessions_json({**data, '_routing_db': getattr(self, '_mirror_owner', None),
                                      '_routing_journal': {'scope': scope, 'generation': uuid4().hex, 'edits': journal,
                                                           'primary': getattr(self, '_routing_db_loaded', None) is True}})
            self._pending_routing_edits = journal
            return
        self._pending_routing_edits = {}
        self._routing_repairs = {}
        originals = getattr(self, '_routing_original', {})
        for key in puts.keys() | deletes:
            originals.pop(key, None)
        if not write_mirror:
            return
        self._mirror_owner = owner
        try:
            self._save_sessions_json({**{key: value for key, value in data.items() if key not in retired}, '_routing_db': owner})
        except (OSError, TypeError, ValueError) as exc:
            logger.warning('gateway.session: sessions.json mirror save failed after state.db commit: %s', exc)

    def _save_sessions_json(self, data: dict[str, Any]) -> None:
        """Write the legacy sessions.json mirror of the routing index (atomic + fsync)."""
        atomic_json_write(self.sessions_dir / "sessions.json", {"_README": _SESSIONS_JSON_README, **data},
                          mode=0o600, fsync_dir='required' if '_routing_journal' in data else False)

    def _save_entries(self) -> None:
        """Snapshot latest state under ``_lock`` and persist after releasing it."""
        with self._lock:
            data, generation = self._snapshot_routing_locked()
        self._persist_routing_data(data, generation)

    def _save_entry(
        self, session_key: str, *, entry_data: Optional[dict[str, Any]] = None,
        lock_held: bool = False) -> None:
        """Persist ONE routing entry via UPSERT — the per-turn fast path (a full rewrite fsyncs a
        multi-MB sessions.json). The key -> session_id mapping never changes here: structural
        transitions use the full rewrite (which also refreshes the sessions.json mirror; it may lag
        in metadata only). The revision comes from the shared routing generation counter; under
        ``_save_lock`` the upsert is skipped if a full snapshot or a newer fast save of this key
        already persisted (the reverse case lives in ``_persist_routing_data``). No DB or a failed
        upsert falls back to the full rewrite. ``entry_data`` persists a candidate BEFORE it is
        published to the live entry (failure-atomic transitions); the fallback carries it too."""
        guard = contextlib.nullcontext() if lock_held else self._lock
        with guard:
            entry = self._entries.get(session_key)
            if entry is None:
                return
            serialized = dict(entry_data) if entry_data is not None else entry.to_dict()
            # The O(n) full snapshot is deferred to the fallback branch.
            entry_json, revision = json.dumps(serialized), self._next_routing_generation_locked()
        store = self._routing_persistence
        if store is not None:
            try:
                with self._lazy("_save_lock", threading.Lock):
                    if getattr(self, "_persisted_routing_generation", 0) >= revision:
                        return
                    fast_persisted = self._lazy("_fast_persisted_entries", dict)
                    persisted = fast_persisted.get(session_key)
                    if persisted is not None and persisted[0] >= revision:
                        return
                    if getattr(self, '_pending_routing_edits', {}) or getattr(self, '_routing_db_loaded', None) is False:
                        raise OSError('gateway routing reconciliation pending')
                    store.routing.upsert_routing_entry(session_key, entry_json, routing_scope=self._routing_scope())
                    fast_persisted[session_key] = (revision, entry_json)
                    self._lazy('_routing_baseline', dict)[session_key] = json.loads(entry_json)
                    getattr(self, '_routing_original', {}).pop(session_key, None)
                return
            except Exception as exc:
                logger.warning(
                    "gateway.session: single-entry routing save failed for %r (%s); falling back "
                    "to full index rewrite", session_key, exc)
        if entry_data is not None:
            # Full-snapshot fallback carrying the candidate transition.
            with guard:
                fallback_data = self._routing_data_locked()
            fallback_data[session_key] = dict(entry_data)
            self._persist_routing_data(fallback_data, revision)
        else:
            self._save_entries()
