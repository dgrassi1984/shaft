"""The web UI's Config, Drive and Access pages (docs/plans/PLAN-webui.md M3): each save runs the exact CLI command
(`shaft config`, `shaft drive`, `shaft limits`, …) with what the page showed as the expected value, a secret's value
never reaches a page, and drive.md is saved only after its diff."""
from __future__ import annotations

import urllib.parse
from pathlib import Path
from unittest import mock

from supervisor import actions, opconfig, web, web_config
from supervisor.tests.test_web_actions import ActionsTest

FIELDS = [{"key": "LLM_MODEL", "set": True, "value": "openrouter/xiaomi/mimo-v2.6-pro", "default": "bonsai",
           "help": "Which model <b>I</b> run on.", "secret": False, "numeric": False,
           "choices": ["claude-haiku-5-5", "openrouter/xiaomi/mimo-v2.6-pro", "gpu/bonsai2"]},
          {"key": "LLM_API_KEY", "set": True, "value": "(set, hidden)", "default": "", "help": "", "secret": True,
           "numeric": False, "choices": None},
          {"key": "WAKE_INTERVAL_MINUTES", "set": False, "value": "", "default": "120", "help": "How often I wake.",
           "secret": False, "numeric": True, "choices": None}]
ACCESS = {"tools_off": ["delegate_coding"], "wake_floor": 5, "wake_floor_default": 10, "wake_floor_own": True,
          "cloud_cap": 15.0, "cloud_cap_default": 8.0, "cloud_cap_own": True, "spent_today": 3.03, "jobs_off": False,
          "jobs_text": "wren: jobs on\n  watcher: every 15 min", "shared": "/Users/example/Shared",
          "mail": [{"account": "work", "label": "AssistantAccess"}],
          "calendar": [{"account": "home", "calendar": "abc@group.calendar.google.com", "name": "the operator's Cal"}],
          "accounts": ["work", "home"], "battery_off": True,
          "keys": ["github: hosts api.github.com · Authorization Bearer · stored 2026-10-14 09:00"]}


class ConfigActionsTest(ActionsTest):
    def post(self, verb: str, **form) -> tuple[int, str]:
        return self.act(verb, **{k: urllib.parse.quote(v, safe="") for k, v in form.items()})

    def test_a_field_saves_through_shaft_config_expecting_what_the_page_showed(self):
        self.post("config-set", name="wren", key="LLM_MODEL", value="claude-haiku-5-5",
                  saw="openrouter/xiaomi/mimo-v2.6-pro", saw_set="1")
        self.post("config-set", name="wren", key="WAKE_INTERVAL_MINUTES", value="30", saw="", saw_set="0")
        self.post("config-unset", name="wren", key="CUSTOM", saw="yes", saw_set="1")
        self.assertEqual([r[1] for r in self.ran], [
            ["config", "wren", "set", "LLM_MODEL=claude-haiku-5-5", "--expect", "LLM_MODEL=openrouter/xiaomi/mimo-v2.6-pro"],
            ["config", "wren", "set", "WAKE_INTERVAL_MINUTES=30", "--expect-unset", "WAKE_INTERVAL_MINUTES"],
            ["config", "wren", "unset", "CUSTOM", "--expect", "CUSTOM=yes"]])
        self.assertEqual({r[0] for r in self.ran}, {"web"})

    def test_a_value_with_spaces_and_equals_passes_whole(self):
        self.post("config-set", name="wren", key="NOTE", value="a = b & c", saw="", saw_set="0")
        self.assertEqual(self.ran[0][1][3], "NOTE=a = b & c")

    def test_the_drive_goes_through_a_file_that_is_removed_after(self):
        seen = {}

        def now(args, timeout=60, who="top"):
            path = Path(args[args.index("--file") + 1])
            seen.update(args=args, text=path.read_text(), path=path)
            return True, "wren: drive.md written · committed as the operator's"
        with mock.patch.object(actions, "run_now", now):
            status, body = self.post("drive-set", name="wren", sha="abc", text="Help with mail.\r\nAnd calendars.")
        self.assertEqual(status, 200, body)
        self.assertEqual(seen["args"][:3] + seen["args"][-2:], ["drive", "wren", "set", "--expect-sha", "abc"])
        self.assertEqual(seen["text"], "Help with mail.\nAnd calendars.")
        self.assertFalse(seen["path"].exists())
        self.post("drive-remove", name="wren", sha="abc")
        self.assertEqual(self.ran[-1][1], ["drive", "wren", "remove", "--expect-sha", "abc"])

    def test_access_actions_run_their_own_verbs(self):
        for verb, form in (("limit", {"limit": "wake-floor", "value": "10"}), ("limit", {"limit": "cloud-cap", "value": "off"}),
                           ("jobs", {"op": "off"}), ("share", {"folder": "/Users/example/Shared"}), ("share", {"folder": "off"}),
                           ("mail", {"op": "grant", "alias": "work", "what": "Assistant Access"}),
                           ("mail", {"op": "off"}),
                           ("calendar", {"op": "revoke", "alias": "home", "what": "abc@group.calendar.google.com"}),
                           ("battery-switch", {"op": "on"})):
            self.post(verb, name="wren", **form)
        self.assertEqual([r[1] for r in self.ran], [
            ["limits", "wren", "wake-floor", "10"], ["limits", "wren", "cloud-cap", "off"], ["jobs", "wren", "off"],
            ["share", "wren", "/Users/example/Shared"], ["share", "wren", "off"],
            ["mail", "wren", "grant", "work", "Assistant Access"], ["mail", "wren", "off"],
            ["calendar", "wren", "revoke", "home", "abc@group.calendar.google.com"], ["battery", "on", "wren"]])

    def test_refused_before_anything_runs(self):
        for verb, form, why in (("config-set", {"key": ""}, "which key"), ("drive-set", {"text": "  ", "sha": "x"}, "empty"),
                                ("limit", {"limit": "speed", "value": "9"}, "no limit"), ("jobs", {"op": "maybe"}, "on or off"),
                                ("mail", {"op": "grant", "alias": "work"}, "needs an account and a label"),
                                ("battery-switch", {"op": "x"}, "on or off")):
            status, body = self.post(verb, name="wren", **form)
            self.assertEqual(status, 409, verb)
            self.assertIn(why, body)
        self.assertNothingRan()

    def test_the_cli_refusing_reads_once(self):
        self.result = (False, "refused: LLM_MODEL changed since you looked: it is now 'x' (you saw 'y')")
        status, body = self.post("config-set", name="wren", key="LLM_MODEL", value="z", saw="y", saw_set="1")
        self.assertEqual(status, 409)
        self.assertIn("refused: LLM_MODEL changed since you looked", body)
        self.assertNotIn("refused: refused", body)


