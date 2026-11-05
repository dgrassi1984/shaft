"""The operator's edits of an instance's config.env and drive.md (docs/plans/PLAN-webui.md M3): `shaft config` and `shaft drive`,
which the web UI's Config and Drive pages run. Stdlib only.

config.env has two writers, the operator and the instance (the broker's set_config); both write under
core.config_lock, so neither loses the other's line. An edit here:
  - changes only the lines it names (core.config_with on the file as it is now), every other line and comment kept;
  - writes the line as set_config would, plus "# <operator>, <date>" (its old inline comment is left to git: a stale
    comment on a changed value is a lie);
  - is refused if a value is no longer what the editor saw (`expect`), naming what it is now;
  - is committed at once, as the operator's (runner._commit_outside_config), when the instance is free; while it runs,
    the edit stays in the file and the run commits it as the operator's before its own commit — the gate undoes only
    the instance's set_config lines, so it cannot take this one for the instance's (kant, 2026-10-13).
drive.md has one writer, the operator: replaced whole, with the same compare-and-set (its sha256), committed the same
way (runner._commit_outside_drive, which also tells the instance in its journal).

Secrets stay CLI-only (`shaft secret`): a key whose name says it holds one is shown masked and is not set here.
"""
from __future__ import annotations

import ast
import hashlib
import re
from datetime import date
from pathlib import Path

from .core import (TEMPLATE, UnsafePath, config_lock, config_set_line, config_values, config_with, instance_config,
                   instance_root, log, safe_read, safe_read_bytes, safe_write)

# A key that holds a secret by its name: *_KEY, *_TOKEN, or SECRET/PASSWORD anywhere — not MAX_TOKENS_STEP or
# LLM_CONTEXT_TOKENS, which a first, looser pattern hid as secrets (live, 2026-10-19).
SECRET_KEY = re.compile(r"(?:^|_)(?:KEY|TOKEN)$|SECRET|PASSWORD|PASSPHRASE")
LOCAL_URL = "http://127.0.0.1:8780/v1"              # SHAFT's own model server (the template's default)


class Refused(Exception):
    pass


def _key(line: str) -> str | None:
    k, eq, _ = line.partition("=")
    return k.strip() if eq and k.strip().isidentifier() else None


