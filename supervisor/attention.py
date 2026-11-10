"""The attention schema, scored against the tool log.

A live cycle may declare in `declare_intent` where it expects to look: up to four {source, why}. Graziano's attention
schema theory says a model of one's own attention is for controlling it; the agent uses the declaration as a top-down
prior for its workspace. Here the supervisor checks the declaration against what the cycle then did — the share of its
calls that went to a source it named, and the named sources it never touched. The score feeds nothing: no probation,
no reward. Only the knob trial (`shaft experiment attention`) decides whether the field stays.

A call touches its tool and what the tool reaches (TOOL_SOURCES: mail, calendar, record, memory, web and the host it
asked; a plugin, what its name says: PLUGIN_SOURCES), the files in its arguments, the files a run's command names (or
that a script the cycle wrote and then ran names), a message's recipient, and a plugin's own code. A declared source is
read as terms: free text split on ' / ', '+', ','; a parenthesis is a comment; an ellipsis or a slash makes a folder,
and a folder is a prefix. A source with no term it can read is reported unreadable, never as untouched.
Deterministic, no model calls.
"""
from __future__ import annotations

import fnmatch
import functools
import json
import posixpath
import re
from urllib.parse import urlsplit

from . import metrics

CONTROLS = ("declare_intent", "confirm_confidence", "settle_commitment", "finish_cycle")
MEMORY_TOOLS = ("search_memory", "remember", "rewrite_facts", "write_digest", "update_self_model", "update_personality")
# Where a tool looks when its arguments do not say: the supervisor's own tools. A plugin an instance built reaches what
# its name says (PLUGIN_SOURCES: gmail and mail_tail are mail, cal-watch calendar, gdrive drive). Every tool touches
# its own name.
TOOL_SOURCES = {
    "web_search": ("web",), "fetch_page": ("web",), "http_request": ("web",), "browse": ("web",),
    "drive_search": ("drive",), "drive_read": ("drive",), "drive_download": ("drive",),
    "gchat_search": ("gchat",), "gchat_read": ("gchat",), "gchat_download": ("gchat",),
    "web_research": ("web",), "mail_search": ("mail",), "mail_read": ("mail",), "mail_attachment": ("mail",),
    "calendar_events": ("calendar",), "calendar_event": ("calendar",), "calendar_create": ("calendar",),
    "calendar_move": ("calendar",), "calendar_respond": ("calendar",), "record": ("record",), "history": ("record",),
    "delegate_coding": ("coder",), **{t: ("memory",) for t in MEMORY_TOOLS}}
PLUGIN_SOURCES = ((re.compile(r"mail"), "mail"), (re.compile(r"(?:^|[-_])cal(?:endar)?(?:$|[-_])"), "calendar"),
                  (re.compile(r"drive"), "drive"), (re.compile(r"notion"), "notion"))
# The agent's own tools (a copy of shaft/tools.py's built-ins and the control and sleep tools): with the supervisor's
# (broker.SPECS) they are the calls that are not a plugin. A plugin call also touches plugins/<name>/main.py.
AGENT_TOOLS = frozenset({
    "read_file", "write_file", "append_file", "list_files", "rewrite_facts", "write_digest", "search_memory",
    "remember", "update_self_model", "update_personality", "view_image", "commit_to", "settle_commitment",
    "close_surprise", "retire_fact", "open_project", "close_project", "propose_drive_amendment", "record_correction",
    "close_correction", *CONTROLS})
PATH_ARGS = ("path", "save_to", "save_as", "body_file")
# Words a declared source may use for what the calls touch. Plain "chat" is not one: it reads as the web UI's
# conversations with an instance, not the operator's Google Chat.
SYNONYMS = {"internet": ("web",), "email": ("mail",), "e-mail": ("mail",), "google drive": ("drive",),
            "google chat": ("gchat",),
            "history": ("record",), "code": ("shaft", "plugins", "tests", "run"), "another instance": ("instance",),
            "other instance": ("instance",), "instances": ("instance",), "the operator": ("operator",)}
VOCAB = frozenset({"web", "memory", "mail", "calendar", "drive", "gchat", "notion", "record", "coder", "operator", "instance",
                   "plugin", "run", "notes", "self", "skills", "shaft", "plugins", "tests", "inbox", "attachments",
                   *SYNONYMS, *TOOL_SOURCES})
_PATH = re.compile(r"(?:\.{1,2}/|/|~/)?[\w.@+~*-]+(?:(?:/[\w.@+~*-]+)+/?|/)")
_FILE = re.compile(r"(?<![\w/.-])[\w.-]*\w\.(?:py|md|json|jsonl|txt|sh|log|csv|tsv|html|ya?ml|toml|xml|pdf)\b")
_MODULE = re.compile(r"(?<![\w./-])(?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*")
_SPLIT = re.compile(r"\s+/\s+|\s*[+,;&]\s*|\s+(?:and|or)\s+")


