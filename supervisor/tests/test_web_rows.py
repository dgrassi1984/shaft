"""Issue #17: mutations return the row's new state without replacing other edits."""
import contextlib
import io
import re
import shutil
import subprocess
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from supervisor import actions, core, opconfig, web_config, web_settings
from supervisor.tests.test_web_actions import ActionsTest
from supervisor.tests.test_web_config import ACCESS

from supervisor.tests.node_helpers import NODE_TIMEOUT_S


class SavedRowsTest(ActionsTest):
    def post_row(self, verb, **form):
        return self.req(f"/action/{verb}", "POST", cookie=self.cookie,
                        body=urllib.parse.urlencode(form).encode(),
                        headers={"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(self.cookie),
                                 "x-row": "1"})

    def test_issue_17_config_twice_and_unset(self):
        """Reverting #17 leaves saw stale, so the next save conflicts with the first."""
        root = self.sup / "config-test"
        root.mkdir()
        (root / "config.env").write_text("CUSTOM=first\n")

        def cli(args, *unused, **kwargs):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                result = opconfig.config_command(args[1], args[2], [args[3]],
                                                  [args[5]] if args[4] == "--expect" else [],
                                                  [args[5]] if args[4] == "--expect-unset" else [])
            return result == 0, out.getvalue()

        with mock.patch.object(opconfig, "instance_root", return_value=root), \
                mock.patch.object(opconfig, "instance_config", return_value={}), \
                mock.patch.object(opconfig, "_template_text", return_value=""), \
                mock.patch.object(opconfig, "_commit", return_value=False), \
                mock.patch.object(opconfig, "log"), mock.patch.object(actions, "run_now", cli):
            first = self.post_row("config-set", name="wisp", key="CUSTOM", value="second", saw="first", saw_set="1")
            self.assertEqual(first.status, 200, first.body)
            self.assertIn(b'name="saw" value="second"', first.body)
            saw = re.search(rb'name="saw" value="([^"]*)"', first.body)[1].decode()
            second = self.post_row("config-set", name="wisp", key="CUSTOM", value="third", saw=saw, saw_set="1")
            self.assertEqual(second.status, 200, second.body)
            self.assertIn(b'name="saw" value="third"', second.body)
            stale = self.post_row("config-set", name="wisp", key="CUSTOM", value="lost", saw=saw, saw_set="1")
            self.assertEqual(stale.status, 409)
            self.assertNotIn(b"<template", stale.body)
            unset = self.post_row("config-unset", name="wisp", key="CUSTOM", saw="third", saw_set="1")
            self.assertEqual(unset.status, 200, unset.body)
            self.assertIn(b'name="saw_set" value="0"', unset.body)
            self.assertNotIn(b">Unset<", unset.body)
            again = self.post_row("config-set", name="wisp", key="CUSTOM", value="back", saw="", saw_set="0")
            self.assertEqual(again.status, 200, again.body)
            self.assertEqual(core.config_values((root / "config.env").read_text())["CUSTOM"], "back")

    def test_issue_17_a_model_switch_returns_its_base_url_row_too(self):
        """A model save also writes LLM_BASE_URL (opconfig.set_values). Returning only the saved row left the base
        URL row's saw stale, so its next save was refused as changed since the page was drawn. The same holds for
        both effort rows, which a model switch may reset (effort per model, 2026-11-13)."""
        root = self.sup / "config-model"
        root.mkdir()
        (root / "config.env").write_text("LLM_MODEL=old-model\nLLM_BASE_URL=https://old.example/v1\n"
                                         "LLM_REASONING_EFFORT=medium\n")

        def cli(args, *unused, **kwargs):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                result = opconfig.config_command(args[1], args[2], [args[3]], [args[5]], [])
            return result == 0, out.getvalue()

        with mock.patch.object(opconfig, "instance_root", return_value=root), \
                mock.patch.object(opconfig, "instance_config", return_value={}), \
                mock.patch.object(opconfig, "_template_text", return_value=""), \
                mock.patch.object(opconfig, "base_url_for", return_value="https://new.example/v1"), \
                mock.patch.object(opconfig, "_check"), \
                mock.patch.object(opconfig, "_commit", return_value=False), \
                mock.patch.object(opconfig, "log"), mock.patch.object(actions, "run_now", cli):
            r = self.post_row("config-set", name="wisp", key="LLM_MODEL", value="new-model", saw="old-model",
                              saw_set="1")
        self.assertEqual(r.status, 200, r.body)
        rows = re.findall(rb'<tr data-row id="config-([A-Z_]+)"', r.body)
        self.assertEqual(rows, [b"LLM_MODEL", b"LLM_BASE_URL", b"LLM_REASONING_EFFORT", b"VOICE_REASONING_EFFORT"])
        self.assertIn(b'name="saw" value="https://new.example/v1"', r.body)

    def test_issue_17_add_returns_an_editable_row(self):
        """Reverting #17 leaves an added key invisible until reload, with no next-save token."""
        field = dict(key="CUSTOM", set=True, value="new", default=None, help="", secret=False, choices=None,
                     numeric=False)
        self.assertIn('data-add-row="#config-rows"', web_config.config_page("wisp", [], []))
        with mock.patch.object(opconfig, "fields", return_value=[field]):
            saved = self.post_row("config-set", name="wisp", key="CUSTOM", value="new", saw="", saw_set="0")
        self.assertEqual(saved.status, 200)
        self.assertEqual(saved.headers.get("X-Row"), "1")
        self.assertIn(b'id="config-CUSTOM"', saved.body)
        self.assertIn(b'name="saw" value="new"', saved.body)
        self.assertIn(b">Unset<", saved.body)

    def test_issue_17_settings_returns_saved_value_and_default_direction(self):
        """Reverting #17 returns only a toast and leaves both the value and reset button stale."""
        field = dict(name="SHAFT_CYCLE_TIMEOUT_S", value="45", in_file="45", source="file", default="2400",
                     web=True, kind="int", services=["runs"], help="Time limit.")
        with mock.patch.object(web_settings, "listing", return_value={"settings": [field]}):
            saved = self.post_row("settings-set", key=field["name"], value="45", saw="", saw_set="0")
            self.assertEqual(saved.status, 200)
            self.assertIn(b'<tr data-row', saved.body)
            self.assertIn(b'value="45"', saved.body)
            self.assertIn(b'name="saw" value="45"', saved.body)
            self.assertIn(b"Use the default", saved.body)
            self.assertNotIn(b'data-after="reload"', saved.body)
            field.update(value="2400", in_file=None, source="default")
            reset = self.post_row("settings-unset", key=field["name"], saw="45", saw_set="1")
            self.assertIn(b'value="2400"', reset.body)
            self.assertIn(b'name="saw_set" value="0"', reset.body)
            self.assertNotIn(b"Use the default", reset.body)

    def test_issue_17_drive_returns_new_sha_and_remove_direction(self):
        """Reverting #17 leaves Drive's old sha and Remove button after a mutation."""
        with mock.patch.object(web_config, "SUP_DIR", self.sup), \
                mock.patch.object(opconfig, "drive", return_value=("new purpose", "new-sha")):
            saved = self.post_row("drive-set", name="wisp", text="new purpose", sha="old-sha")
            self.assertEqual(saved.status, 200)
            self.assertIn(b'name="sha" value="new-sha"', saved.body)
            self.assertIn(b"new purpose", saved.body)
            self.assertIn(b"Remove drive.md", saved.body)
        with mock.patch.object(opconfig, "drive", return_value=(None, "empty-sha")):
            removed = self.post_row("drive-remove", name="wisp", sha="new-sha")
            self.assertIn(b'name="sha" value="empty-sha"', removed.body)
            self.assertNotIn(b"Remove drive.md", removed.body)

    def test_removing_wake_floor_returns_no_floor_control(self):
        with mock.patch.object(web_config, "access", return_value=ACCESS):
            r = self.post_row("limit", name="wisp", limit="wake-floor", value="5")
        self.assertEqual(r.status, 200)
        self.assertIn(b"back to none", r.body)
        self.assertIn(b'name="value" value="off"', r.body)
        access = dict(ACCESS, wake_floor=1, wake_floor_own=False)
        with mock.patch.object(web_config, "access", return_value=access):
            r = self.post_row("limit", name="wisp", limit="wake-floor", value="off")
        self.assertEqual(r.status, 200)
        self.assertIn(b"No floor", r.body)
        self.assertIn(b'already no floor', r.body)
        self.assertNotIn(b"Default floor", r.body)

    def test_issue_17_access_returns_only_changed_control(self):
        """Reverting #17 leaves switches facing the old direction; a whole-page swap loses other edits."""
        with mock.patch.object(web_config, "access", return_value=ACCESS):
            for verb, form, wanted, absent in [
                ("jobs", {"op": "on"}, "Switch jobs off", "Share</button>"),
                ("battery-switch", {"op": "off"}, "Switch the battery on", "Switch jobs"),
                ("limit", {"limit": "wake-floor", "value": "5"}, "5 min", "Set cloud cap"),
                ("limit", {"limit": "cloud-cap", "value": "15"}, "$15.00", "Set wake floor"),
                ("share", {"folder": "/Users/example/Shared"}, "Stop sharing", "Grant</button>"),
                ("mail", {"op": "grant", "alias": "work", "what": "AssistantAccess"}, "AssistantAccess", "Your calendars"),
                ("calendar", {"op": "grant", "alias": "home", "what": "cal"}, "operator&#x27;s Cal", "Your mail"),
            ]:
                with self.subTest(verb=verb, form=form):
                    r = self.post_row(verb, name="wisp", **form)
                    self.assertEqual(r.status, 200, r.body)
                    self.assertIn(wanted, r.body.decode())
                    self.assertNotIn(absent, r.body.decode())
        with mock.patch.object(core, "disabled_tools", return_value={"delegate_coding"}):
            r = self.post_row("tool", name="wisp", tool="delegate_coding", to="off")
            self.assertIn(b"Switch on", r.body)
            self.assertNotIn(b"Switch off", r.body)


class RowSwapJsTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_issue_17_row_swap_preserves_other_input_and_refusal(self):
        """Reverting #17 never replaces the saved row; replacing the page destroys the other input."""
        script = r"""
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const handlers = {}, other = { value: "unsaved other edit" }, notices = {};
const rows = [{}, { input: other }];
let replaced = null, ok = true, sent, adding = false, existing = null;
const saved = { id: "config-CUSTOM", value: "new", saw: "new" };
const row = { replaceWith(next) { rows[0] = next; replaced = next; } };
const form = { dataset: { post: "/action/config-set" },
  closest: q => q === "form[data-post]" ? form : q === "[data-row]" && !adding ? row : null,
  querySelector: () => null, querySelectorAll: () => [] };
const document = { readyState: "loading",
  addEventListener(k, fn) { (handlers[k] ||= []).push(fn); },
  querySelector: q => q === "#config-rows" ? { appendChild(next) { rows.push(next); } } : null,
  querySelectorAll: () => [],
  getElementById: id => id === "config-CUSTOM" ? existing : notices[id] ||= { setAttribute() {} },
  dispatchEvent() {} };
const context = { document, navigator: {}, URL, Event, URLSearchParams, AbortController,
  FormData: class { *[Symbol.iterator]() { yield ["value", "new"]; } },
  DOMParser: class { parseFromString() { return {
    body: { textContent: ok ? "Saved" : "Conflict" },
    querySelector: q => q === "template[data-saved-row]" ? { content: { firstElementChild: saved } } : null
  }; } },
  fetch: async (url, opts) => { sent = opts; return { ok, status: ok ? 200 : 409,
    text: async () => "<p>Saved</p><template data-saved-row>row</template>",
    headers: { get: k => k === "X-Row" && ok ? "1" : null } }; },
  window: { addEventListener() {}, confirm: () => true },
  location: { reload() { throw Error("must not reload"); } },
  setTimeout() {}, clearTimeout() {}, setInterval() {} };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
(async () => {
  for (const fn of handlers.submit) await fn({ target: form, preventDefault() {} });
  assert.equal(replaced, saved);
  assert.equal(rows[0].saw, "new");
  assert.equal(sent.headers["X-Row"], "1");
  assert.equal(rows[1].input.value, "unsaved other edit");
  replaced = null; ok = false;
  for (const fn of handlers.submit) await fn({ target: form, preventDefault() {} });
  assert.equal(replaced, null);
  assert.equal(rows[1].input.value, "unsaved other edit");
  ok = true; adding = true; form.dataset.addRow = "#config-rows";
  for (const fn of handlers.submit) await fn({ target: form, preventDefault() {} });
  assert.equal(rows.length, 3);
  assert.equal(rows[2], saved);
  existing = row;
  for (const fn of handlers.submit) await fn({ target: form, preventDefault() {} });
  assert.equal(rows.length, 3);                        // a default row is replaced, never duplicated
  assert.equal(rows[1].input.value, "unsaved other edit");
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        js = Path(__file__).resolve().parents[1] / "web_static" / "web.js"
        result = subprocess.run(["node", "-e", script, str(js)], capture_output=True, text=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr)
