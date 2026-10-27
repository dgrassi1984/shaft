"""My tools: built-ins, plugins I (or my ancestors) wrote, and the supervisor's broker tools.

Every tool takes JSON arguments and returns text. A failing tool returns
"ERROR: ..." so I can adapt, instead of crashing the cycle.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import broker, clock, images, memory, personality, selfedit

MAX_RESULT_CHARS = 6000
# My tool log is what the judge grades my cycles from: it keeps what I saw. It was once cut at 2,000
# characters while I saw 6,000, and the judge marked my read-backs of self/self.md "cut off" — unsupported.
LOG_CHARS = MAX_RESULT_CHARS
FACTS_MAX_CHARS = 6000     # facts.md is shown in full: consolidation keeps it to this
DIGEST_MAX_CHARS = 1500
SOURCE = re.compile(r" \(src: (.+)\)\s*$", re.M)   # a fact line's source, as remember writes it (a URL may hold ")")
FACT_LINE = re.compile(r"^- \[\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?\] \S")   # every result stays in context for the rest of the cycle
PLUGIN_TIMEOUT_S = 120

# Paths I may not overwrite with write_file: append-only history, files with their own
# validated tools, and files the supervisor writes about me.
PROTECTED = {
    "memory/journal.md": "the journal is append-only; it is written at the end of each cycle",
    "memory/facts.md": "use remember to add a fact, rewrite_facts to consolidate (dropped facts are archived)",
    "memory/facts-archive.md": "facts dropped in consolidation are kept here; it is only appended to",
    "memory/digests.md": "use write_digest; digests are only appended to",
    "self/self.md": "use update_self_model",
    "self/personality.md": "use update_personality",
    "self/stats.md": "written by the supervisor from my measured results",
    "memory/commitments.jsonl": "use commit_to to make a commitment; my sleep settles them (settle_commitment)",
    "config.env": "change a value with the supervisor's set_config(key, value) — the only way to change this "
                  "file, so the supervisor can tell my settings from the operator's",
}
PROTECTED_DIRS = {".git": "git internals",
                  "secrets": "my keys are handled by the supervisor: secret_set / secret_list / secret_delete", "memory/episodes": "episodes are append-only records",
                  "logs": "logs are written by my runtime", "inbox": "the supervisor delivers messages here"}
# My records (the mind, beside self/'s own tools): write_file may not drop their lines unless I say replace.
RECORD_DIRS = ("notes", "memory", "self")
# The operator's folder I may read (shaft share <name> <folder>): seen as shared/, never written.
SHARED = "shared"
SHARED_READ_ONLY = "read-only: shared/ is the operator's folder — write what you make from it to notes/"
SHARED_NONE = "I have no shared folder now: the operator has not given me one, or has taken it back"


def _spec(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required if required is not None else list(properties)}}}


S = {"type": "string"}
BUILTINS = [
    _spec("read_file", "Read a text file inside my own folder (paths are relative to it), or under shared/ when "
          "the operator has given me a folder to read.",
          {"path": S, "offset": {"type": "integer", "description": "start character, default 0"}}, ["path"]),
    _spec("write_file", "Create or overwrite a text file inside my own folder. Use it for notes/, "
          "skills/, plugins/, self/goals.md and my own code. Code changes are tested by the "
          "supervisor after the run and reverted if tests fail. It replaces the whole file: to add an "
          "entry to a log or record, use append_file. A write that would drop lines of a record in notes/, "
          "memory/ or self/ is refused unless replace is true.",
          {"path": S, "content": S,
           "replace": {"type": "boolean", "description": "true only to replace a notes/, memory/ or self/ record on "
                       "purpose, dropping lines it holds now (default false)"}}, ["path", "content"]),
    _spec("append_file", "Add text to the end of a text file inside my own folder (created if missing) — "
          "the safe way to add a dated entry to a running record such as a notes/ log; nothing already "
          "in the file can be lost.", {"path": S, "content": S}),
    _spec("list_files", "List files under a directory of my folder (or of shared/, the operator's folder, when I "
          "have one).", {"path": S}, []),
    _spec("rewrite_facts", "Consolidate memory/facts.md (in my sleep): give the whole new list — duplicates "
          "merged, superseded or trivial facts dropped, what still matters kept — one per line as "
          "'- [YYYY-MM-DD HH:MM] fact' (keep each fact's date and '(src: …)'). At most 6,000 characters: "
          "it is shown to me in full. Facts I drop are kept in memory/facts-archive.md, still found by search_memory.", {"facts": S}),
    _spec("write_digest", "Add today's digest (in my sleep): a short summary, at most 1,500 characters, of "
          "what happened since my last sleep and what it means — what my future self needs once these "
          "journal entries are no longer in view. Kept in memory/digests.md; recent digests are shown to me.",
          {"text": S}),
    _spec("search_memory", "Search everything I remember (journal, facts, notes, conversations) "
          "and return the most relevant passages with their dates.",
          {"query": S, "k": {"type": "integer", "description": "max results, default 5"}}, ["query"]),
    _spec("remember", "Store a durable fact in memory/facts.md with its source: log:<episode ts>[#step], "
          "web:<url> seen in this run, operator, instance:<name>, or inferred.",
          {"fact": S, "source": S}),
    _spec("update_self_model", "Replace my self-model (self/self.md) with a complete new version "
          "starting with '# '. Include everything I want to keep.", {"text": S}),
    _spec("update_personality", "Rewrite one layer of self/personality.md. 'surface' = current "
          "interests, opinions, moods (changes freely). 'core' = values and temperament (changes "
          "rarely; the supervisor refuses core changes made too soon after the last one). Every "
          "bullet must cite the journal entry that supports it, like [2026-10-12 14:03].",
          {"layer": {"type": "string", "enum": ["core", "surface"]}, "text": S}),
]


# Shown only when my model sees images (SHAFT_VISION, PLAN-vision.md): the image follows the tool results.
VIEW_IMAGE = _spec("view_image", "Look at an image file (JPEG, PNG, GIF or WebP) in my folder — a photo the operator "
                   "attached (attachments/…), a mail attachment (mail/…), a file in shared/. The image is shown to "
                   "me right after the tool results.", {"path": S})


WEB_READS = ("fetch_page", "http_request", "run")   # the tools whose successful result is a page I saw (remember web:)


class Toolbox:
    def __init__(self, root: Path, extra: list[dict] | None = None):
        self.root = Path(root).resolve()
        shared = os.environ.get("SHAFT_SHARED")
        self.shared = Path(shared).resolve() if shared else None
        self.extra = extra or []          # control tools owned by the caller (e.g. finish_cycle)
        self.plugins = self._discover_plugins()
        self.remote = self._discover_broker()
        self.log: list[dict] = []
        self.shown: list[Path] = []       # images view_image showed, for the agent loop to send after the results
        self.commit_source = ""           # "cycle" or "talk" where commit_to is offered (COMMITMENTS on), else ""

    # -- discovery ---------------------------------------------------------------
    def _discover_plugins(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        base = self.root / "plugins"
        taken = {t["function"]["name"] for t in BUILTINS}
        for manifest in sorted(base.glob("*/plugin.json")) if base.is_dir() else []:
            try:
                spec = json.loads(manifest.read_text())
                name = spec["name"]
                if name in taken or name != manifest.parent.name:
                    continue
                found[name] = _spec(name, spec["description"], spec.get("parameters", {}).get("properties", {}),
                                    spec.get("parameters", {}).get("required", []))
            except (ValueError, KeyError):
                continue
        return found

    def _discover_broker(self) -> dict[str, dict]:
        if not broker.available():
            return {}
        try:
            return {t["function"]["name"]: t for t in broker.tools()}
        except broker.BrokerError:
            return {}

    def _builtins(self) -> list[dict]:
        from . import commitments   # it builds its spec with _spec, from this module
        return (BUILTINS + ([VIEW_IMAGE] if images.vision() else [])
                + ([commitments.COMMIT_TO] if self.commit_source else []))

    def specs(self) -> list[dict]:
        return self._builtins() + list(self.plugins.values()) + list(self.remote.values()) + self.extra

    def describe(self) -> str:
        groups = [("built-in", [t["function"]["name"] for t in self._builtins()]),
                  ("my plugins", list(self.plugins)), ("supervisor", list(self.remote))]
        return "; ".join(f"{label}: {', '.join(names) or '(none)'}" for label, names in groups)

    # -- execution ---------------------------------------------------------------
    def run(self, name: str, raw_args: str) -> str:
        started = datetime.now()
        try:
            args = json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
            result = self._dispatch(name, args)
        except Exception as e:  # a tool failure must never kill the cycle
            result = f"ERROR: {type(e).__name__}: {e}"
        if len(result) > MAX_RESULT_CHARS:
            result = result[:MAX_RESULT_CHARS] + f"\n[... truncated, {len(result)} chars total]"
        self.log.append({"tool": name, "args": for_log(_loggable(name, raw_args)), "result": result,   # what I saw
                         "seconds": round((datetime.now() - started).total_seconds(), 2)})
        return result

    def _dispatch(self, name: str, args: dict) -> str:
        builtin = getattr(self, f"_t_{name}", None)
        if builtin and name in {t["function"]["name"] for t in self._builtins()}:
            return builtin(**args)
        if name in self.plugins:
            return self._run_plugin(name, args)
        if name in self.remote:
            return broker.call(name, **args)
        return f"ERROR: unknown tool {name!r}. Available: {self.describe()}"

    def _path(self, rel: str, for_write: bool = False) -> Path:
        parts = Path(rel).parts
        if parts and parts[0] == SHARED and not self.shared and not (self.root / SHARED).exists():
            # Not an empty folder or a missing file: say there is none (with the grant just taken back, "(empty)"
            # and "No such file" left me checking the same path again and again).
            raise PermissionError(SHARED_NONE)
        if self.shared and parts and parts[0] == SHARED:
            if for_write:
                raise PermissionError(SHARED_READ_ONLY)
            path = self.shared.joinpath(*parts[1:]).resolve()
            if path != self.shared and self.shared not in path.parents:
                raise PermissionError(f"{rel!r} is outside the shared folder")
            return path
        path = (self.root / rel).resolve()
        if path != self.root and self.root not in path.parents:
            raise PermissionError(f"{rel!r} is outside my folder")
        if path == self.root / "secrets" or self.root / "secrets" in path.parents:   # not even reading
            raise PermissionError(f"{rel}: {PROTECTED_DIRS['secrets']}")
        if for_write:
            r = path.relative_to(self.root).as_posix()
            if r in PROTECTED:
                raise PermissionError(f"{r}: {PROTECTED[r]}")
            for d, why in PROTECTED_DIRS.items():
                if r == d or r.startswith(d + "/"):
                    raise PermissionError(f"{r}: {why}")
        return path

    def _t_view_image(self, path: str) -> str:
        target = self._path(path)
        if not images.is_image(target):
            raise ValueError(f"{path}: not a JPEG, PNG, GIF or WebP file")
        if not target.is_file():
            raise FileNotFoundError(f"{path}: no such file")
        if len(self.shown) >= images.MAX_SHOWN:
            raise ValueError(f"at most {images.MAX_SHOWN} images per step; look at the rest next step")
        self.shown.append(target)
        return f"{path}: shown below ({target.stat().st_size // 1024} KB)."

    def _t_read_file(self, path: str, offset: int = 0) -> str:
        text = self._path(path).read_text(errors="replace")
        return text[offset:offset + MAX_RESULT_CHARS] or "(empty)"

    def _t_write_file(self, path: str, content: str, replace: bool = False) -> str:
        target = self._path(path, for_write=True)
        old = target.read_text(errors="replace").splitlines() if target.is_file() else []
        kept = set(content.splitlines())
        gone = [l for l in old if l.strip() and l not in kept]
        record = target.relative_to(self.root).parts[0] in RECORD_DIRS
        if gone and record and replace is not True and str(replace).lower() != "true":
            # 2026-10-16 03:59: I meant to add an audit entry and wrote it with write_file over my 546 KB trend
            # record; the report after the write said 2,067 lines were gone, but they already were. Refused first.
            return (f"ERROR: not written. {path} has {len(old)} lines and this write would drop {len(gone)} of "
                    f"them, e.g. {gone[0][:120]!r}. To add an entry, use append_file. To replace this record on "
                    f"purpose, call write_file again with replace=true.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        if not gone:
            return f"wrote {len(content)} chars to {path}"
        # I once rewrote a running record and silently dropped its earlier entries: say what went — and say
        # first that the write is complete ("N lines are gone" alone read as "only N lines were replaced").
        return (f"wrote {len(content)} chars to {path}: the file now contains exactly what you wrote "
                f"({len(content.splitlines())} lines). Compared with before, {len(gone)} earlier line(s) are no "
                f"longer in it, e.g. {gone[0][:120]!r}. If that was not intended, the old version is in "
                f"history(path={path!r}): history(commit=<id>, path={path!r}, save_as=<new path>) writes it back "
                f"whole for you to check and put in place; append_file adds without replacing.")

    def _t_append_file(self, path: str, content: str) -> str:
        target = self._path(path, for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = target.read_text(errors="replace") if target.is_file() else ""
        sep = "" if not before or before.endswith("\n") else "\n"
        with target.open("a") as f:
            f.write(sep + content + ("" if content.endswith("\n") else "\n"))
        return f"appended {len(content)} chars to {path}"

    def _t_list_files(self, path: str = ".") -> str:
        base = self._path(path)
        in_shared = bool(self.shared) and (base == self.shared or self.shared in base.parents)
        top, prefix = (self.shared, SHARED + "/") if in_shared else (self.root, "")
        skip = {".git", "__pycache__", ".tmp", "logs", "secrets"}
        # Tens of thousands of files (an environment, a browser profile's caches): listed, not walked.
        bulky = {".venv", ".uv", ".browser"} - set(base.relative_to(top).parts)
        rows = []
        for here, dirs, files in os.walk(base) if not skip & set(base.relative_to(top).parts) else ():
            rel = Path(here).relative_to(top)
            dirs[:] = sorted(d for d in dirs if d not in skip)
            for d in [d for d in dirs if d in bulky]:
                dirs.remove(d)
                rows.append(prefix + (rel / d).as_posix() + "/ (not listed: list_files " + (rel / d).as_posix() + ")")
            rows += [prefix + (rel / d).as_posix() + "/" for d in dirs]
            rows += [prefix + (rel / f).as_posix() for f in sorted(files)]
        rows.sort()
        if self.shared and base == self.root:
            rows.append(f"{SHARED}/ (the operator's folder, read-only: list_files {SHARED})")
        return "\n".join(rows[:400]) or "(empty)"

    def _t_search_memory(self, query: str, k: int = 5) -> str:
        hits = memory.search(self.root, query, k=k)
        # Say how it searched every time, not only when meaning search fails: when it worked and said nothing, I
        # used it daily and still wrote in my self-model that I had "no embeddings".
        note = f"(searched by {memory.mode})\n\n" if memory.mode else ""
        return note + ("\n\n".join(f"[{h.source}] {h.text}" for h in hits) or "nothing relevant found")

    def _remember_source(self, source: str) -> bool:
        if source in ("operator", "inferred") or re.fullmatch(r"instance:[a-z0-9][a-z0-9_-]*", source):
            return True
        if source.startswith("web:"):
            # An observation only if this run fetched that very URL and the fetch worked: a URL mentioned in an
            # argument, a search snippet or a failed request is not something I saw (Codex review, 2026-10-26).
            url = source[4:]
            if not re.fullmatch(r"https?://[^\s/]+(?:/[^\s]*)?", url):
                return False
            normalize = lambda u: re.sub(r"^https?://", "", u).rstrip("/")
            for entry in self.log:
                result = str(entry.get("result", ""))
                if entry.get("tool") not in WEB_READS or result.startswith("ERROR") or re.match(r"HTTP [45]\d\d", result):
                    continue
                if entry["tool"] == "run" and not result.startswith("exit 0"):
                    continue
                try:
                    args = json.loads(entry.get("args") or "{}") or {}
                    fetched = [str(args.get("url", ""))] if entry["tool"] != "run" else \
                        re.findall(r"https?://[^\s\"'<>]+", str(args.get("command", "")))
                except (ValueError, AttributeError):
                    continue
                if any(f and normalize(f) == normalize(url) for f in fetched):
                    return True
            return False
        if not source.startswith("log:"):
            return False
        stamp, sep, step = source[4:].partition("#")
        try:
            datetime.fromisoformat(stamp)
        except ValueError:
            return False
        if not re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", stamp) or (
                sep and not re.fullmatch(r"[1-9][0-9]*", step)):
            return False
        for path in sorted((self.root / "memory" / "episodes").glob("*.jsonl")):
            for line in path.read_text(errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = rec.get("ts", "") if isinstance(rec, dict) else ""
                if isinstance(ts, str) and (ts.replace("T", " ") == stamp.replace("T", " ") or
                        len(stamp) == 16 and ts[:16].replace("T", " ") == stamp.replace("T", " ")):
                    # Episodes record calls in order, without model-turn numbers: #1 is the first call.
                    if not sep or int(step) <= len(rec.get("tools") or []):
                        return True
        return False

    def _t_remember(self, fact: str, source: str = "") -> str:
        if not isinstance(source, str) or not self._remember_source(source):
            return ("ERROR: source must be log:<episode ts>[#step], web:<url>, operator, "
                    "instance:<name> ([a-z0-9][a-z0-9_-]*), or inferred; "
                    "if you did not observe it in this run's tool results or your record, use source=inferred")
        facts = self.root / "memory" / "facts.md"
        facts.parent.mkdir(parents=True, exist_ok=True)
        if not facts.exists():
            facts.write_text("# Facts I have learned\n\n")
        with facts.open("a") as f:
            f.write(f"- [{clock.now():%Y-%m-%d %H:%M}] {fact.strip()} (src: {source})\n")
        return "remembered"

    def _t_rewrite_facts(self, facts: str) -> str:
        lines = [l.strip() for l in facts.strip().splitlines() if l.strip()]
        bad = [l for l in lines if not FACT_LINE.match(l)]
        if not lines or bad:
            return (f"ERROR: write one fact per line as '- [YYYY-MM-DD HH:MM] fact' (keeping its date); "
                    f"not like that: {bad[0][:120]!r}" if bad else "ERROR: no facts given")
        # An observation source is attached only by remember, which checks it against my record: a consolidation may
        # keep or drop one, never invent one (Grok's review: rewrite_facts let any (src: web:…) through).
        known = set()
        for name in ("facts.md", "facts-archive.md"):
            path = self.root / "memory" / name
            known |= set(SOURCE.findall(path.read_text(errors="replace"))) if path.exists() else set()
        new = sorted({src for l in lines for src in SOURCE.findall(l)
                      if src.startswith(("web:", "log:")) and src not in known})
        if new:
            return (f"ERROR: {new[0]!r} is on no fact I hold: an observation source is added with remember, which "
                    f"checks it; a consolidation keeps the sources it merges (or says inferred)")
        text = "\n".join(lines)
        if len(text) > FACTS_MAX_CHARS:
            return f"ERROR: {len(text)} characters; consolidate to at most {FACTS_MAX_CHARS} (merge or drop more)"
        path = self.root / "memory" / "facts.md"
        old = [l.strip() for l in path.read_text().splitlines() if l.strip().startswith("- ")] if path.exists() else []
        dropped = [l for l in old if l not in set(lines)]
        if dropped:
            with (self.root / "memory" / "facts-archive.md").open("a") as f:
                f.write(f"\n## Dropped in consolidation, {clock.now():%Y-%m-%d %H:%M}\n\n" + "\n".join(dropped) + "\n")
        path.write_text("# Facts I have learned\n\n" + text + "\n")
        return f"facts consolidated: {len(old)} -> {len(lines)} ({len(dropped)} archived in memory/facts-archive.md)"

    def _t_write_digest(self, text: str) -> str:
        text = text.strip()
        if not text:
            return "ERROR: empty digest"
        if len(text) > DIGEST_MAX_CHARS:
            return f"ERROR: {len(text)} characters; a digest is at most {DIGEST_MAX_CHARS}"
        with (self.root / "memory" / "digests.md").open("a") as f:
            f.write(f"\n## {clock.now():%Y-%m-%d %H:%M} — digest\n\n{text}\n")
        return "digest written"

    def _t_update_self_model(self, text: str) -> str:
        edit = selfedit.parse(f"<self_model>\n{text}\n</self_model>")
        if edit.new_self is None:
            return f"ERROR: self-model not changed: {edit.rejected or 'empty or no-change text'}"
        path = self.root / "self" / "self.md"
        path.write_text(edit.new_self + "\n")
        return "self-model updated"

    def _t_commit_to(self, promise: str, due: str, criterion: str, made_to: str) -> str:
        from . import commitments   # an OSError here is a tool failure like any other: run() reports it
        return commitments.add(self.root, {"promise": promise, "due": due, "criterion": criterion, "made_to": made_to},
                               self.commit_source, clock.now())

    def _t_update_personality(self, layer: str, text: str) -> str:
        return personality.update(self.root, layer, text)

    def _run_plugin(self, name: str, args: dict) -> str:
        # SHAFT_PYTHON: my own environment's interpreter (.venv/), set by the supervisor; I run on the system one.
        python = os.environ.get("SHAFT_PYTHON") or sys.executable
        proc = subprocess.run([python, "-E", "-s", "-m", "shaft.plugin_runner", name],
                              input=json.dumps(args), capture_output=True, text=True,
                              cwd=self.root, timeout=PLUGIN_TIMEOUT_S)
        if proc.returncode != 0:
            return f"ERROR: plugin {name} failed (exit {proc.returncode}): {proc.stderr[-1500:]}"
        return proc.stdout


def for_log(text: str) -> str:
    """Text for my tool log: whole up to LOG_CHARS, else cut there with a note saying so and how long it was."""
    return text if len(text) <= LOG_CHARS else text[:LOG_CHARS] + f"\n[... cut in the log, {len(text)} chars total]"


def _loggable(tool: str, raw_args: str) -> str:
    """The tool log goes into my episodes (and from there into my context): a key's value never does."""
    if tool != "secret_set":
        return raw_args
    try:
        args = json.loads(raw_args or "{}")
        return json.dumps({**args, "value": "[withheld]"} if isinstance(args, dict) else {}, ensure_ascii=False)
    except ValueError:
        return '{"value": "[withheld]"}'