def _args(entry: dict) -> dict:
    try:
        args = json.loads(entry.get("args") or "{}")
        return args if isinstance(args, dict) else {}
    except ValueError:   # the log cut a long argument: the keys this needs are short and come first
        return {k: m.group(1) for k in ("path", "url", "to", "command", *PATH_ARGS)
                if (m := re.search(rf'"{k}"\s*:\s*"((?:[^"\\]|\\.)*)"', entry.get("args") or ""))}


def _norm(path: str) -> str:
    """A path as a source names it: lower case, no './', no trailing slash; '' for the folder itself."""
    p = path.strip().strip("'\"`").lower()
    if not p:
        return ""
    p = posixpath.normpath(p)
    return "" if p == "." else p


def _paths(path: str) -> set[str]:
    p = _norm(path)
    if not p:
        return set()
    return {p, "memory"} if p == "memory" or p.startswith("memory/") else {p}


def _in_command(command: str) -> set[str]:
    """The files a shell command names: paths with a slash, bare file names, and the test modules a unittest or
    pytest run is given (tests.test_x is tests/test_x.py)."""
    command = re.sub(r"\w+://\S+", " ", command)   # a URL is the web, not a file
    out = set()
    for m in _PATH.finditer(command):
        out |= _paths(m.group(0))
    out |= {_norm(m.group(0)) for m in _FILE.finditer(command)}
    for seg in re.split(r"[;&|\n]", command):
        if re.search(r"\b(?:unittest|pytest)\b|\s-m\s", seg):
            out |= {_norm(m.group(0).replace(".", "/") + ".py") for m in _MODULE.finditer(seg)
                    if not re.search(r"\.(?:py|md|json|jsonl|txt|sh|log)$", m.group(0))}
            out |= {_norm(m.group(1)) for m in re.finditer(r"\s-s\s+([\w./-]+)", seg)}   # discover -s tests
    return out - {""}


def touches(entry: dict) -> set[str]:
    """Where one call looked, as words and paths a declared source can name: the tool itself and what it reaches
    ('mail', 'calendar', 'record', 'memory', 'web' and the host), the files in its arguments and in a run's command,
    the recipient of a message, and a plugin's own code."""
    tool, args = str(entry.get("tool") or ""), _args(entry)
    out = {tool.lower(), *TOOL_SOURCES.get(tool, ())}
    if (fixed := metrics.WRITERS.get(tool)) and "/" in fixed:
        out |= _paths(fixed)
    for key in PATH_ARGS:
        if isinstance(args.get(key), str):
            out |= _paths(args[key])
    if isinstance(args.get("url"), str) and (host := (urlsplit(args["url"]).hostname or "").lower()):
        labels = host.split(".")
        out |= {"web", *(".".join(labels[i:]) for i in range(len(labels) - 1))}
        if len(labels) >= 2:
            out.add(labels[-2])            # api.notion.com is 'notion'
    if tool == "send_message":
        to = str(args.get("to") or "").lower()       # a send without a recipient went nowhere
        out |= {"message"} | ({to} if to else set()) | ({"instance"} if to and to != "operator" else set())
    if tool == "run":
        out |= _in_command(str(args.get("command") or ""))
    if tool and tool not in AGENT_TOOLS and tool not in _supervisor_tools():
        out |= {"plugin", f"plugins/{tool.lower()}/main.py",
                *(source for word, source in PLUGIN_SOURCES if word.search(tool.lower()))}
    return out - {""}


def _through_scripts(calls: list[dict]) -> list[set[str]]:
    """Each call's touches(), where a run of a script the cycle wrote (write_file/append_file of .py or .sh) also
    touches the files the script names: python3 .tmp/patch.py reads what patch.py opens."""
    scripts: dict[str, set[str]] = {}
    out = []
    for e in calls:
        t = touches(e)
        args = _args(e)
        if e.get("tool") == "run":
            t |= set().union(*(scripts[p] for p in t if p in scripts))
        elif e.get("tool") in ("write_file", "append_file") and isinstance(args.get("content"), str) \
                and re.search(r"\.(?:py|sh|zsh|bash)$", p := _norm(str(args.get("path") or ""))):
            scripts[p] = scripts.get(p, set()) | _in_command(args["content"])
        out.append(t)
    return out


@functools.cache
def _supervisor_tools() -> frozenset:
    from .broker import SPECS
    return frozenset(SPECS)


