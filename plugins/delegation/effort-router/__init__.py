"""effort-router delegation strategy (``delegation.backend: effort-router``).

Same route as ``default`` (the ``delegation`` section), plus a per-task ``effort``. The parent picks it per
task. Two rules keep a cheap level away from work that matters:

- **Floor.** Work whose goal or context touches money, legal, sends, code changes, destructive operations or
  research synthesis, or names any write/action verb (en/pt/fr/es), never runs below ``high``: an explicit
  low/medium pick is raised, and so is a default that is empty, disabled or below high. The route then
  carries ``high`` explicitly, so the child can never inherit a low parent level.
- **Auto.** When the parent omits ``effort``, the level drops to ``medium`` only for a positively identified
  mechanical goal: one read-only verb first (fetch, list, read, look up, grep ...), one operation, no second
  clause, plain English. Compound, ambiguous or other-language goals keep ``delegation.reasoning_effort``.

``xhigh`` is never chosen automatically. The schema, guidance and rules are the same at every depth, so an
orchestrator child tiers its own sub-subtasks the same way. Each child's route label says what was picked and
why (``effort medium (auto: mechanical)``, ``effort high (floor: asked low, money)``).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Mapping, Optional

from agent.delegation_provider import DelegationProvider

logger = logging.getLogger(__name__)

LEVELS = ("low", "medium", "high", "xhigh")
FLOOR = "high"
_AUTO = "medium"
_AT_OR_ABOVE_FLOOR = frozenset({"high", "xhigh", "max", "ultra"})
_AT_OR_BELOW_AUTO = frozenset({"minimal", "low", "medium", "none", "false", "disabled"})
_MAX_MECHANICAL_GOAL = 160


def _words(*alternatives: str) -> re.Pattern:
    """One case-insensitive whole-word pattern, matched on accent-folded text (``_fold``)."""
    return re.compile(r"(?<![\w-])(?:" + "|".join(alternatives) + r")(?![\w-])", re.IGNORECASE)


# Floor triggers on accent-folded goal + context, first match names the category. A false positive only raises
# a task to high (the safe direction), so the lists lean broad; ``\w*`` stems cover pt/fr/es conjugations.
_FLOOR = (
    ("money", re.compile(r"[$€£¥]\s?\d|\d\s?[$€£¥]|r\$")),
    ("money", _words(
        r"money", r"funds?", r"funding", r"fundrais\w*", r"transfer\w*", r"balances?", r"payments?", r"pay", r"pays",
        r"paid", r"paying", r"payouts?", r"payroll\w*", r"salar\w*", r"wages?", r"invoic\w*", r"bills?", r"billing",
        r"billed", r"refund\w*", r"wires?", r"wired", r"wiring", r"bank\w*", r"iban", r"swift", r"ach", r"treasur\w*",
        r"wallets?", r"crypto\w*", r"usdc", r"usdt", r"btc", r"eth", r"pric(?:e|es|ed|ing)", r"quotes?", r"quoted",
        r"budgets?", r"revenues?", r"costs?", r"expens\w*", r"tax\w*", r"equity", r"cap ?tables?", r"safes?",
        r"investors?", r"loans?", r"credits?", r"debits?", r"cards?", r"charg(?:e|es|ed|ing)", r"purchas\w*",
        r"usd", r"eur", r"gbp", r"brl", r"reais", r"dollars?", r"euros?", r"pounds?", r"cents?",
        r"dinheiro", r"transfir\w*", r"transferenc\w*", r"pagament\w*", r"pag(?:ar|ue|uem|a|am|ou|o)", r"faturas?",
        r"boletos?", r"pix", r"saldos?", r"contas?", r"impostos?", r"reembols\w*", r"cobranc\w*", r"precos?",
        r"orcament\w*", r"investidor\w*", r"emprestimos?", r"argent", r"virements?", r"vir(?:er|ez|e)", r"paiements?",
        r"pa(?:yer|yez|ie|ient)", r"factur\w*", r"soldes?", r"comptes?", r"impots?", r"rembours\w*", r"prix", r"devis",
        r"investisseurs?", r"prets?", r"dinero", r"transfier\w*", r"pagos?", r"cuentas?", r"impuestos?", r"precios?",
        r"presupuestos?", r"inversor\w*", r"prestamos?", r"dolares", r"libras?")),
    ("legal", _words(
        r"legal\w*", r"laws?", r"lawyers?", r"attorneys?", r"counsel\w*", r"contract\w*", r"agreements?", r"claus\w*",
        r"terms", r"terminat\w*", r"liabilit\w*", r"indemn\w*", r"warrant\w*", r"jurisdiction\w*", r"complian\w*",
        r"ndas?", r"sows?", r"msas?", r"dpas?", r"gdpr", r"licen[cs]\w*", r"patents?", r"trademarks?", r"copyrights?",
        r"lawsuits?", r"litigat\w*", r"courts?", r"sue", r"sued", r"settlements?", r"signs?", r"signed", r"signing",
        r"signatur\w*", r"e-?sign\w*", r"docusign", r"kyc", r"kyb", r"visas?", r"immigration", r"regulat\w*",
        r"obligations?", r"consent\w*", r"privacy", r"permits?",
        r"contrat\w*", r"clausul\w*", r"acordos?", r"advogad\w*", r"juridic\w*", r"rescis\w*", r"assin\w*",
        r"obrigac\w*", r"termos", r"leis?", r"accords?", r"avocats?", r"resiliation\w*", r"sign(?:er|ez|e)",
        r"acuerdos?", r"abogad\w*", r"rescision\w*", r"firm(?:a|ar|e|as|ado|ada)", r"obligacion\w*", r"terminos",
        r"leyes", r"ley", r"lois?")),
    ("send", _words(
        r"(?:re)?send\w*", r"sent", r"forward\w*", r"repl(?:y|ies|ied|ying)", r"respond\w*", r"e-?mail\w*", r"mails?",
        r"inbox\w*", r"messag\w*", r"dms?", r"posts?", r"posted", r"posting", r"publish\w*", r"tweet\w*",
        r"shar(?:e|es|ed|ing)", r"invit\w*", r"connect\w*", r"notif\w*", r"announc\w*", r"slack", r"whatsapp",
        r"telegram", r"imessage", r"sms", r"outreach", r"submit\w*", r"upload\w*", r"outbox", r"cc", r"bcc",
        r"envi(?:e|em|ar|a|am|ou|o)", r"reenvi\w*", r"encaminh\w*", r"mensage\w*", r"mensaj\w*", r"public\w*",
        r"compartilh\w*", r"convid\w*", r"envoy\w*", r"envoi\w*", r"repond\w*", r"partag\w*", r"compart\w*")),
    ("code", _words(
        r"code", r"codebase", r"coding", r"commit\w*", r"push\w*", r"merg\w*", r"rebas\w*", r"prs?", r"pull requests?",
        r"patch\w*", r"refactor\w*", r"implement\w*", r"fix\w*", r"bugs?", r"debug\w*", r"deploy\w*", r"migrat\w*",
        r"releas\w*", r"repos?", r"repositor\w*", r"git", r"github", r"branch\w*", r"hotfix\w*", r"schemas?", r"ci",
        r"config\w*", r"credential\w*", r"secrets?", r"passwords?", r"passcodes?", r"tokens?", r"api ?keys?", r"ssh",
        r"sudo", r"codigo", r"corrig\w*", r"consert\w*", r"implant\w*", r"deploi\w*", r"despleg\w*",
        r"despliegu\w*", r"arregl\w*")),
    ("destructive", _words(
        r"delet\w*", r"remov\w*", r"rm", r"rmdir", r"drop\w*", r"truncat\w*", r"wip(?:e|es|ed|ing)", r"purg\w*",
        r"eras\w*", r"destroy\w*", r"kill\w*", r"reset\w*", r"revok\w*", r"cancel\w*", r"unsubscrib\w*",
        r"overwrit\w*", r"prun\w*", r"clean ?up", r"nuk(?:e|es|ed|ing)", r"shred\w*", r"rotat\w*",
        r"apag\w*", r"exclu\w*", r"supprim\w*", r"effac\w*", r"detrui\w*", r"borr\w*", r"elimin\w*", r"suprim\w*",
        r"destru\w*", r"annul\w*")),
    ("research", _words(
        r"research\w*", r"synthes\w*", r"analy[sz]\w*", r"analis\w*", r"strateg\w*", r"estrateg\w*", r"recommend\w*",
        r"recomend\w*", r"recommand\w*", r"compar\w*", r"evaluat\w*", r"evalu\w*", r"avalia\w*", r"assess\w*",
        r"review\w*", r"reports?", r"relatorios?", r"rapports?", r"informes?", r"audit\w*", r"investigat\w*",
        r"pesquis\w*", r"recherch\w*", r"investiga\w*", r"due diligence", r"memos?")),
    ("write", _words(
        r"(?:re)?writ(?:e|es|ing|ten)", r"wrote", r"edit\w*", r"modif\w*", r"chang\w*", r"updat\w*", r"replac\w*",
        r"insert\w*", r"append\w*", r"creat\w*", r"add", r"adds", r"added", r"adding", r"renam\w*",
        r"mov(?:e|es|ed|ing)", r"cop(?:y|ies|ied|ying)", r"mv", r"cp", r"chmod", r"chown", r"install\w*",
        r"uninstall\w*", r"upgrad\w*", r"downgrad\w*", r"set", r"sets", r"setting", r"setup", r"put", r"puts",
        r"execut\w*", r"run", r"runs", r"running", r"ran", r"launch\w*", r"restart\w*", r"stop", r"stops", r"stopped",
        r"approv\w*", r"buy\w*", r"bought", r"orders?", r"ordered", r"book", r"booked", r"booking", r"schedul\w*",
        r"format\w*", r"convert\w*", r"resiz\w*", r"unzip\w*", r"zip", r"compress\w*", r"apply", r"applies",
        r"applied", r"enabl\w*", r"disabl\w*", r"grant\w*", r"assign\w*", r"draft\w*",
        r"escrev\w*", r"alter\w*", r"mud(?:a|ar|e|ou)", r"atualiz\w*", r"substitu\w*", r"cri(?:a|ar|e|ou)",
        r"adicion\w*", r"renome\w*", r"mov(?:a|er|eu|ido)", r"copi\w*", r"instal\w*", r"rod(?:e|ar)", r"aprov\w*",
        r"compr(?:a|ar|e|ou)", r"agend\w*", r"reserv\w*", r"ecri\w*", r"ecriv\w*", r"mett\w*", r"remplac\w*",
        r"cre(?:e|er|ez)", r"ajout\w*", r"renomm\w*", r"deplac\w*", r"lanc\w*", r"approuv\w*", r"achet\w*",
        r"planifi\w*", r"escrib\w*", r"modific\w*", r"cambi\w*", r"actualiz\w*", r"reemplaz\w*", r"cre(?:a|ar)",
        r"agreg\w*", r"anad\w*", r"renombr\w*", r"muev\w*", r"ejecut\w*", r"aprob\w*")),
)

# Auto level: the goal STARTS with one read-only verb ...
_MECHANICAL = re.compile(
    r"^\s*(?:fetch|get|list|read|look ?up|lookup|find|search|grep|count|extract|show|print|display|query|locate"
    r"|view)\b", re.IGNORECASE)
# ... holds no second clause, sequencing word or judgment once URLs and paths are blanked ...
_COMPOUND = re.compile(r"[,;&+|]|->|=>|\.\s+\S|\n|:\s|" + _words(
    r"and", r"then", r"also", r"after\w*", r"before", r"if", r"unless", r"when\w*", r"while", r"but", r"so", r"plus",
    r"or", r"whether", r"why", r"how", r"should", r"which", r"best", r"better", r"worst", r"cheap\w*", r"top",
    r"most", r"least", r"right", r"decid\w*", r"choos\w*", r"pick", r"judg\w*", r"think", r"plan\w*", r"design\w*",
    r"explain\w*", r"summar\w*", r"interpret\w*", r"verif\w*", r"done", r"working", r"green", r"passing", r"ready",
    r"live", r"fixed", r"e", r"et", r"y", r"o", r"ou", r"depois", r"entao", r"puis", r"ensuite", r"luego",
    r"despues", r"donc", r"pero", r"mais", r"mas").pattern, re.IGNORECASE)
# ... and reads as English: common pt/fr/es function words mean another language, so no auto level.
_OTHER_LANGUAGE = _words(
    r"le", r"la", r"les", r"des", r"du", r"une", r"un", r"est", r"pour", r"avec", r"dans", r"sur", r"au", r"aux",
    r"os", r"um", r"uma", r"para", r"com", r"em", r"na", r"nas", r"nos", r"da", r"das", r"dos", r"ao", r"el", r"los",
    r"las", r"con", r"del", r"al", r"por", r"que", r"en", r"es", r"arquivos?", r"fichiers?", r"archivos?")
_URL_OR_PATH = re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.)\S+|\S*[/\\~]\S*|\S+@\S+|\S+\.\w{1,8}\b", re.IGNORECASE)


def _fold(text: str) -> str:
    """Casefolded with accents stripped (``é`` -> ``e``), so one pattern serves accented and plain spellings."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def floor_reason(goal: str, context: str = "") -> Optional[str]:
    """The category (money, legal, send, code, destructive, research, write) that keeps a task at high or above,
    or None."""
    text = _fold(f"{goal}\n{context}")
    return next((category for category, pattern in _FLOOR if pattern.search(text)), None)


