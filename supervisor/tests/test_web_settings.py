"""The web UI's Settings page (docs/plans/PLAN-webui-chat.md §4, S1): every group renders each knob with its value, source and
default; a save runs `shaft settings set … --from-web` with what the page showed as the expected value; ports and
paths have no form; a saved value not live yet says which services to restart and offers the guarded restart."""
from __future__ import annotations

import copy
import re
import tempfile
import urllib.parse
from pathlib import Path
from unittest import mock

from supervisor import core, panel, settings, web, web_settings
from supervisor.tests.test_web_actions import ActionsTest


def fields(file_text: str = "") -> list[dict]:
    """The real listing (every registered knob), with a settings file holding `file_text`."""
    d = Path(tempfile.mkdtemp(prefix="shaft-web-settings-"))
    (d / "settings.env").write_text(file_text)
    with mock.patch.object(core, "SETTINGS_FILE", d / "settings.env"), \
            mock.patch.object(settings, "not_live", return_value={}):
        core._file_cache.update(stamp=None, values={})
        try:
            return settings.listing()
        finally:
            core._file_cache.update(stamp=None, values={})


FS = fields("SHAFT_JUDGE_MODELS=claude-sonnet-5-5,openrouter/some/new-model\n")


class SettingsPageTest(ActionsTest):
    def page(self, fs=FS, path="/settings", runs=(), secrets=None) -> str:
        with mock.patch.object(web_settings, "listing", return_value={"settings": fs, "file": "/x/settings.env"}), \
                mock.patch.object(panel, "scheduled_runs", return_value=list(runs)), \
                mock.patch.object(web_settings, "secrets_env", return_value=secrets or {}):
            r = self.req(path, cookie=self.cookie)
        self.assertEqual(r.status, 200, r.body[:300])
        return r.body.decode()

    def test_every_group_renders_with_value_source_and_default(self):
        body = self.page()
        for group in core.KNOB_GROUPS:
            self.assertIn(f"<h2>{group}</h2>", body)
        for k in core.KNOBS.values():
            self.assertIn(f"<code>{k.name}</code>", body)
        self.assertIn('claude-sonnet-5-5,openrouter/some/new-model</code> <span class="dim">[settings.env]', body)
        self.assertIn("default <code>openrouter/xiaomi/mimo-v2.6-pro,claude-sonnet-5-5</code>", body)
        self.assertIn('2400</code> <span class="dim">[default]', body)                  # SHAFT_CYCLE_TIMEOUT_S
        self.assertIn('href="/settings" class="on"', body)

    def test_the_judge_pool_is_a_multi_select_of_the_cloud_models(self):
        body = self.page()
        self.assertIn('name="models[]" value="claude-sonnet-5-5" checked', body)
        self.assertIn('name="models[]" value="openrouter/xiaomi/mimo-v2.6-pro">', body)   # offered, not ticked
        self.assertIn('name="models[]" value="claude-haiku-5-5">', body)
        self.assertIn('name="models[]" value="openrouter/some/new-model" checked', body)   # the pool as set
        self.assertIn('name="other" value=""', body)

    def test_ports_paths_and_endpoints_have_no_form(self):
        body = self.page()
        for key in ("SHAFT_WEB_PORT", "SHAFT_API_PORT", "SHAFT_LLM_PORTS", "SHAFT_TELEGRAM_API", "SHAFT_FT_PYTHON"):
            self.assertIn(f"terminal only: <code>shaft settings set {key}=…</code>", body)
            self.assertNotIn(f'name="key" value="{key}"', body)
        self.assertIn('name="key" value="SHAFT_JUDGE_MODELS"', body)

    def test_long_values_fit_a_phone(self):
        """Live, 2026-10-19: JSON values in a size=60 input pushed the page past a 400 px screen. A long value is a
        textarea (as wide as its column); no input sets its own width."""
        body = self.page()
        self.assertIn('<textarea name="value" rows="3" maxlength="4000" aria-label="SHAFT_OPENROUTER_PROVIDER">', body)
        self.assertLessEqual(max(map(int, re.findall(r' size="(\d+)"', body)), default=0), 8)   # numbers only
        self.assertIn('<code class="wrap">/x/settings.env</code>', body)

    def test_secrets_show_their_names_never_their_values(self):
        body = self.page(secrets={"ANTHROPIC_API_KEY": "sk-ant-very-secret", "OPENROUTER_API_KEY": "or-secret"})
        self.assertIn("ANTHROPIC_API_KEY, OPENROUTER_API_KEY", body)
        self.assertNotIn("very-secret", body)
        self.assertNotIn("or-secret", body)

    def test_saved_not_live_yet_names_the_restarts_and_guards_them(self):
        fs = copy.deepcopy(FS)
        for f in fs:
            if f["name"] == "SHAFT_JUDGE_MODELS":
                f["not_live"] = ["api", "scheduler"]
            if f["name"] == "SHAFT_JUDGE_ALERT_USD":
                f["not_live"] = ["web"]
        body = self.page(fs, "/fragment/settings", runs=["wren (live since 10:00)"])
        self.assertIn("Saved, not live yet: restart api, scheduler, web.", body)
        self.assertIn('data-post="/action/service"', body)
        self.assertIn("Restart api", body)
        self.assertIn("Force restart scheduler now? Not safe: the scheduler&#x27;s runs share its process group", body)
        self.assertIn('>Restart api</button>', body)                    # the api is not held back by a scheduled run
        self.assertIn("the web UI cannot restart itself", body)
        live = self.page(FS, "/fragment/settings")
        self.assertIn("Every running service holds the saved settings", live)
        self.assertIn('data-poll="/fragment/settings"', self.page())

    def test_the_listing_comes_from_the_cli(self):
        data = web_settings.listing()                         # the real `shaft settings --json`, in its own process
        names = {f["name"] for f in data["settings"]}
        self.assertEqual(names, set(core.KNOBS))