def terms(source: str, vocab=VOCAB) -> list[str]:
    """What a declared source names, as terms touches() can hit; [] when it names nothing the scorer can read.
    Free text is split on ' / ', '+', ',', 'and'; a parenthesis is a comment; an ellipsis or a trailing slash makes a
    folder (every folder is a prefix); in a phrase only the words that are a path or a known source count."""
    s = re.sub(r"\([^)]*\)", " ", " ".join(str(source).lower().split()))
    out: list[str] = []
    for part in _SPLIT.split(s):
        part = part.strip().replace("…", "").replace("...", "").strip()
        if not part:
            continue
        if part in SYNONYMS:
            out += SYNONYMS[part]
            continue
        words = part.split()
        if len(words) == 1 and "/" in part and all(w in vocab for w in part.split("/")):
            words = part.split("/")            # mail_search/mail_read names two tools
        elif len(words) > 1:
            words = [w for w in words if w in vocab or re.search(r"[/*]|\w\.\w|^\.\w", w)]
        for w in words:                       # a tool names its source too: mail_search is mail
            out += SYNONYMS.get(w, (w, *TOOL_SOURCES.get(w, ())))
    return [t for t in dict.fromkeys(_norm(t) for t in out) if t]


def _hit(term: str, touched: set[str]) -> bool:
    if any(c in term for c in "*?["):
        return any(fnmatch.fnmatchcase(x, term) for x in touched)
    if term in touched:
        return True
    if "/" not in term and re.search(r"\w\.\w", term) and any(x.endswith("/" + term) for x in touched):
        return True                           # a bare file name: main.py
    if "/" in term:                           # a folder, or the start of a name in one: notes/doc is notes/docs/x.md
        return any(x.startswith(term) for x in touched)
    return any(x.startswith(term + sep) for x in touched for sep in ("/", "_", "-"))   # mail is mail_read


def matches(source: str, touched: set[str], vocab=VOCAB) -> bool:
    """Whether a call that touched these went where a declared source points."""
    return any(_hit(t, touched) for t in terms(source, vocab))


def score(ep: dict) -> dict | None:
    """One cycle: the declared sources, its calls (controls left out), the share of calls on a named source, the named
    sources it never touched, and the declared entries it could not read (no source, or a source naming no path and no
    known source: those are neither touched nor untouched). None when it declared none."""
    declared = ((ep.get("intent") or {}).get("attention")) or []
    if not isinstance(declared, list) or not declared:
        return None
    sources = [str(a.get("source") or "").strip() for a in declared if isinstance(a, dict)]
    missing = len(declared) - len([s for s in sources if s])
    sources = [s for s in sources if s]
    calls = [e for e in ep.get("tools") or [] if e.get("tool") not in CONTROLS]
    touched = _through_scripts(calls)
    vocab = VOCAB | {str(e.get("tool") or "").lower() for e in calls} | set().union(*touched)
    readable = [s for s in sources if terms(s, vocab)]
    on = sum(1 for t in touched if any(matches(s, t, vocab) for s in readable))
    untouched = [s for s in readable if not any(matches(s, t, vocab) for t in touched)]
    return {"declared": sources, "calls": len(calls), "on_named": on,
            "share": on / len(calls) if calls else None, "untouched": untouched,
            "unreadable": missing + len(sources) - len(readable),
            "unreadable_sources": [s for s in sources if s not in readable]}


def section(name: str) -> list[str]:
    """stats.md: how well where I said I would look matched where I looked."""
    scored = [s for s in (score(ep) for ep in metrics.live_episodes(name)) if s]
    if not scored:
        return []
    shares = [s["share"] for s in scored if s["share"] is not None]
    named = sum(len(s["declared"]) for s in scored)
    untouched = sum(len(s["untouched"]) for s in scored)
    lines = ["", "## Where I said I would look, and where I looked (attention schema)",
             f"- Cycles that declared it: {len(scored)}; calls on a source I named: "
             + (f"{sum(shares) / len(shares):.0%} on average" if shares else "no calls"),
             f"- Named sources I never touched: {untouched} of {named}"]
    if (bad := sum(s["unreadable"] for s in scored)):
        lines.append(f"- Declared entries the scorer could not read (no source, or no path or known source in it): {bad}")
    last = scored[-1]
    if last["untouched"]:
        lines.append(f"- Last cycle that declared it named but never touched: {', '.join(last['untouched'][:4])}")
    return lines


def report(names: list[str]) -> str:
    out = []
    for name in names:
        scored = [(ep["ts"], s) for ep in metrics.live_episodes(name) if (s := score(ep))]
        out.append(f"{name}: {len(scored)} cycle(s) declared where they would look")
        for ts, s in scored[-20:]:
            share = "no calls" if s["share"] is None else f"{s['share']:.0%}"
            out.append(f"  {ts[:16]}  {share} of {s['calls']} calls on {', '.join(s['declared'])}"
                       + (f"; never touched: {', '.join(s['untouched'])}" if s["untouched"] else "")
                       + (f"; unreadable: {', '.join(s['unreadable_sources'])}" if s["unreadable_sources"] else ""))
    return "\n".join(out) + "\n"
