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
import tempfile
from datetime import datetime
from pathlib import Path

from . import broker, clock, images, memory, personality, selfedit

MAX_RESULT_CHARS = 6000
MAX_OUTPUT_CHARS = 1_000_000
MAX_OUTPUT_FILES = 32
# My tool log is what the judge grades my cycles from: it keeps what I saw. It was once cut at 2,000
# characters while I saw 6,000, and the judge marked my read-backs of self/self.md "cut off" — unsupported.
LOG_CHARS = MAX_RESULT_CHARS
READ_MARKER_ROOM = 400     # what a cut read_file leaves for its continuation marker (path included)
FACTS_MAX_CHARS = 6000     # facts.md is shown in full: consolidation keeps it to this
DIGEST_MAX_CHARS = 1500
SOURCE = re.compile(r" \(src: (.+?)\)(?=\s*$| \(superseded by \[)", re.M)   # a fact line's source, as remember writes it (a URL may hold ")")
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
    "drive.md": "only the operator edits my drive; propose_drive_amendment during sleep",
    "memory/projects.jsonl": "use open_project and close_project",
    "memory/corrections.jsonl": "use record_correction at conversation writeup; close_correction in sleep",
    "memory/commitments.jsonl": "use commit_to for promises to the operator or another agent; "
                                "own plans go in goals/notes; sleep settles them",
    "config.env": "change a value with the supervisor's set_config(key, value) — the only way to change this "
                  "file, so the supervisor can tell my settings from the operator's",
}
PROTECTED_DIRS = {"notes/drive-proposals": "use propose_drive_amendment during sleep; the operator writes decisions",
                  ".git": "git internals",
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


def coerce_args(args: dict, spec: dict | None) -> dict:
    """Arguments typed as the tool's schema declares them. Some models send every value as a string: on mimo
    (2026-10-28..30) each record_correction carried reopens "null" — not JSON null — and every one of 44 calls was
    refused, so the corrections ledger got no row; read_file's offset and finish_cycle's step came as "120" and "11".
    Only a string whose declared types rule out that string is converted; anything else is left for the tool to
    judge."""
    props = (((spec or {}).get("function") or {}).get("parameters") or {}).get("properties") or {}
    out = dict(args)
    for key, value in args.items():
        declared = props.get(key) if isinstance(props, dict) else None
        if not isinstance(value, str) or not isinstance(declared, dict):
            continue
        kinds = declared.get("type")
        kinds = [kinds] if isinstance(kinds, str) else list(kinds) if isinstance(kinds, list) else []
        text = value.strip()
        if "null" in kinds and text.lower() in ("null", "none"):
            out[key] = None
        elif "string" in kinds:
            continue
        elif "integer" in kinds and re.fullmatch(r"[+-]?\d+", text):
            out[key] = int(text)
        elif "number" in kinds and re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", text):
            out[key] = float(text)
        elif "boolean" in kinds and text.lower() in ("true", "false"):
            out[key] = text.lower() == "true"
    return out


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
          "merged, trivial facts dropped, what still matters kept; retire contradictions with "
          "remember(supersedes=...) or sleep-only retire_fact — one per line as "
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
          "web:<url> seen in this run, operator, instance:<name>, or inferred. Optional supersedes names "
          "one current fact by its date stamp and optional opening words; it is archived with both dates.",
          {"fact": S, "source": S, "supersedes": S}, ["fact", "source"]),
    _spec("update_self_model", "Replace my self-model (self/self.md) with a complete new version "
          "starting with '# '. Include everything I want to keep. For operator claims supply source=operator and "
          "checked_with=log:<episode ts> or record:<JSON arguments>/history:<JSON arguments> from a successful "
          "call in this run, or mark each new line as operator's hypothesis, not verified (YYYY-MM-DD).",
          {"text": S, "source": S, "checked_with": S}, ["text"]),
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

# Handoff evidence (#104), from BUILTINS and the supervisor's broker registry. Plugins and shell runs are opaque:
# record the successful invocation, never infer that the work claimed in its prose happened.
STATE_CHANGING = frozenset({
    "write_file", "append_file", "rewrite_facts", "write_digest", "remember", "update_self_model",
    "update_personality", "commit_to", "settle_commitment", "close_surprise", "send_message", "run",
    "secret_set", "secret_delete", "delegate_coding", "set_config", "trial", "mail_attachment", "drive_download",
    "calendar_create", "calendar_move", "calendar_respond", "withdraw_held",
})


