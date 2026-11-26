"""Kanban assignee alias resolution (Bot-Mode aliases -> profile folder id)."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def resolve_assignee(assignee: Optional[str]) -> tuple[Optional[str], bool]:
    """Resolve an assignee name or Bot-Mode alias to ``(stored_value, is_installed_profile)``.

    Normalizes via ``normalize_profile_name`` first (preserving its ``ValueError``
    on non-string/blank input). Fast path: installed profile folder ids return
    immediately without reading alias files. Slow path delegates to the same
    ``tools.bot_mode_dm._resolve_local_name`` resolver ``message_agent`` uses so
    ``hermes``, display names (``Neo``), and slugs (``atlas-growth-lead``) map to
    their profile folder id. Unresolvable names return ``(canon, False)`` so
    CLI/dashboard terminal lanes remain legal.
    """
    if assignee is None:
        return None, False
    from hermes_cli.profiles import list_profile_names, normalize_profile_name

    canon = normalize_profile_name(assignee)
    try:
        installed = list_profile_names()
    except Exception:
        logger.debug("kanban: failed to list profiles while resolving %r", assignee, exc_info=True)
        return canon, False
    if canon in installed:
        return canon, True
    try:
        from hermes_constants import get_default_hermes_root
        from tools.bot_mode_dm import _resolve_local_name

        resolved = _resolve_local_name(assignee, installed, get_default_hermes_root())
    except Exception:
        logger.debug("kanban: alias lookup failed while resolving %r", assignee, exc_info=True)
        return canon, False
    if resolved is not None and resolved in installed:
        return resolved, True
    return canon, False


def assignee_sql_filter(assignee: Optional[str]) -> tuple[str, tuple[str, ...]]:
    """Return ``(sql_clause, params)`` matching the resolved id and any legacy alias value."""
    if assignee is None:
        return "", ()
    from hermes_cli.profiles import normalize_profile_name

    canon = normalize_profile_name(assignee)
    resolved, _ = resolve_assignee(assignee)
    if resolved and resolved != canon:
        return " AND assignee IN (?, ?)", (resolved, canon)
    return " AND assignee = ?", (resolved or canon,)