class SettingsActionsTest(ActionsTest):
    def post(self, verb: str, pairs: list[tuple[str, str]]) -> tuple[int, str]:
        body = urllib.parse.urlencode(pairs).encode()
        with mock.patch.object(web, "overview", return_value={"instances": [], "alerts": []}):
            r = self.req(f"/action/{verb}", "POST", cookie=self.cookie, body=body,
                         headers={"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(self.cookie)})
        return r.status, r.body.decode()

    def test_save_runs_shaft_settings_with_expect(self):
        self.post("settings-set", [("key", "SHAFT_IDLE_MINUTES"), ("value", " 45 "), ("saw", "30"), ("saw_set", "1")])
        self.post("settings-set", [("key", "SHAFT_JUDGE2_EVERY"), ("value", "2"), ("saw", ""), ("saw_set", "0")])
        self.post("settings-unset", [("key", "SHAFT_IDLE_MINUTES"), ("saw", "45"), ("saw_set", "1")])
        self.assertEqual([r[1] for r in self.ran], [
            ["settings", "set", "SHAFT_IDLE_MINUTES=45", "--from-web", "--expect", "SHAFT_IDLE_MINUTES=30"],
            ["settings", "set", "SHAFT_JUDGE2_EVERY=2", "--from-web", "--expect-unset", "SHAFT_JUDGE2_EVERY"],
            ["settings", "unset", "SHAFT_IDLE_MINUTES", "--from-web", "--expect", "SHAFT_IDLE_MINUTES=45"]])
        self.assertEqual({r[0] for r in self.ran}, {"web"})

    def test_the_ticked_models_and_the_typed_ones_make_the_pool(self):
        status, _ = self.post("settings-set", [
            ("key", "SHAFT_JUDGE_MODELS"), ("saw", ""), ("saw_set", "0"), ("kind", "models"),
            ("models[]", "claude-sonnet-5-5"), ("models[]", "openrouter/xiaomi/mimo-v2.6-pro"),
            ("other", " openrouter/z-ai/glm-5.3-flash, claude-sonnet-5-5")])
        self.assertEqual(status, 200)
        self.assertEqual(self.ran[0][1][2],
                         "SHAFT_JUDGE_MODELS=claude-sonnet-5-5,openrouter/xiaomi/mimo-v2.6-pro,openrouter/z-ai/glm-5.3-flash")

    def test_none_ticked_sends_an_empty_pool_which_the_cli_refuses(self):
        self.result = (False, "refused: SHAFT_JUDGE_MODELS='' is refused: must name at least one model")
        status, body = self.post("settings-set", [("key", "SHAFT_JUDGE_MODELS"), ("kind", "models"), ("other", "")])
        self.assertEqual(self.ran[0][1][2], "SHAFT_JUDGE_MODELS=")
        self.assertEqual(status, 409)
        self.assertIn("at least one model", body)

    def test_not_a_setting_is_refused_before_anything_runs(self):
        status, body = self.post("settings-set", [("key", "LLM_MODEL"), ("value", "x")])
        self.assertEqual((status, body), (409, "<p>which setting?</p>"))
        self.assertNothingRan()

    def test_the_cli_refuses_what_the_web_may_not_set(self):
        """--from-web is what keeps ports terminal-only: checked against the real CLI, not a mock."""
        with mock.patch.object(core, "SETTINGS_FILE", Path(tempfile.mkdtemp()) / "settings.env"):
            with self.assertRaises(settings.Refused):
                settings.set_values({"SHAFT_WEB_PORT": "8790"}, from_web=True)