def completed(entries: list[dict], plugins: dict) -> list[str]:
    """At most eight successful state-changing calls, with bounded argument summaries from the log alone."""
    from . import surprises   # imported here: surprises reads episodes, which this module's callers write
    out = []
    for entry in entries:
        name, result = entry.get("tool"), entry.get("result", "")
        if not isinstance(result, str) or result.startswith("(held, not run yet)") or (
                (surprises.flag(result) or ("",))[0] in ("error", "refused", "nonzero_exit", "stderr_on_success",
                                                         "http_error")):
            continue   # the same failure shapes the surprise rule counts: a failed write did not happen
        raw = str(entry.get("args") or "{}")
        try:
            args = json.loads(raw)
        except ValueError:
            args = {}   # for_log may have cut the arguments; ordinary writers are still evidence
        if not isinstance(args, dict):
            args = {}
        changes = name in STATE_CHANGING or name in plugins
        if name == "http_request":
            changes = str(args.get("method", "GET")).upper() not in ("GET", "HEAD", "OPTIONS") or bool(args.get("save_to"))
        elif name == "history":
            changes = bool(args.get("save_as"))
        if not changes:
            continue
        summary = (json.dumps({k: str(v)[:80] for k, v in args.items()}, ensure_ascii=False) if args else raw)
        out.append(f"{name}: {' '.join(summary.split())[:240]}")
        if len(out) == 8:
            break
    return out


