"""effort-router delegation strategy (``delegation.backend: effort-router``).

Same route as ``default`` (the ``delegation`` section), plus a per-task ``effort`` and, when
``delegation.backends.effort-router.options`` lists models, a per-task ``option`` (another model for that
subagent). The parent picks the effort per task. Strict tiers keep a cheap level away from work that matters:

- **Deep floor (xhigh).** A goal that is a review, audit, red-team, QA or fact-check, a plan or design, a debugging
  or root-cause hunt, or a synthesis/comparison/recommendation runs at ``xhigh`` (en/pt/fr/es), whoever asked
  less. A goal that is one plain read (``Read the review notes in /tmp/x``) is not deep work.
- **Floor (high).** Work whose goal or context touches money, legal, sends, access grants or permission changes,
  code changes, destructive operations or research synthesis, or names any write/action verb (en/pt/fr/es), never
  runs below ``high``, hyphenated compounds included (``wire-transfer``, ``re-send``); so does a goal that hands
  something to someone (``Get the deck to Armin``, ``Show Jackson the dashboard``). Floors only ever raise:
  an explicit cheaper pick is raised, a default below the floor is raised and an empty default keeps the level the
  child would inherit when that already meets it (``task["inherited_effort"]``), else carries the floor
  explicitly, so the child can never inherit a low parent level.
- **Cheap tiers.** ``low`` only for a deterministic task: one read-only step on a concrete target (a path, URL,
  id, file, row, field, or a fixed read-only command such as ``Run `git log -5` ``). ``medium`` only for one
  bounded read-only step (one read-only verb first, one operation, plain English). A cheaper explicit pick on
  anything else is raised (low -> medium for a bounded step, else high). When the parent omits ``effort`` the
  level drops to ``medium`` only for a deterministic task; everything else keeps ``delegation.reasoning_effort``.

The schema, guidance and rules are the same at every depth and on every option, so an orchestrator child tiers
its own sub-subtasks the same way and a GPT or Gemini reviewer gets the same tier as an Opus one. Each child's
route label says what was picked and why (``effort medium (auto: deterministic)``, ``effort high (floor: asked
low, money)``, ``gpt · effort xhigh (deep: review)``).

Options use the official ``model_map`` entry shape (``advanced-delegation``): ``name``, ``description``,
``provider`` and/or ``base_url``, ``model``, optional ``api_key``, ``api_mode``, ``service_tier``,
``reasoning_effort`` (that option's default level, still under the tiers) and ``fallback_providers`` (absent =
``delegation.fallback_providers``; ``[]`` = none, so a GPT reviewer never quietly becomes another model).
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
DEEP = "xhigh"
_AUTO = "medium"
_AT_OR_BELOW_AUTO = frozenset({"minimal", "low", "medium", "none", "false", "disabled"})
_RANK = {"none": 0, "false": 0, "disabled": 0, "minimal": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5,
         "ultra": 6}
_MAX_MECHANICAL_GOAL = 160
#: Deep work is read from the goal's head: a long brief names its task first, and words further on are topics
#: (``... cross-model review, and stop conditions``), not the job.
_DEEP_HEAD = 200
OPTION_PARAMETER = "option"
#: Option entry keys copied into the route (same meaning as the matching core route fields and ``model_map``).
_OPTION_ROUTE_FIELDS = ("provider", "model", "base_url", "api_key", "api_mode", "service_tier")
#: Default-route keys an option replaces: its model identity and the request knobs tuned for the default model.
_MODEL_KEYS = frozenset({"provider", "model", "base_url", "api_key", "api_mode", "request_overrides"})


def _words(*alternatives: str) -> re.Pattern:
    """One case-insensitive whole-word pattern, matched on accent-folded text (``_fold``)."""
    return re.compile(r"(?<![\w-])(?:" + "|".join(alternatives) + r")(?![\w-])", re.IGNORECASE)


# Floor triggers on accent-folded goal + context, first match names the category. A false positive only raises
# a task to high (the safe direction), so the lists lean broad; ``\w*`` stems cover pt/fr/es conjugations.
# Hyphens (and other dashes) separate words here: every one for the sensitive topics, so ``wire-transfer`` and
# ``invoice-1023`` still name money; for the generic write verbs (``_LETTER_HYPHENS_ONLY``) only one between two
# letters, so ``re-write`` is a write but a name like ``run-1119`` is not an action.
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
    ("access", _words(
        r"acces\w*", r"acess\w*", r"admin\w*", r"permis\w*", r"privil\w*", r"(?:on|off)board(?:s|ed)?",
        r"offboarding", r"(?:de)?provision\w*", r"seats?", r"sudoers?", r"(?:allow|white|block|black)list\w*",
        r"(?:un)?ban(?:s|ned|ning)?")),
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
        r"destru\w*", r"annul\w*", r"rid", r"deactivat\w*", r"suspend\w*")),
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
        r"agreg\w*", r"anad\w*", r"renombr\w*", r"muev\w*", r"ejecut\w*", r"aprob\w*", r"back up",
        r"reboot\w*", r"start(?:s|ed|ing)?")),
)
_LETTER_HYPHENS_ONLY = frozenset({"write"})
_DASHES = re.compile(r"[\u2010-\u2015\u2212]")  # hyphen, en/em dash, minus ... all read as "-"
_LETTER_HYPHEN = re.compile(r"(?<=[^\W\d_])-(?=[^\W\d_])")

# A light verb handing something to someone is a send: ``Get the deck to Armin`` / ``to the team``, ``Show Jackson
# the dashboard``, ``Get the contributors their Scout codes``. Read on the raw goal, since a name is what marks the
# recipient; ``me`` / ``us`` (the parent asking for a result) is not one.
_HANDOFF = re.compile(
    r"^\s*(?:(?i:please)\s+)?(?i:get|show|give|hand|bring|take|pass|deliver|provide)\s+"
    r"(?:.*\b(?i:to)\s+(?:[A-Z]|(?i:(?:the\s+)?(?:team|clients?|buyers?|customers?|users?|contributors?|partners?"
    r"|investors?|board|group|channel)|everyone|everybody|all)\b)"
    r"|(?!(?i:me|us|it)\b)(?:[A-Z][\w'.-]*|(?i:him|her|them|everyone|everybody)|(?i:the|our|all|my|your)\s+\w+)\s+"
    r"(?i:the|their|his|her|its|our|your|my|a|an|this|that|these|those|every|some)\b)")
# Auto level: the goal STARTS with one read-only verb ...
_MECHANICAL = re.compile(
    r"^\s*(?:fetch|list|read|look ?up|lookup|find|search|grep|count|extract|print|display|query|locate|view)\b",
    re.IGNORECASE)
# A plain read (not find/search/look up, which also start investigations: "Find the root cause of ...").
_PLAIN_READ = re.compile(r"^\s*(?:fetch|list|read|grep|count|extract|print|display|view)\b", re.IGNORECASE)
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

# Deep floor (xhigh): the GOAL is a review, a plan, a debugging hunt or a synthesis. Read on the accent-folded goal
# with URLs and paths blanked and dashes as spaces, so a file named plan-v2.md or a red-team folder decides nothing.
_DEEP = (
    ("review", _words(
        r"review\w*", r"audit\w*", r"red ?team\w*", r"adversar\w*", r"critiqu\w*", r"cross ?examin\w*",
        r"fact ?check\w*", r"scrutin\w*", r"second opinion", r"sanity ?check\w*", r"re ?scor\w*", r"qa",
        r"revis(?:ar|ao|oes|e|er|ion|iones)", r"revue", r"relis\w*", r"auditor\w*", r"canonical check\w*")),
    # evaluate / assess / judge as the task, never as a description (``vendors Hub is evaluating``).
    ("review", re.compile(
        r"(?:^|[.;:!?]\s+|\band\s+|\bthen\s+)(?:evaluate|assess|judge|grade|rank|score|avali\w*|evalu\w*)\b"
        r"|\b(?:evaluat\w*|assess\w*|judg\w*|avali\w*)\s+(?:the|a|an|our|these|this|each|every|whether|how|which"
        r"|its|their)\b|\b(?:evaluation|assessment)\s+of\b")),
    ("review", re.compile(r"\bverif\w*\b.{0,60}\b(?:fix|fixes|claims?|findings?|facts?|figures?|numbers?|results?"
                          r"|statements?|assumptions?|estimates?|math)\b")),
    ("planning", _words(
        r"plann\w*", r"roadmap\w*", r"system design", r"design doc\w*", r"planej\w*",
        r"planific\w*", r"planeac\w*")),
    ("planning", re.compile(
        r"^\s*(?:plan|design|architect|strategi[sz]e)\b|\b(?:write|draft|make|build|create|produce|propose|prepare|"
        r"design)\s+(?:\w+\s+){0,3}(?:plan|strategy|spec|design|architecture)\b|\b(?:implementation|migration|rollout|execution|"
        r"action|project|test|launch|go to market|gtm|research|hiring|pricing)\s+(?:plan|strategy)\b|\bdesign\s+"
        r"(?:the|a|an|our)\s+(?:system|architecture|pipeline|schema|api|approach|flow|protocol|algorithm)\b")),
    ("debugging", _words(
        r"debug\w*", r"root ?caus\w*", r"diagnos\w*", r"troubleshoot\w*", r"bisect\w*", r"post ?mortem\w*",
        r"depur\w*", r"debogu\w*", r"causa raiz", r"cause racine")),
    ("debugging", re.compile(
        r"\bwhy\b.{0,80}\b(?:fail\w*|break\w*|broke|broken|crash\w*|hang\w*|stuck|slow\w*|errors?|wrong|missing|lost|"
        r"drop\w*|flak\w*|leak\w*|timeout\w*|timing out)\b|\binvestigat\w*\b.{0,60}\b(?:bugs?|crash\w*|failures?|"
        r"errors?|regressions?|outages?|leaks?|flak\w*|incidents?|issues?|slowness|latency)\b")),
    ("synthesis", _words(
        r"synthes\w*", r"sintetiz\w*", r"synthetis\w*", r"consolidat\w*", r"cross ?check\w*", r"compar\w*",
        r"trade ?offs?", r"due diligence", r"verdicts?")),
    # recommend as the deliverable, never as reported speech (``what the community recommends``).
    ("synthesis", re.compile(
        r"^\s*(?:recommend|recomend|recommand)\w*\b|\brecommend\s+(?:a|an|the|one|which|what|whether|how|our|your)\b"
        r"|\b(?:give|make|write|with|plus|and)\s+(?:a\s+|an\s+|your\s+|one\s+|clear\s+)?recommendations?\b"
        r"|\b(?:decision|recommendation|investment)\s+memos?\b")),
    ("synthesis", re.compile(r"\bsummar\w*\b.{0,40}\b(?:findings|lanes|reports|results|research|reviews|sources)\b")),
)
# A concrete target makes a read-only step deterministic: a URL, path, address, file name, quoted string, hash or
# id, or a named record-like object.
_TARGET = re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.)\S+|\S*[/\\~]\S*|\S+@\S+|\S+\.\w{1,8}\b|`[^`]+`|\"[^\"]+\""
                     r"|\b[0-9a-f]{7,40}\b|\b\d{4,}\b|\b[a-z]{2,}-\d+\b|" + _words(
                         r"files?", r"folders?", r"director(?:y|ies)", r"dirs?", r"logs?", r"logfiles?", r"rows?",
                         r"columns?", r"fields?", r"tables?", r"sheets?", r"cells?", r"records?", r"entr(?:y|ies)",
                         r"lines?", r"headers?", r"json", r"csv", r"yaml", r"yml", r"toml", r"xml", r"pdfs?",
                         r"env vars?", r"process(?:es)?", r"ports?", r"pids?", r"shas?", r"hash(?:es)?").pattern,
                     re.IGNORECASE)
# A fixed read-only shell command in backticks is a deterministic step: ``Run `git log -5 --oneline` in /repo``.
_RUN_COMMAND = re.compile(r"^\s*(?:run|execute)\s+`(?P<cmd>[^`]+)`", re.IGNORECASE)
_READ_ONLY_PROGRAMS = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "stat", "du", "df", "ps", "pwd", "date", "uname", "whoami",
    "uptime", "free", "which", "file", "tree", "jq", "sha256sum", "md5sum", "nproc", "lsblk", "hostname"})
_READ_ONLY_SUBCOMMANDS = {
    "git": frozenset({"log", "status", "show", "diff", "rev-parse", "ls-files", "blame", "describe", "shortlog"}),
    "systemctl": frozenset({"status", "is-active", "is-enabled", "show", "list-units", "list-timers"}),
}
_SHELL_OPERATORS = re.compile(r"[;&|<>$`()]|\s-(?:delete|exec|execdir|i|ok)\b|\s--(?:in-place|output|write)\b")


def _fold(text: str) -> str:
    """Casefolded with accents stripped (``é`` -> ``e``), so one pattern serves accented and plain spellings."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def floor_reason(goal: str, context: str = "") -> Optional[str]:
    """The category (money, legal, send, access, code, destructive, research, write) that keeps a task at high or
    above, or None. A fixed read-only command (``Run `git log -5` ``) is read without its verb and program."""
    goal = _without_read_only_command(goal)
    text = _DASHES.sub("-", _fold(f"{goal}\n{context}"))
    split = {False: text.replace("-", " "), True: _LETTER_HYPHEN.sub(" ", text)}
    return next((category for category, pattern in _FLOOR
                 if pattern.search(split[category in _LETTER_HYPHENS_ONLY])),
                "send" if _HANDOFF.match(goal) else None)


