"""effort-router delegation strategy (``delegation.backend: effort-router``).

Same route as ``default`` (the ``delegation`` section), plus a per-task ``effort``. The parent picks it per
task; when it does not, a cheap heuristic tiers the task: a short mechanical goal (fetch, list, read,
rename, look up ...) runs ``medium``, everything else ``delegation.reasoning_effort``. Work touching money,
legal, sends, code changes or research synthesis never runs below ``high``, whoever chose the level, and
``xhigh`` is never chosen automatically. The schema, guidance and heuristic are the same at every depth, so
an orchestrator child tiers its own sub-subtasks the same way. Each child's route label says what was
picked and why (``effort medium (auto: mechanical)``), so a swarm's result shows its effort mix.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Optional

from agent.delegation_provider import DelegationProvider

logger = logging.getLogger(__name__)

LEVELS = ("low", "medium", "high", "xhigh")
FLOOR = "high"
_AUTO = "medium"
_MAX_MECHANICAL_GOAL = 160

# Sensitive = money, legal, outbound sends, code changes, research synthesis: never below FLOOR.
_SENSITIVE = re.compile(
    r"\$\s?\d|\b(?:invoice\w*|pay(?:ment|out)s?\w*|wire[sd]?|refund\w*|bank\w*|pric(?:e|es|ing)|quote[sd]?|budget\w*"
    r"|revenue|usd|eur|brl|tax(?:es)?|cap ?table|safe|fund(?:ing|raise)?|investor\w*"
    r"|legal|contract\w*|nda|sow|terms|compliance|lawyer\w*|counsel|sign(?:ed|ing|ature)?|kyc|kyb"
    r"|send\w*|e-?mail\w*|repl(?:y|ies)|post(?:ed|ing)?|publish\w*|message\w*|dm|tweet\w*|slack|whatsapp|outreach"
    r"|code|commit\w*|patch\w*|refactor\w*|implement\w*|fix\w*|bug\w*|deploy\w*|migrat\w*|merge\w*|pr|pull request"
    r"|research\w*|synthes\w*|analy[sz]\w*|strateg\w*|recommend\w*|compare|comparison|evaluat\w*|review\w*|report)\b",
    re.IGNORECASE)
_MECHANICAL = re.compile(
    r"^\s*(?:fetch|get|look ?up|find|list|read|extract|download|count|check|grep|search|format|convert|rename"
    r"|move|copy|collect|scrape|open|print|show|query|resize|unzip|zip)\b", re.IGNORECASE)


def _rank(level: Any) -> int:
    return LEVELS.index(level) if level in LEVELS else -1


def choose_effort(goal: str, context: str, asked: Optional[str], default: Optional[str]) -> tuple[Optional[str], str]:
    """``(effort, why)`` for one task: the parent's pick, else the heuristic, then the sensitive-work floor."""
    sensitive = _SENSITIVE.search(f"{goal}\n{context}")
    if asked in LEVELS:
        effort, why = asked, ""
        if sensitive and _rank(asked) < _rank(FLOOR):
            effort, why = FLOOR, f"floor: asked {asked}"
    elif not sensitive and len(goal) <= _MAX_MECHANICAL_GOAL and _MECHANICAL.match(goal):
        effort, why = _AUTO, "auto: mechanical"
    elif sensitive and default in LEVELS and _rank(default) < _rank(FLOOR):
        effort, why = FLOOR, f"floor: default {default}"
    else:
        effort, why = default or None, "default"
    return effort, why


class EffortRouterProvider(DelegationProvider):
    @property
    def name(self) -> str:
        return "effort-router"

    @property
    def display_name(self) -> str:
        return "Effort router"

    @property
    def summary(self) -> str:
        return "Default route; each subagent's effort is tiered per task (medium for mechanical work, high floor)."

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        default = str((config or {}).get("reasoning_effort") or "the parent's level")
        return {"effort": {"type": "string", "enum": list(LEVELS), "description": (
            f"Reasoning effort for this subagent. low/medium for clearly easy mechanical work (lookups, "
            f"fetch-and-extract, formatting, file moves, a single API read); omit for {default}; xhigh only for "
            f"genuinely hard work (deep multi-source research, tricky debugging, architecture). Work touching "
            f"money, legal, sends, code changes or research synthesis runs never below high.")}}

    def description(self, config: Mapping[str, Any]) -> str:
        return ("Effort tiering: give each task an `effort`. Easy mechanical subtasks get low or medium; omitted = "
                "medium for short mechanical goals, else the default; money, legal, sends, code changes and research "
                "synthesis are never below high. Your own subagents follow the same rule.")

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        route = {k: v for k, v in config.items() if k != "service_tier"}
        default = config.get("reasoning_effort") if isinstance(config.get("reasoning_effort"), str) else None
        effort, why = choose_effort(str(task.get("goal") or ""), str(task.get("context") or ""),
                                    task.get("effort"), default)
        if effort:
            route["reasoning_effort"] = effort
        route["label"] = f"effort {effort or 'inherited'}" + (f" ({why})" if why else "")
        logger.info("effort-router: task %s -> %s", task.get("index"), route["label"])
        return route


def register(ctx) -> None:
    ctx.register_delegation_provider(EffortRouterProvider())