class ConfigPagesTest(ActionsTest):
    def page(self, path: str) -> str:
        r = self.req(path, cookie=self.cookie)
        self.assertEqual(r.status, 200, r.body[:400])
        return r.body.decode()

    def test_the_config_page_one_field_per_key_and_the_secret_masked(self):
        with mock.patch.object(opconfig, "fields", return_value=FIELDS), \
                mock.patch.object(web, "_config_history", return_value=["2026-10-19 12:00 · SHAFT supervisor · wren: "
                                                                        "config.env changed from outside the instance"]):
            body = self.page("/i/wren/config")
        self.assertIn('<option value="claude-haiku-5-5">', body)
        self.assertIn('<option value="openrouter/xiaomi/mimo-v2.6-pro" selected>', body)
        self.assertIn('name="saw" value="openrouter/xiaomi/mimo-v2.6-pro"', body)
        self.assertIn("It also sets LLM_BASE_URL to match.", body)
        self.assertIn("Which model &lt;b&gt;I&lt;/b&gt; run on.", body)
        self.assertIn("a secret: <code>shaft secret wren …</code>", body)
        self.assertNotIn('value="LLM_API_KEY"', body)                     # no form for it at all
        self.assertIn('inputmode="numeric"', body)
        self.assertIn("not set · default 120", body)
        self.assertIn("changed from outside the instance", body)

    def test_no_secret_value_reaches_the_page(self):
        root = Path(self.sup) / "inst"
        (root / "shaft").mkdir(parents=True)
        (root / "config.env").write_text('LLM_API_KEY="sk-very-secret"\nGITHUB_TOKEN=ghp_abcdefgh12345\nX=1\n')
        with mock.patch.object(opconfig, "instance_root", return_value=root), \
                mock.patch.object(opconfig, "TEMPLATE", Path(self.sup) / "none"), \
                mock.patch.object(web, "_config_history", return_value=[]):
            body = self.page("/i/wren/config")
        self.assertNotIn("sk-very-secret", body)
        self.assertNotIn("ghp_abcdefgh12345", body)

    def test_the_drive_page_shows_it_and_saves_after_a_diff(self):
        with mock.patch.object(opconfig, "drive", return_value=("# Purpose\nHelp with **mail**.\n", "sha1")):
            body = self.page("/i/wren/drive")
            self.assertIn("<b>mail</b>", body)                              # rendered
            self.assertIn('data-diff="/preview/drive/wren"', body)
            self.assertIn('name="sha" value="sha1"', body)
            r = self.req("/preview/drive/wren", "POST", cookie=self.cookie, body=b"text=%23+Purpose%0AHelp+with+calendars.",
                         headers={"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(self.cookie)})
        self.assertEqual(r.status, 200)
        diff = r.body.decode()
        self.assertIn("-Help with **mail**.", diff)
        self.assertIn("+Help with calendars.", diff)
        self.assertNothingRan()                                             # a preview changes nothing

    def test_the_access_page_shows_every_grant_and_key_names_only(self):
        with mock.patch.object(web_config, "access", return_value=ACCESS):
            body = self.page("/i/wren/access")
        for want in ("5 min (yours)", "$15.00 a day (yours)", "spent today $3.03", "watcher: every 15 min",
                     "/Users/example/Shared", "work/AssistantAccess", "home/the operator&#x27;s Cal", "battery is <b>off</b>",
                     "github: hosts api.github.com", 'data-post="/action/limit"', 'data-post="/action/mail"',
                     'value="abc@group.calendar.google.com"', "Switch the battery on", "delegate_coding"):
            self.assertIn(want, body)

    def test_every_new_tab_is_linked(self):
        with mock.patch.object(web_config, "access", return_value=ACCESS):
            body = self.page("/i/wren/access")
        for href in ("/i/wren/config", "/i/wren/drive", "/i/wren/access"):
            self.assertIn(f'href="{href}"', body)