def deep_reason(goal: str) -> Optional[str]:
    """The kind of deep work (review, planning, debugging, synthesis) the goal asks for, which runs at xhigh, or
    None. One plain read (read, fetch, list ...) is never deep work, whatever it reads."""
    if is_mechanical(goal) and _PLAIN_READ.match(_without_read_only_command(goal)):
        return None
    text = _DASHES.sub(" ", _URL_OR_PATH.sub(" ", _fold(goal))).replace("-", " ")[:_DEEP_HEAD]
    return next((category for category, pattern in _DEEP if pattern.search(text)), None)


def _read_only_command(goal: str) -> Optional[re.Match]:
    """The ``Run `<cmd>` `` match when cmd is one fixed read-only command (allowlisted program or subcommand, no
    shell operators), else None."""
    match = _RUN_COMMAND.match(goal)
    if not match or _SHELL_OPERATORS.search(f" {match['cmd']} "):
        return None
    words = match["cmd"].split()
    if not words:
        return None
    program, sub = words[0].rsplit("/", 1)[-1], next((w for w in words[1:] if not w.startswith("-")), "")
    if program in _READ_ONLY_PROGRAMS or sub in _READ_ONLY_SUBCOMMANDS.get(program, ()):
        return match
    return None


def _without_read_only_command(goal: str) -> str:
    """The goal with a fixed read-only command's verb and program blanked (its arguments stay, so ``git log
    --grep=password`` still reads as secrets)."""
    match = _read_only_command(goal)
    if not match:
        return goal
    words = match["cmd"].split()
    keep = words[2:] if words[0].rsplit("/", 1)[-1] in _READ_ONLY_SUBCOMMANDS else words[1:]
    return "List " + " ".join(keep) + goal[match.end():]