class Toolbox:
    def __init__(self, root: Path, extra: list[dict] | None = None):
        self.root = Path(root).resolve()
        shared = os.environ.get("SHAFT_SHARED")
        self.shared = Path(shared).resolve() if shared else None
        self.extra = extra or []          # control tools owned by the caller (e.g. finish_cycle)
        # What discovery could not load, with why: shown in the tool list (describe) instead of dropped silently —
        # a malformed manifest or a broker error once left a tool missing with no word of it (2026-10-31).
        self.problems: list[str] = []
        self.plugins = self._discover_plugins()
        self.remote = self._discover_broker()
        self.log: list[dict] = []
        self.shown: list[Path] = []       # images view_image showed, for the agent loop to send after the results
        self.commit_source = ""           # "cycle" or "talk" where commit_to is offered (COMMITMENTS on), else ""
        self.commit_talk_id = ""   # the conversation open now: what makes a promise there one to the operator

    # -- discovery ---------------------------------------------------------------
    def _discover_plugins(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        base = self.root / "plugins"
        taken = {t["function"]["name"] for t in BUILTINS}
        for manifest in sorted(base.glob("*/plugin.json")) if base.is_dir() else []:
            where = f"plugins/{manifest.parent.name}"
            try:
                spec = json.loads(manifest.read_text())
                if not isinstance(spec, dict):
                    raise ValueError("plugin.json must be a JSON object")
                name = spec["name"]
                if name in taken:
                    raise ValueError(f"name {name!r} is taken by a built-in tool")
                if name != manifest.parent.name:
                    raise ValueError(f"name {name!r} differs from its folder {manifest.parent.name!r}")
                parameters = spec.get("parameters", {})
                if not isinstance(parameters, dict):
                    raise ValueError("parameters must be a JSON object")
                found[name] = _spec(name, spec["description"], parameters.get("properties", {}),
                                    parameters.get("required", []))
            except KeyError as e:
                self.problems.append(f"{where} (plugin.json has no {e.args[0]!r})")
            except Exception as e:   # never a crash of the run, never silence
                self.problems.append(f"{where} ({type(e).__name__}: {e})"[:300])
        return found

    def _discover_broker(self) -> dict[str, dict]:
        if not broker.available():
            return {}
        try:
            return {t["function"]["name"]: t for t in broker.tools()}
        except Exception as e:
            self.problems.append(f"the supervisor's tools ({type(e).__name__}: {e})"[:300])
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
        if self.extra:
            groups.append(("extra", [t["function"]["name"] for t in self.extra]))
        return "; ".join(f"{label}: {', '.join(names) or '(none)'}" for label, names in groups) + self.not_loaded()

    def not_loaded(self) -> str:
        return f"; not loaded: {'; '.join(self.problems)}" if self.problems else ""

    def spec_of(self, name: str) -> dict | None:
        return next((s for s in self.specs() if s["function"]["name"] == name), None)

    # -- execution ---------------------------------------------------------------
    def run(self, name: str, raw_args: str) -> str:
        started = datetime.now()
        try:
            args = json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
            result = self._dispatch(name, coerce_args(args, self.spec_of(name)))
        except Exception as e:  # a tool failure must never kill the cycle
            result = f"ERROR: {type(e).__name__}: {e}"
        if len(result) > MAX_RESULT_CHARS:
            total = len(result)
            try:
                folder = self._path(".tmp/outputs", for_write=True)
                folder.mkdir(parents=True, exist_ok=True)
                old = sorted(folder.glob("output-*.txt"), key=lambda p: p.lstat().st_mtime)
                for index, path in enumerate(old):
                    if index > len(old) - MAX_OUTPUT_FILES and datetime.now().timestamp() - path.lstat().st_mtime < 86400:
                        continue
                    path.unlink()
                # errors="replace": a broker result decoded from JSON may hold a lone surrogate, which strict
                # UTF-8 refuses (one character stays one character, so the offsets below still line up).
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", errors="replace", prefix="output-",
                                                 suffix=".txt", dir=folder, delete=False) as saved:
                    saved.write(result[:MAX_OUTPUT_CHARS])
                rel = Path(saved.name).relative_to(self.root).as_posix()
                cap = f"; storage cap: first {MAX_OUTPUT_CHARS} chars saved" if total > MAX_OUTPUT_CHARS else ""
                marker = (f"\n[... truncated, {total} chars total{cap}; saved to {rel}. "
                          f"Read the rest with read_file({rel!r}, offset={MAX_RESULT_CHARS - READ_MARKER_ROOM})]")
            except Exception as e:   # outside the dispatch's own catch: a failed save must not kill the cycle
                marker = f"\n[... truncated, {total} chars total; could not save output: {type(e).__name__}: {str(e)[:200]}]"
            result = result[:MAX_RESULT_CHARS - READ_MARKER_ROOM] + marker
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
        """A cut read says how much is left and the call that reads on: a plain cut at 6,000 characters read as
        the whole file (2026-10-31). The marker fits within MAX_RESULT_CHARS, so run() never cuts it off."""
        text = self._path(path).read_text(errors="replace")
        offset = int(offset)
        offset = max(0, len(text) + offset if offset < 0 else offset)   # a negative offset reads the tail
        if len(text) - offset <= MAX_RESULT_CHARS:
            return text[offset:] or "(empty)"
        end = offset + MAX_RESULT_CHARS - READ_MARKER_ROOM
        return (text[offset:end] + f"\n[… {len(text) - end} more characters; read_file({path!r}, offset={end})]")

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

    def _t_remember(self, fact: str, source: str = "", supersedes: str = "") -> str:
        if not isinstance(source, str) or not self._remember_source(source):
            return ("ERROR: source must be log:<episode ts>[#step], web:<url>, operator, "
                    "instance:<name> ([a-z0-9][a-z0-9_-]*), or inferred; "
                    "if you did not observe it in this run's tool results or your record, use source=inferred")
        facts = self.root / "memory" / "facts.md"
        old = self._fact_match(supersedes) if supersedes else ""
        stamp = f"{clock.now():%Y-%m-%d %H:%M}"
        suffix = f" (supersedes: [{old.split(']')[0][3:]}])" if old else ""
        new = f"- [{stamp}] {fact.strip()}{suffix} (src: {source})"
        if old:
            self._retire_fact(old, f"superseded by [{stamp}]: {source}: {' '.join(fact.split())[:120]}", new)
            return f"remembered; retired {old} (superseded by [{stamp}])"
        facts.parent.mkdir(parents=True, exist_ok=True)
        if not facts.exists():
            facts.write_text("# Facts I have learned\n\n")
        with facts.open("a") as f:
            f.write(new + "\n")
        return "remembered"

    def _fact_match(self, fact: str) -> str:
        path = self.root / "memory" / "facts.md"
        lines = [line for line in path.read_text().splitlines() if FACT_LINE.match(line)] if path.exists() else []
        key = fact.strip().removeprefix("- ").removeprefix("[").replace("]", "", 1)
        selector = re.fullmatch(r"(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)(?: (.+))?", key)
        matches = [line for line in lines if selector and line.startswith(f"- [{selector[1]}] ")
                   and line.split("] ", 1)[1].startswith(selector[2] or "")]
        if len(matches) != 1:
            candidates = matches or lines
            raise ValueError(f"fact {fact!r} matched {len(matches)} current facts; candidates: "
                             + ("; ".join(candidates) or "none"))
        return matches[0]

    def _retire_fact(self, old: str, evidence: str, new: str = "") -> str:
        path = self.root / "memory" / "facts.md"
        lines = path.read_text().splitlines()
        lines.remove(old)
        with (path.parent / "facts-archive.md").open("a") as f:
            f.write(f"\n## Retired (superseded) {clock.now():%Y-%m-%d %H:%M}\n\n{old} ({evidence})\n")
        if new:
            lines.append(new)
        path.write_text("\n".join(lines) + "\n")
        return f"retired {old} ({evidence})"

    def _t_retire_fact(self, fact: str, evidence: str) -> str:
        old = self._fact_match(fact)
        source = evidence.strip()
        at = old.split("]", 1)[0][3:]
        if source.startswith("log:") and self._remember_source(source):
            newer = datetime.fromisoformat(source[4:].partition("#")[0])
        elif source == "operator" or source.startswith("web:") and self._remember_source(source):
            newer = clock.now()
        else:
            try:
                observation = self._fact_match(source.removeprefix("fact:"))
                newer = datetime.fromisoformat(observation.split("]", 1)[0][3:])
            except ValueError:
                return "ERROR: evidence must cite a checked log:<episode ts>[#step], newer fact's date, web:<url>, or operator"
        if newer.astimezone().replace(tzinfo=None) <= datetime.fromisoformat(at):
            return f"ERROR: evidence {evidence!r} must be newer than [{at}]"
        return self._retire_fact(old, f"superseded by [{newer:%Y-%m-%d %H:%M}]: {source}")

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

    def _t_update_self_model(self, text: str, source: str = "", checked_with: str = "") -> str:
        # #139: only explicit operator attribution is gated; existing callers still work.
        if source == "operator":
            checked = checked_with.startswith("log:") and self._remember_source(checked_with)
            name, _, raw = checked_with.partition(":")
            if name in ("record", "history"):
                try:
                    args = json.loads(raw)
                    checked = any(e["tool"] == name and json.loads(e["args"]) == args
                                  and not e["result"].startswith("ERROR")
                                  and e["result"] != "(repeat: not run)" for e in self.log)
                except (ValueError, TypeError):
                    checked = False
            path = self.root / "self" / "self.md"
            held = set(path.read_text().splitlines()) if path.exists() else set()
            new = [line for line in text.splitlines() if line.strip() and not line.startswith("#") and line not in held]
            marked = all(re.search(r"operator's hypothesis, not verified \(\d{4}-\d{2}-\d{2}\)", line)
                         for line in new)
            if not checked and not marked:
                return "ERROR: operator claim needs checked_with from record/history or an episode, or a dated hypothesis marking"
        edit = selfedit.parse(f"<self_model>\n{text}\n</self_model>")
        if edit.new_self is None:
            return f"ERROR: self-model not changed: {edit.rejected or 'empty or no-change text'}"
        path = self.root / "self" / "self.md"
        path.write_text(edit.new_self + "\n")
        return "self-model updated"

    def _t_commit_to(self, promise: str, due: str, criterion: str, made_to: str) -> str:
        from . import commitments   # an OSError here is a tool failure like any other: run() reports it
        return commitments.add(self.root, {"promise": promise, "due": due, "criterion": criterion, "made_to": made_to,
                                           "talk_id": self.commit_talk_id, **commitments.message_source(self.log)},
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