def _comments(text: str) -> dict[str, str]:
    """Each key's help: the comment block right above its line (or above the run of keys it belongs to), plus the
    line's own inline comment."""
    out, block, after_key = {}, [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            if after_key:                            # a new block starts after a run of keys
                block, after_key = [], False
            block.append(s.lstrip("#").strip())
            continue
        if not s:
            block, after_key = [], False
            continue
        if key := _key(line):
            after_key = True
            inline = line.partition("=")[2]
            inline = inline.split('"', 2)[2] if inline.strip().startswith('"') else inline
            inline = inline.partition("#")[2].strip()
            out[key] = " ".join(block + ([f"({inline})"] if inline else []))
            # the block stays for the next keys in the same run (one comment over several lines of keys)
    return out


def _template_text() -> str:
    try:
        return (TEMPLATE / "config.env").read_text()
    except OSError:
        return ""


def model_choices() -> list[dict]:
    """The models an instance may run on: SHAFT_CLOUD_MODELS and the self-hosted servers through the broker, or SHAFT's
    own server — each with the LLM_BASE_URL it needs."""
    from . import cloudllm, codexcli, selfhosted
    out = [{"model": m, "base_url": "broker", "label": f"{m} (cloud)"} for m in cloudllm.CLOUD_MODELS]
    out += [{"model": m, "base_url": "broker", "label": f"{m} (Codex plan)"} for m in codexcli.models()]
    out += [{"model": f"{s}/bonsai2", "base_url": "broker", "label": f"{s}/bonsai2 (self-hosted)"}
            for s in selfhosted.SERVERS]
    local = config_values(_template_text()).get("LLM_MODEL")
    if local:
        out.append({"model": local, "base_url": LOCAL_URL, "label": f"{local} (SHAFT's own server)"})
    return out


def base_url_for(model: str) -> str | None:
    from . import cloudllm, codexcli, selfhosted
    if model in cloudllm.CLOUD_MODELS or model in codexcli.models() or selfhosted.serves(model):
        return "broker"
    if model == config_values(_template_text()).get("LLM_MODEL"):
        return LOCAL_URL
    return None


def effort_choices(root: Path) -> tuple[str, ...] | None:
    """The reasoning efforts the instance's own code accepts (its shaft/config.py CHOICES), read as text: the
    supervisor never imports agent code."""
    try:
        text = safe_read(root, "shaft/config.py")   # the instance writes it: never read through a link
    except (OSError, UnsafePath, ValueError):
        return None
    try:
        tree = ast.parse(text)
        value = next((node.value for node in tree.body
                      if isinstance(node, (ast.Assign, ast.AnnAssign))
                      and any(isinstance(target, ast.Name) and target.id == "CHOICES"
                              for target in (node.targets if isinstance(node, ast.Assign) else [node.target]))), None)
        choices = ast.literal_eval(value) if value is not None else {}
    except (ValueError, SyntaxError, RecursionError, MemoryError):
        return None
    got = choices.get("LLM_REASONING_EFFORT") if isinstance(choices, dict) else None
    return tuple(str(c) for c in got) if isinstance(got, (tuple, list)) else None


def fields(name: str) -> list[dict]:
    """Every key the instance's config.env sets, then every key the template documents that it does not, with its
    value, its help and what values it takes."""
    root = instance_root(name)
    text = safe_read(root, "config.env", "")
    tmpl = _template_text()
    values, defaults = config_values(text), config_values(tmpl)
    helps, tmpl_helps = _comments(text), _comments(tmpl)
    order = list(dict.fromkeys([k for l in text.splitlines() if (k := _key(l))] +
                               [k for l in tmpl.splitlines() if (k := _key(l))]))
    efforts = effort_choices(root)
    out = []
    for key in order:
        secret = bool(SECRET_KEY.search(key))
        f = {"key": key, "set": key in values, "value": values.get(key, ""), "default": defaults.get(key),
             "help": helps.get(key) or tmpl_helps.get(key, ""), "secret": secret, "choices": None,
             "numeric": bool(re.fullmatch(r"\d+", defaults.get(key, "") or values.get(key, "") or "x"))}
        if secret:
            f["value"] = "(set, hidden)" if values.get(key) else "(empty)"
        if key == "LLM_MODEL":
            f["choices"] = [c["model"] for c in model_choices()]
        elif key == "LLM_REASONING_EFFORT" and efforts:
            f["choices"] = list(efforts)
        out.append(f)
    return out


def _check(name: str, root: Path, key: str, value: str) -> None:
    if SECRET_KEY.search(key):
        raise Refused(f"{key} looks like a secret: keys go in with `shaft secret {name} set …`, never config.env")
    if key == "LLM_MODEL" and base_url_for(value) is None:
        raise Refused(f"{value!r} is not a model an instance may run on: "
                      + ", ".join(c["model"] for c in model_choices()))
    if key == "LLM_REASONING_EFFORT" and (efforts := effort_choices(root)) and value not in efforts:
        raise Refused(f"LLM_REASONING_EFFORT must be one of {', '.join(efforts)} (its shaft/config.py), not {value!r}")
    numeric = re.fullmatch(r"\d+", config_values(_template_text()).get(key, "") or "x")
    if numeric and not re.fullmatch(r"\d+", value):
        raise Refused(f"{key} is a whole number, not {value!r}")


def coupled(key: str) -> list[str]:
    """Keys a set of `key` also writes (set_values): a page showing them must refresh them with it."""
    return ["LLM_BASE_URL"] if key == "LLM_MODEL" else []


def set_values(name: str, changes: dict[str, str | None], expect: dict[str, str | None] | None = None) -> dict:
    """Set (a value) or unset (None) keys in one write and one commit. `expect`: key -> the value the editor saw
    (None: not set). Returns {"changed", "committed", "notes"}; raises Refused with the reason."""
    root = instance_root(name)
    changes = dict(changes)
    if "LLM_MODEL" in changes and changes["LLM_MODEL"] is not None and "LLM_BASE_URL" not in changes:
        changes["LLM_BASE_URL"] = base_url_for(changes["LLM_MODEL"])      # a model switch is never half-applied
    who = instance_config(name).get("OPERATOR_NAME") or "operator"
    lines = {}
    for key, value in changes.items():
        if value is None:
            if SECRET_KEY.search(key):
                raise Refused(f"{key} looks like a secret: `shaft secret {name} delete …`")
            lines[key] = None
            continue
        _check(name, root, key, value)
        try:
            lines[key] = config_set_line(key, value, f"{who}, {date.today().isoformat()}")
        except ValueError as e:
            raise Refused(str(e)) from None
    path = root / "config.env"
    with config_lock(path):
        text = safe_read(root, "config.env", "")
        now = config_values(text)
        for key, saw in (expect or {}).items():
            if now.get(key) != saw:
                raise Refused(f"{key} changed since you looked: it is now "
                              + (repr(now[key]) if key in now else "not set") + f" (you saw {saw!r})")
        changed = []
        for key, line in lines.items():
            if (line is None and key not in now) or (line is not None and now.get(key) == changes[key]):
                continue                             # already so: the line (and its comment) stays as it is
            text = config_with(text, key, line)
            changed.append(key)
        if not changed:
            return {"changed": [], "committed": False, "notes": ["nothing to change"]}
        safe_write(root, "config.env", text)
    changed.sort()
    log(f"{name}: config.env {', '.join(f'{k}={changes[k]!r}' if changes[k] is not None else f'{k} unset' for k in changed)}"
        " by the operator")
    notes = _notes(name, changes)
    return {"changed": changed, "committed": _commit(name, root, "config"), "notes": notes}


def _notes(name: str, changes: dict) -> list[str]:
    from . import autonomy
    out = []
    raw = changes.get("WAKE_INTERVAL_MINUTES")
    if raw and raw.isdigit() and 0 < int(raw) < autonomy.wake_floor(name):
        out.append(f"its wake floor is {autonomy.wake_floor(name)} min (shaft limits): it wakes every "
                   f"{autonomy.wake_floor(name)} min, not {raw}")
    return out


def _commit(name: str, root: Path, what: str) -> bool | str:
    """Commit the operator's edit now if the instance is free: True if committed, False if it is running (its run
    commits it), or why committing failed — the edit is written either way, and the next run or `shaft update`
    commits it as the operator's."""
    from . import runner
    try:
        with runner.run_lock(name, wait=False, kind=f"operator {what} edit"):
            if what == "config":
                runner._commit_outside_config(name, root, [])
            else:
                runner._commit_outside_drive(name, root)
        return True
    except runner.Busy:
        return False
    except (Exception, SystemExit) as e:          # the file is written: say so, never report the edit as refused
        log(f"{name}: operator {what} edit written but not committed: {e}")
        return f"written, not committed ({e}); its next run or shaft update commits it as yours"


def drive(name: str) -> tuple[str | None, str]:
    """(drive.md's text or None, its sha256 — "none" when there is no file)."""
    try:
        data = safe_read_bytes(instance_root(name), "drive.md")   # never through a link
    except (OSError, UnsafePath):
        return None, "none"
    return data.decode(errors="replace"), hashlib.sha256(data).hexdigest()


def set_drive(name: str, text: str | None, expect_sha: str) -> dict:
    """Replace drive.md (None removes it: the default drive applies), if it is still what the editor saw."""
    root = instance_root(name)
    with config_lock(root / "drive.md"):
        _, now = drive(name)
        if now != expect_sha:
            raise Refused("drive.md changed since you looked: reload it")
        if text is None:
            (root / "drive.md").unlink(missing_ok=True)
        else:
            if not text.strip():
                raise Refused("an empty drive.md: remove it instead (the default drive applies)")
            safe_write(root, "drive.md", text.rstrip("\n") + "\n")
    log(f"{name}: drive.md {'removed' if text is None else 'changed'} by the operator")
    return {"committed": _commit(name, root, "drive")}


# -- `shaft config` / `shaft drive` ---------------------------------------------------------------------------------
def config_command(name: str, action: str | None, items: list[str], expect: list[str], expect_unset: list[str]) -> int:
    instance_root(name)
    if not action:
        for f in fields(name):
            mark = "" if f["set"] else "  (not set; template: " + str(f["default"]) + ")"
            print(f"{f['key']}={f['value']}{mark}")
        return 0
    try:
        if action == "set":
            pairs = [i.partition("=") for i in items]
            if not pairs or any(not eq for _, eq, _ in pairs):
                print("usage: shaft config <name> set KEY=VALUE [KEY=VALUE …]")
                return 2
            changes = {k.strip(): v for k, _, v in pairs}
        else:
            if not items:
                print("usage: shaft config <name> unset KEY [KEY …]")
                return 2
            changes = {k.strip(): None for k in items}
        saw = {k.strip(): v for k, _, v in (e.partition("=") for e in expect)} | {k: None for k in expect_unset}
        r = set_values(name, changes, saw)
    except Refused as e:
        print(f"refused: {e}")
        return 1
    if not r["changed"]:
        print(f"{name}: nothing to change")
        return 0
    print(f"{name}: {', '.join(r['changed'])} changed · " + _committed_text(r["committed"])
          + "".join(f" · {n}" for n in r["notes"]))
    return 0


def _committed_text(committed: bool | str) -> str:
    if committed is True:
        return "committed as the operator's"
    if committed is False:
        return "it is running now: its run commits this as the operator's before its own commit"
    return committed


def drive_command(name: str, action: str | None, file: str | None, expect_sha: str | None) -> int:
    instance_root(name)
    text, sha = drive(name)
    if not action:
        print(f"sha256 {sha}")
        print(text if text is not None else "(no drive.md: the default drive applies)")
        return 0
    if not expect_sha:
        print("--expect-sha is required (shaft drive <name> prints it)")
        return 2
    try:
        if action == "remove":
            r = set_drive(name, None, expect_sha)
        else:
            if not file:
                print("usage: shaft drive <name> set --file FILE --expect-sha SHA")
                return 2
            r = set_drive(name, Path(file).read_text(), expect_sha)
    except (Refused, OSError) as e:
        print(f"refused: {e}")
        return 1
    print(f"{name}: drive.md {'removed' if action == 'remove' else 'written'} · " + _committed_text(r["committed"]))
    return 0