def is_mechanical(goal: str) -> bool:
    """True only for a positively identified single read-only operation in plain English: one mechanical verb
    first (or one fixed read-only command), no second clause, sequencing word or judgment, no other-language
    words. The bounded step ``medium`` is for."""
    goal = _without_read_only_command(goal)
    if not goal.isascii() or len(goal) > _MAX_MECHANICAL_GOAL or not _MECHANICAL.match(goal):
        return False
    shape = _URL_OR_PATH.sub(" ", _fold(goal)).strip().rstrip(".")
    return not (_COMPOUND.search(shape) or _OTHER_LANGUAGE.search(shape))


def is_deterministic(goal: str) -> bool:
    """A mechanical step on a concrete target (path, URL, id, file, row, field ...) or a fixed read-only command:
    the only work ``low`` is for, and the only work an omitted effort sends to ``medium``."""
    if _read_only_command(goal):
        return is_mechanical(goal)
    return is_mechanical(goal) and bool(_TARGET.search(goal.split(None, 1)[1] if " " in goal.strip() else ""))


def _level(value: Any) -> str:
    """A configured effort as a lowercase level name, ``""`` when unset (None, "", True)."""
    if value is False:
        return "false"
    return value.strip().lower() if isinstance(value, str) else ""


def _cheapest(goal: str) -> str:
    """The lowest level this goal may run at when nothing floors it: low (deterministic), medium (bounded), high."""
    return "low" if is_deterministic(goal) else "medium" if is_mechanical(goal) else FLOOR