def is_mechanical(goal: str) -> bool:
    """True only for a positively identified single read-only operation in plain English: one mechanical verb
    first, no second clause, sequencing word or judgment, no other-language words."""
    if not goal.isascii() or len(goal) > _MAX_MECHANICAL_GOAL or not _MECHANICAL.match(goal):
        return False
    shape = _URL_OR_PATH.sub(" ", _fold(goal)).strip().rstrip(".")
    return not (_COMPOUND.search(shape) or _OTHER_LANGUAGE.search(shape))


def _level(value: Any) -> str:
    """A configured effort as a lowercase level name, ``""`` when unset (None, "", True)."""
    if value is False:
        return "false"
    return value.strip().lower() if isinstance(value, str) else ""


def choose_effort(goal: str, context: str, asked: Optional[str], default: Any) -> tuple[Optional[str], str]:
    """``(effort, why)`` for one task: the parent's pick, else the auto level or the default, under the floor."""
    reason, configured = floor_reason(goal, context), _level(default)
    if asked in LEVELS:
        if reason and asked not in _AT_OR_ABOVE_FLOOR:
            return FLOOR, f"floor: asked {asked}, {reason}"
        return asked, ""
    if reason:
        if configured in _AT_OR_ABOVE_FLOOR:
            return configured, "default"
        return FLOOR, f"floor: default {configured or 'inherited'}, {reason}"
    if configured not in _AT_OR_BELOW_AUTO and is_mechanical(goal):
        return _AUTO, "auto: mechanical"
    return configured or None, "default"


