"""The Mac app shows a setup verb's or a setup route's error as it is, with Try again: it never tells the operator to
run a terminal command (the onboarding's first anti-goal; final review I6). This scans the code those messages come
from for terminal commands. A message with a terminal command is the terminal's wording only when it is the first
argument of core.say(human, app), whose second argument is what the app shows; the allowlist names, with the reason,
the few strings no app reads."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

SUP = Path(__file__).resolve().parents[1]
COMMANDS = ("shaft ", "brew ", "tailscale ", "git config", "xcode-select", "launchctl ")

# module: the functions whose messages reach the wizard (None: every function of the module)
SCOPE = {
    "__main__.py": {"cmd_new", "cmd_first_reply", "_git_identity_problem", "cmd_codex", "_codex_login_json"},
    "provider.py": None,
    "opsecrets.py": None,
    "install.py": None,
    "api.py": {"service_on", "restart", "json_verb", "command", "tailnet_problem"},
    "web.py": {"command", "restart", "_pair_local"},
    "telegram.py": {"command", "new_pairing_code"},
    "modelcall.py": {"available", "reads_images"},
    "codexcli.py": {"_start", "login_json", "login", "status"},
    "app_api.py": "app",       # every function: only the apps read /app/v1
    "setup.py": None,
}

# (module, function, a piece of the string): why no app shows it
ALLOWLIST = {
    ("opsecrets.py", "command", "usage: shaft secrets"): "a usage error (exit 2): the app's route sends a valid name",
    ("install.py", "_identity", "git config --local"): "names the command that failed, with git's words; not advice",
    ("web.py", "command", "--local goes with"): "usage errors (exit 2) of flags the app always sends right",
    ("web.py", "command", "no SHAFT_API_LOGINS — run: shaft api on (it records your tailnet login)"):
        "the terminal branch: with --json the next line replaces it with the app's words",
    ("web.py", "command", "unpair what? shaft web unpair"): "unpair is a terminal verb; the app never runs it",
    ("api.py", "command", "no key yet — run: shaft api on"): "`api key` is a terminal verb (it prints the key)",
    ("api.py", "command", "shaft api inbox-key"): "`api status` and `api inbox-key` are terminal verbs",
    ("api.py", "command", "  key:    shaft api key"): "the tailnet `api on` summary printed in a terminal; with --json "
                                                     "json_verb prints only ok",
    ("__main__.py", "cmd_codex", " has no --json"): "a usage error (exit 2) of a flag the app sends only to login",
    ("__main__.py", "cmd_codex", "missing; run shaft codex login"): "`codex status` is a terminal verb",
    ("api.py", "command", "--local goes with on only (shaft api on --local)"): "a usage error (exit 2)",
    ("api.py", "command", "API restarted; tailscale serve left as it was"): "the terminal's summary: with --json "
                                                                             "json_verb prints only ok",
    ("web.py", "command", "pair a browser: shaft web pair --name"): "the tailnet `web on` summary printed in a "
                                                                   "terminal; with --json json_verb prints only ok",
    ("telegram.py", "command", "usage: shaft telegram token"): "a usage error (exit 2): the route checks the name first",
    ("telegram.py", "command", "has no --json"): "a usage error (exit 2) of a flag the app never sends",
    ("telegram.py", "command", "shaft telegram pair"): "pairing help printed by the terminal pair and status",
    ("telegram.py", "command", "shaft telegram token <instance>)"): "the terminal pair's list of bots",
}


def _said_by_human_arg(tree: ast.AST) -> set[int]:
    """ids of the nodes that are the human wording: the first argument of a say(human, app) call."""
    human = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and len(node.args) >= 2:
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in ("say", "core_say"):
                for sub in ast.walk(node.args[0]):
                    human.add(id(sub))
    return human


def _not_shown(tree: ast.AST) -> set[int]:
    """Docstrings, log lines and argparse help: never an answer the app shows."""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):           # an f-string is read whole, not part by part
            skip.update(id(v) for v in node.values)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and node.body \
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
            skip.add(id(node.body[0].value))
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in ("log", "_log", "supervisor_log", "add_argument", "add_parser"):
                for sub in ast.walk(node):
                    skip.add(id(sub))
    return skip


def offenders() -> list[tuple[str, str, int, str]]:
    found = []
    for module, functions in SCOPE.items():
        tree = ast.parse((SUP / module).read_text())
        human, skip = _said_by_human_arg(tree), _not_shown(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(functions, set) and fn.name not in functions:
                continue
            for node in ast.walk(fn):
                if id(node) in human or id(node) in skip:
                    continue
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    text = node.value
                elif isinstance(node, ast.JoinedStr):
                    text = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
                else:
                    continue
                if any(c in text for c in COMMANDS) and not any(
                        (module, fn.name) == (m, f) and piece in text for (m, f, piece) in ALLOWLIST):
                    found.append((module, fn.name, node.lineno, text))
    return sorted(set(found))


class AppWordingTest(unittest.TestCase):
    def test_no_setup_message_the_app_shows_names_a_terminal_command(self):
        bad = offenders()
        self.assertEqual(bad, [], "\n".join(f"{m}:{line} in {f}(): {t!r}" for m, f, line, t in bad))

    def test_the_allowlist_names_only_strings_that_still_exist(self):
        for (module, fn, piece), why in ALLOWLIST.items():
            with self.subTest(piece=piece):
                self.assertIn(piece, (SUP / module).read_text(), why)


if __name__ == "__main__":
    unittest.main()