def _floor(goal: str, context: str) -> tuple[Optional[str], Optional[str]]:
    """``(level, why)`` the task may not run below: xhigh for deep work, high for sensitive or write work."""
    deep = deep_reason(goal)
    if deep:
        return DEEP, f"deep: {deep}"
    reason = floor_reason(goal, context)
    return (FLOOR, f"floor: {reason}") if reason else (None, None)


def choose_effort(goal: str, context: str, asked: Optional[str], default: Any,
                  inherited: Any = "") -> tuple[Optional[str], str]:
    """``(effort, why)`` for one task: the parent's pick, else the auto level or the default, under the floors and
    the cheap-tier rules. ``inherited`` is the level the child gets when the route sets none (``""`` = unknown or
    the provider default); floors only ever raise it."""
    floor, why = _floor(goal, context)
    configured = _level(default)
    if asked in LEVELS:
        if floor and _RANK[asked] < _RANK[floor]:
            return floor, why.replace(": ", f": asked {asked}, ", 1)
        cheapest = _cheapest(goal)
        if _RANK[asked] < _RANK[cheapest]:
            reason = "not deterministic" if asked == "low" else "not one bounded read-only step"
            return cheapest, f"raised: asked {asked}, {reason}"
        return asked, ""
    if floor:
        if _RANK.get(configured, -1) >= _RANK[floor]:
            return configured, "default"
        if not configured and _RANK.get(_level(inherited), -1) >= _RANK[floor]:
            return None, why.replace(": ", f" met: {_level(inherited)}, ", 1)
        return floor, why.replace(": ", f": default {configured or 'inherited'}, ", 1)
    if configured not in _AT_OR_BELOW_AUTO and is_deterministic(goal):
        return _AUTO, "auto: deterministic"
    return configured or None, "default"