class EffortRouterProvider(DelegationProvider):
    @property
    def name(self) -> str:
        return "effort-router"

    @property
    def display_name(self) -> str:
        return "Effort router"

    @property
    def summary(self) -> str:
        return "Default route; each subagent's effort is tiered per task (medium for one read-only step, high floor)."

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        default = str((config or {}).get("reasoning_effort") or "the parent's level")
        return {"effort": {"type": "string", "enum": list(LEVELS), "description": (
            f"Reasoning effort for this subagent. low/medium only for clearly easy read-only mechanical work "
            f"(a lookup, fetch-and-extract, a listing, a single API read). Omitted = medium for one plain "
            f"read-only step, else {default}. xhigh only for genuinely hard work (deep multi-source research, "
            f"tricky debugging, architecture). Work touching money, legal, sends, code changes, deletes or research "
            f"synthesis, or that writes or changes anything, runs never below high.")}}

    def description(self, config: Mapping[str, Any]) -> str:
        return ("Effort tiering: give each task an `effort`. Easy read-only mechanical subtasks get low or medium; "
                "omitted = medium for one plain read-only step, else the default; money, legal, sends, code "
                "changes, deletes, research synthesis and any write are never below high. Your own subagents "
                "follow the same rule.")

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        route = {k: v for k, v in config.items() if k != "service_tier"}
        effort, why = choose_effort(str(task.get("goal") or ""), str(task.get("context") or ""),
                                    task.get("effort"), config.get("reasoning_effort"))
        if effort:
            route["reasoning_effort"] = effort
        route["label"] = f"effort {effort or 'inherited'}" + (f" ({why})" if why else "")
        logger.info("effort-router: task %s -> %s", task.get("index"), route["label"])
        return route


def register(ctx) -> None:
    ctx.register_delegation_provider(EffortRouterProvider())
