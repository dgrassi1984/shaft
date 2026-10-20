"""Resolved configuration with provenance.

Precedence: environment variable > config.env > built-in default.
A bad value falls back to the next source with a warning; a missing
required value is an error that names where it looked.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# name: (type, default). A default of None means required.
FIELDS: dict[str, tuple[type, object]] = {
    "NAME": (str, ""),            # empty → the instance directory's name
    "LLM_BASE_URL": (str, None),
    "LLM_MODEL": (str, None),
    "LLM_API_KEY": (str, ""),
    "LLM_TIMEOUT_S": (int, 900),
    "MAX_TOKENS_REFLECT": (int, 16384),
    "MAX_TOKENS_TALK": (int, 8192),
    "WAKE_INTERVAL_MINUTES": (int, 120),   # read by the supervisor's scheduler (floor 10; 0 = never)
    "LLM_CONTEXT_TOKENS": (int, 262144),   # the model's context window (Bonsai 2: 262,144), for the status bar
    "LLM_REASONING_EFFORT": (str, "medium"),   # local server: medium thinks as much as a request needs; xhigh to the cap
    "LLM_THINK_FOLLOWUP": (int, 1024),   # reasoning tokens after a tool result (local server; 0 = its own budget)
    "LLM_PRESENCE_PENALTY": (int, 15),   # tenths: 15 = 1.5 (Qwen's advice against loops in quantized models)
    "MAX_TOKENS_STEP": (int, 16384),     # one model turn inside a tool loop (live, sleep)
    "MAX_STEPS_LIVE": (int, 12),
    "MAX_STEPS_SLEEP": (int, 30),
    "MAX_STEPS_TALK": (int, 0),        # 0 = no limit (conversations)
    "CONTEXT_JOURNAL_CHARS": (int, 16000),
    "OPERATOR_NAME": (str, "the operator"),
}


class ConfigError(Exception):
    pass


@dataclass
class Config:
    root: Path
    values: dict[str, object] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def __getattr__(self, name: str):
        values = self.__dict__.get("values", {})
        if name in values:
            return values[name]
        raise AttributeError(name)

    def describe(self) -> str:
        parts = []
        for name in FIELDS:
            value = self.values[name]
            if name == "LLM_API_KEY" and value:
                value = "***"
            parts.append(f"{name}={value} [{self.sources[name]}]")
        return f"root={self.root} " + " ".join(parts)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Quotes are stripped; `#` starts a comment outside quotes."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"warning: {path}:{lineno}: ignoring line without '=': {raw!r}", file=sys.stderr)
            continue
        key, rest = line.split("=", 1)
        rest = rest.strip()
        if rest[:1] in ("'", '"'):
            quote = rest[0]
            end = rest.find(quote, 1)
            value = rest[1:end] if end != -1 else rest[1:]
        else:
            value = rest.split("#", 1)[0].strip()
        out[key.strip()] = value
    return out


# Values the model server accepts (Bonsai's template raises on anything else, which would fail every call).
CHOICES = {"LLM_REASONING_EFFORT": ("medium", "xhigh", "low")}


def _coerce(kind: type, raw: str, name: str = ""):
    if name in CHOICES and raw not in CHOICES[name]:
        raise ValueError(f"must be one of {', '.join(CHOICES[name])}")
    if kind is int:
        return int(raw)
    return raw


def load(root: Path | None = None, environ: dict[str, str] | None = None) -> Config:
    environ = dict(os.environ if environ is None else environ)
    root = Path(root or environ.get("SHAFT_ROOT") or PACKAGE_ROOT).resolve()
    file_values = parse_env_file(root / "config.env")
    cfg = Config(root=root)
    missing = []
    for name, (kind, default) in FIELDS.items():
        chosen, source = default, "default"
        for src_name, src in (("config.env", file_values), ("env", environ)):
            if name in src and src[name] != "":
                try:
                    chosen, source = _coerce(kind, src[name], name), src_name
                except ValueError as e:
                    why = str(e) if name in CHOICES else f"is not a valid {kind.__name__}"
                    print(f"warning: {name}={src[name]!r} from {src_name}: {why}; keeping {chosen!r} [{source}]",
                          file=sys.stderr)
        if chosen is None:
            missing.append(name)
        cfg.values[name], cfg.sources[name] = chosen, source
    if not cfg.values["NAME"]:
        cfg.values["NAME"], cfg.sources["NAME"] = root.name, "dirname"
    if missing:
        raise ConfigError(
            f"missing required config {', '.join(missing)}: set it in {root / 'config.env'} "
            f"or the environment. Resolved so far: "
            + " ".join(f"{k}={cfg.values[k]!r} [{cfg.sources[k]}]" for k in FIELDS)
        )
    return cfg