def configured_options(settings: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{name: {"description", "route", "effort"}}`` from ``delegation.backends.effort-router.options`` in config
    order (``model_map``'s entry shape). Malformed entries and repeated names are skipped with one warning each."""
    raw = settings.get("options") if isinstance(settings, Mapping) else None
    if not isinstance(raw, list):
        if raw is not None:
            _warn_once("delegation.backends.effort-router.options must be a list; ignoring it.")
        return {}
    options: dict[str, dict[str, Any]] = {}
    for i, entry in enumerate(raw):
        problem = _option_problem(entry)
        name = _text(entry, "name") if not problem else ""
        if problem or name in options:
            _warn_once(f"delegation.backends.effort-router.options[{i}] {problem or 'repeats ' + repr(name)}; skipped.")
            continue
        route = {key: entry[key].strip() for key in _OPTION_ROUTE_FIELDS
                 if isinstance(entry.get(key), str) and entry[key].strip()}
        if isinstance(entry.get("fallback_providers"), list):
            route["fallback_providers"] = list(entry["fallback_providers"])
        options[name] = {"description": _text(entry, "description"), "route": route,
                         "effort": _text(entry, "reasoning_effort")}
    return options


def _text(entry: Any, key: str) -> str:
    value = entry.get(key) if isinstance(entry, Mapping) else None
    return " ".join(value.split()) if isinstance(value, str) else ""


def _option_problem(entry: Any) -> str:
    if not isinstance(entry, Mapping):
        return "is not a mapping"
    if not _text(entry, "name") or " " in _text(entry, "name") or not _text(entry, "description"):
        return "needs a one-word name and a description"
    if not _text(entry, "model") or not (_text(entry, "provider") or _text(entry, "base_url")):
        return "needs a model and a provider or base_url"
    return ""


_warned: set[str] = set()


def _warn_once(message: str) -> None:
    # task_parameters / route_task run on every session build and every task: say each problem once.
    if message not in _warned:
        _warned.add(message)
        logger.warning(message)


class EffortRouterProvider(DelegationProvider):
    @property
    def name(self) -> str:
        return "effort-router"

    @property
    def display_name(self) -> str:
        return "Effort router"

    @property
    def summary(self) -> str:
        return ("Default route with strict per-task effort tiers (xhigh for reviews, plans, debugging and synthesis; "
                "high floor; low only for deterministic reads), plus optional per-task model options.")

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        default = str((config or {}).get("reasoning_effort") or "the parent's level")
        params = {"effort": {"type": "string", "enum": list(LEVELS), "description": (
            f"Reasoning effort for this subagent. low ONLY for a deterministic step (read a known file, fetch a "
            f"known URL, run a fixed read-only command, extract fields); medium only for one bounded read-only "
            f"step with an objective answer. Omitted = {default} (medium for a deterministic step). Reviews, "
            f"audits, QA, plans, debugging and synthesis run at xhigh; money, legal, sends, code changes, deletes "
            f"and any write never below high. A cheaper pick is raised.")}}
        options = configured_options(self.settings(config))
        if options:
            lines = ["Which configured model runs this subagent. Omit for the default subagent model. Same effort "
                     "tiers on every option.", *(f"{name}: {option['description']}" for name, option in options.items())]
            params[OPTION_PARAMETER] = {"type": "string", "enum": list(options), "description": "\n".join(lines)}
        return params

    def description(self, config: Mapping[str, Any]) -> str:
        text = ("Effort tiering: give each task an `effort`. low only for deterministic reads, medium only for one "
                "bounded read-only step, else the default; reviews, plans, debugging and synthesis run xhigh; money, "
                "legal, sends, code changes, deletes and any write never below high. Your own subagents follow the "
                "same rule.")
        if configured_options(self.settings(config)):
            text += (" `option` runs a subagent on another model; for an adversarial or multi-model review give "
                     "each option the same brief, blind to the others, then cross-examine where they disagree.")
        return text

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        route = {k: v for k, v in config.items() if k != "service_tier"}
        default, chosen = config.get("reasoning_effort"), task.get(OPTION_PARAMETER)
        if chosen:
            # Core already refused a value outside the enum; the option replaces the default route's model.
            option = configured_options(self.settings(config))[chosen]
            route = {k: v for k, v in route.items() if k not in _MODEL_KEYS} | option["route"]
            default = option["effort"] or default
        effort, why = choose_effort(str(task.get("goal") or ""), str(task.get("context") or ""),
                                    task.get("effort"), default, task.get("inherited_effort"))
        if effort:
            route["reasoning_effort"] = effort
        label = f"effort {effort or 'inherited'}" + (f" ({why})" if why else "")
        route["label"] = f"{chosen} · {label}" if chosen else label
        logger.info("effort-router: task %s -> %s", task.get("index"), route["label"])
        return route


def register(ctx) -> None:
    ctx.register_delegation_provider(EffortRouterProvider())
