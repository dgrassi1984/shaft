"""My tools: built-ins, plugins I (or my ancestors) wrote, and the supervisor's broker tools.

Every tool takes JSON arguments and returns text. A failing tool returns
"ERROR: ..." so I can adapt, instead of crashing the cycle.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import broker, memory, personality, selfedit

MAX_RESULT_CHARS = 12000
PLUGIN_TIMEOUT_S = 120

# Paths I may not overwrite with write_file: append-only history, files with their own
# validated tools, and files the supervisor writes about me.
PROTECTED = {
    "memory/journal.md": "the journal is append-only; it is written at the end of each cycle",
    "self/self.md": "use update_self_model",
    "self/personality.md": "use update_personality",
    "self/stats.md": "written by the supervisor from my measured results",
}
PROTECTED_DIRS = {".git": "git internals", "memory/episodes": "episodes are append-only records",
                  "logs": "logs are written by my runtime", "inbox": "the supervisor delivers messages here"}


def _spec(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required if required is not None else list(properties)}}}


S = {"type": "string"}
BUILTINS = [
    _spec("read_file", "Read a text file inside my own folder (paths are relative to it).",
          {"path": S, "offset": {"type": "integer", "description": "start character, default 0"}}, ["path"]),
    _spec("write_file", "Create or overwrite a text file inside my own folder. Use it for notes/, "
          "skills/, plugins/, self/goals.md and my own code. Code changes are tested by the "
          "supervisor after the run and reverted if tests fail.", {"path": S, "content": S}),
    _spec("list_files", "List files under a directory of my folder.", {"path": S}, []),
    _spec("search_memory", "Search everything I remember (journal, facts, notes, conversations) "
          "and return the most relevant passages with their dates.",
          {"query": S, "k": {"type": "integer", "description": "max results, default 5"}}, ["query"]),
    _spec("remember", "Store a durable fact in memory/facts.md (about the world, Daniele, or myself).",
          {"fact": S}),
    _spec("update_self_model", "Replace my self-model (self/self.md) with a complete new version "
          "starting with '# '. Include everything I want to keep.", {"text": S}),
    _spec("update_personality", "Rewrite one layer of self/personality.md. 'surface' = current "
          "interests, opinions, moods (changes freely). 'core' = values and temperament (changes "
          "rarely; the supervisor refuses core changes made too soon after the last one). Every "
          "bullet must cite the journal entry that supports it, like [2026-10-12 14:03].",
          {"layer": {"type": "string", "enum": ["core", "surface"]}, "text": S}),
]


class Toolbox:
    def __init__(self, root: Path, extra: list[dict] | None = None):
        self.root = Path(root).resolve()
        self.extra = extra or []          # control tools owned by the caller (e.g. finish_cycle)
        self.plugins = self._discover_plugins()
        self.remote = self._discover_broker()
        self.log: list[dict] = []

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

    def specs(self) -> list[dict]:
        return BUILTINS + list(self.plugins.values()) + list(self.remote.values()) + self.extra

    def describe(self) -> str:
        groups = [("built-in", [t["function"]["name"] for t in BUILTINS]),
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
        self.log.append({"tool": name, "args": raw_args[:2000], "result": result[:2000],
                         "seconds": round((datetime.now() - started).total_seconds(), 2)})
        return result

    def _dispatch(self, name: str, args: dict) -> str:
        builtin = getattr(self, f"_t_{name}", None)
        if builtin and name in {t["function"]["name"] for t in BUILTINS}:
            return builtin(**args)
        if name in self.plugins:
            return self._run_plugin(name, args)
        if name in self.remote:
            return broker.call(name, **args)
        return f"ERROR: unknown tool {name!r}. Available: {self.describe()}"

    def _path(self, rel: str, for_write: bool = False) -> Path:
        path = (self.root / rel).resolve()
        if path != self.root and self.root not in path.parents:
            raise PermissionError(f"{rel!r} is outside my folder")
        if for_write:
            r = path.relative_to(self.root).as_posix()
            if r in PROTECTED:
                raise PermissionError(f"{r}: {PROTECTED[r]}")
            for d, why in PROTECTED_DIRS.items():
                if r == d or r.startswith(d + "/"):
                    raise PermissionError(f"{r}: {why}")
        return path

    def _t_read_file(self, path: str, offset: int = 0) -> str:
        text = self._path(path).read_text(errors="replace")
        return text[offset:offset + MAX_RESULT_CHARS] or "(empty)"

    def _t_write_file(self, path: str, content: str) -> str:
        target = self._path(path, for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {len(content)} chars to {path}"

    def _t_list_files(self, path: str = ".") -> str:
        base = self._path(path)
        skip = {".git", "__pycache__", ".tmp", "logs"}
        rows = [p.relative_to(self.root).as_posix() + ("/" if p.is_dir() else "")
                for p in sorted(base.rglob("*")) if not skip & set(p.relative_to(self.root).parts)]
        return "\n".join(rows[:400]) or "(empty)"

    def _t_search_memory(self, query: str, k: int = 5) -> str:
        hits = memory.search(self.root, query, k=k)
        return "\n\n".join(f"[{h.source}] {h.text}" for h in hits) or "nothing relevant found"

    def _t_remember(self, fact: str) -> str:
        facts = self.root / "memory" / "facts.md"
        facts.parent.mkdir(parents=True, exist_ok=True)
        if not facts.exists():
            facts.write_text("# Facts I have learned\n\n")
        with facts.open("a") as f:
            f.write(f"- [{datetime.now():%Y-%m-%d %H:%M}] {fact.strip()}\n")
        return "remembered"

    def _t_update_self_model(self, text: str) -> str:
        edit = selfedit.parse(f"<self_model>\n{text}\n</self_model>")
        if edit.new_self is None:
            return f"ERROR: self-model not changed: {edit.rejected or 'empty or no-change text'}"
        path = self.root / "self" / "self.md"
        path.write_text(edit.new_self + "\n")
        return "self-model updated"

    def _t_update_personality(self, layer: str, text: str) -> str:
        return personality.update(self.root, layer, text)

    def _run_plugin(self, name: str, args: dict) -> str:
        proc = subprocess.run([sys.executable, "-E", "-s", "-m", "shaft.plugin_runner", name],
                              input=json.dumps(args), capture_output=True, text=True,
                              cwd=self.root, timeout=PLUGIN_TIMEOUT_S)
        if proc.returncode != 0:
            return f"ERROR: plugin {name} failed (exit {proc.returncode}): {proc.stderr[-1500:]}"
        return proc.stdout
