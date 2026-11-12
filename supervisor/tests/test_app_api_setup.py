"""The Mac app's setup routes under /app/v1 (docs/app-api.md §18): create an instance, the drive presets, provider keys,
the provider switch and its check, access grants, a Telegram token and the hidden welcome. Every state change runs
the CLI verb an operator would type (actions.run_now / run_json, faked here: no subprocess, no service, no network)
as the paired device; a secret travels on stdin, never in argv or a log line. It also writes the setup fixtures the
Swift Kit decodes (supervisor/tests/app_api_fixtures/setup-*.json, access.json, google-*.json)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import actions, app_api, calendar, core, drive_presets, google, mail, opconfig, setup, web_chat, web_config
from supervisor.tests.test_app_api import AppTest, write_or_check_fixtures
from supervisor.tests.test_web import ME
from supervisor.tests.test_web_local import LocalTest

CALENDARS = [{"id": "team-id@group.calendar.google.com", "title": "Team, \"core\"", "summary": "", "primary": False,
              "access": "owner"},
             {"id": "family-id@group.calendar.google.com", "title": "Family", "summary": "", "primary": False,
              "access": "owner"}]
DRIVE = "You were created to keep the ledgers straight. Check every total before you report it."
DEFAULT_DRIVE = "You were created with one innate drive: to improve yourself.\n"
KEY = "sk-or-v1-never-in-argv-or-a-log-91c3"
TOKEN = "123456:never-in-argv-or-a-log-AAbb"


class SetupTest(AppTest):
    def setUp(self):
        super().setUp()
        self.calls: list[dict] = []          # every CLI verb the routes ran, in order: args, timeout, who, input
        self.refuse: dict[str, tuple[int, list[dict]] | tuple[bool, str]] = {}    # verb -> its answer instead
        self.env: dict[str, str] = {}
        self.browser_version: str | None = "1.64.0"
        self.during_grant = None             # runs inside the next `calendar grant`: another request interleaving
        self.real_run_json = actions.run_json            # before the fake: the secret test runs the real one
        self.tmpl = Path(tempfile.mkdtemp(prefix="shaft-setup-tmpl-"))
        self.addCleanup(shutil.rmtree, self.tmpl, True)
        (self.tmpl / "drive.md").write_text(DEFAULT_DRIVE)
        for p in (mock.patch.object(actions, "run_json", side_effect=self.run_json),
                  mock.patch.object(actions, "run_now", side_effect=self.run_now),
                  mock.patch.object(core, "TEMPLATE", self.tmpl),
                  mock.patch.object(core, "secrets_env", side_effect=lambda: dict(self.env)),
                  mock.patch("supervisor.browser.version", side_effect=lambda: self.browser_version),
                  mock.patch.object(opconfig, "_commit", return_value=True), mock.patch.object(opconfig, "log"),
                  mock.patch("supervisor.cloudllm.spent_today", return_value=0.0)):
            p.start()
            self.addCleanup(p.stop)

    # -- the CLI, faked: what each verb would answer ------------------------------------------------------------------
    def record(self, args, timeout, who, input):
        self.calls.append({"args": list(args), "timeout": timeout, "who": who, "input": input})

    def run_json(self, args, timeout=actions.RUN_NOW_S, who="top", input=None):
        self.record(args, timeout, who, input)
        if args[0] in self.refuse:
            return self.refuse[args[0]]
        if args[0] == "new":
            name = args[1]
            root = self.inst / name
            for d in ("shaft", ".git"):
                (root / d).mkdir(parents=True)
            (root / "drive.md").write_text(DEFAULT_DRIVE)                   # the template's, as `shaft new` leaves it
            if "--paused-until-first-reply" in args:                         # as `shaft new`: paused, with its marker
                core.set_paused(name, True, reason="waiting for the operator's first reply", note=False)
                (self.sup / f"first-reply-{name}").write_text(json.dumps({"then": args[-1], "created": "x"}))
            return 0, [{"ok": True, "name": name, "branch": f"instance/{name}", "worktree": str(root),
                        "paused": "--paused-until-first-reply" in args}]
        if args[:2] == ["secrets", "set"]:
            return 0, [{"ok": True, "name": args[2]}]
        if args[0] == "first-reply":
            return 0, [{"ok": True, "name": args[1], "then": args[2]}]
        if args[:2] == ["provider", "use"]:
            return 0, [{"ok": True, "provider": args[2], "model": "openrouter/a", "changed": ["SHAFT_JUDGE_MODELS"],
                        "written": {"SHAFT_JUDGE_MODELS": "openrouter/a"}, "covers": ["instances", "judges"],
                        "off": {"coding": "no openrouter coder is configured"}, "not_live": []}]
        if args[:2] == ["provider", "check"]:
            return 1, [{"ok": True, "model": "openrouter/a", "error": None},
                       {"ok": False, "model": "openrouter/b", "error": "OpenRouter HTTP 401: no key"}]
        raise AssertionError(f"unexpected JSON verb {args}")

    def run_now(self, args, timeout=actions.RUN_NOW_S, who="top", input=None):
        self.record(args, timeout, who, input)
        if args[0] in self.refuse:
            return self.refuse[args[0]]
        if args[0] == "drive":       # the real verb, in this process: it checks --expect-sha against the file
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = opconfig.drive_command(args[1], args[2], args[args.index("--file") + 1],
                                              args[args.index("--expect-sha") + 1])
            lines = out.getvalue().strip().splitlines()
            return code == 0, lines[-1] if lines else ""
        if args[0] == "share":
            core.set_shared_folder(args[1], None if args[2] == "off" else args[2])
        if args[0] == "mail" and args[2] in ("grant", "grant-draft"):
            core.set_mail_grants(args[1], core.mail_grants(args[1]) + [
                {"account": args[3], "label": args[4], "draft": args[2] == "grant-draft"}])
        if args[0] == "calendar" and args[2] in ("grant", "grant-write"):
            cal = next((c for c in CALENDARS if args[4] in (c["id"], c["title"])),
                       {"id": f"{args[4]}@group.calendar.google.com", "title": args[4]})
            # as cmd_calendar: the grant moves to the end of the stored list, the write flag kept or added
            grants = core.calendar_grants(args[1])
            prior = next((g for g in grants if (g["account"], g["calendar"]) == (args[3], cal["id"])), {})
            rest = [g for g in grants if (g["account"], g["calendar"]) != (args[3], cal["id"])]
            core.set_calendar_grants(args[1], rest + [{"account": args[3], "calendar": cal["id"], "name": cal["title"],
                                                       "write": args[2] == "grant-write" or bool(prior.get("write"))}])
            if self.during_grant and args[2] == "grant":
                hook, self.during_grant = self.during_grant, None
                hook()
        if args[0] == "gdrive" and args[2] == "grant":
            core.set_gdrive_grants(args[1], [{"account": args[3], "reach": "all" if "--all" in args else "own"}])
        return True, "done"

    def verbs(self) -> list[list[str]]:
        return [c["args"] for c in self.calls]

    def create(self, **over):
        body = {"name": "ledger", "operator": "Ada", "model": "openrouter/x/model", "drive": DRIVE, "autonomy": "own",
                **over}
        return self.api("/app/v1/setup/instance", "POST", body)


# -- create (POST /setup/instance) ------------------------------------------------------------------------------------
class CreateTest(SetupTest):
    def test_create_runs_the_cli_verbs_in_order_with_the_device_as_who(self):
        r = self.create()
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(json.loads(r.body), {"instance": "ledger", "done": ["new", "config", "drive"], "paused": True})
        ran = self.verbs()
        self.assertEqual(ran[0], ["new", "ledger", "--json", "--paused-until-first-reply", "resume"])
        self.assertEqual(ran[1], ["config", "ledger", "set", "OPERATOR_NAME=Ada", "LLM_MODEL=openrouter/x/model"])
        self.assertEqual(ran[2][:4], ["drive", "ledger", "set", "--file"])
        # the sha of the drive.md `shaft new` left (the template's), not "none": the real verb refuses a wrong one
        self.assertEqual(ran[2][-2:], ["--expect-sha", hashlib.sha256(DEFAULT_DRIVE.encode()).hexdigest()])
        self.assertEqual(len(ran), 3)                                   # the welcome is a later route (hello)
        self.assertEqual({c["who"] for c in self.calls}, {"app iphone-app"})
        self.assertEqual(self.calls[0]["timeout"], actions.SLOW_S)
        self.assertEqual((self.inst / "ledger" / "drive.md").read_text(), DRIVE + "\n")
        self.assertFalse(Path(ran[2][4]).exists(), "the drive's temporary file is removed")
        self.assertEqual(self.chat_turns("ledger"), [], "creating sends no opening")

    def chat_turns(self, name):
        chat = web_chat.from_chat(name)
        return chat["turns"] if chat else []

    def test_talk_only_stays_paused_after_the_first_reply(self):
        self.assertEqual(self.create(autonomy="talk").status, 200)
        self.assertEqual(self.verbs()[0], ["new", "ledger", "--json", "--paused-until-first-reply", "stay"])

    def test_the_default_drive_unchanged_is_not_rewritten(self):
        r = self.create(drive=DEFAULT_DRIVE)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(json.loads(r.body)["done"], ["new", "config", "drive"])
        self.assertEqual([a[0] for a in self.verbs()], ["new", "config"])

    def test_create_refusal_reports_what_was_done(self):
        self.refuse["config"] = (False, "refused: LLM_MODEL: no such model")
        err = self.error(self.create(), 409, "refused")
        self.assertEqual(err["message"], "refused: LLM_MODEL: no such model")
        self.assertEqual(err["done"], ["new"])
        self.assertEqual([a[0] for a in self.verbs()], ["new", "config"])

    def test_a_refused_new_reports_its_own_words_and_nothing_done(self):
        self.refuse["new"] = (2, [{"ok": False, "error": "instance 'ledger' already exists"}])
        err = self.error(self.create(), 409, "refused")
        self.assertEqual(err["message"], "instance 'ledger' already exists")
        self.assertEqual(err["done"], [])
        self.assertEqual(len(self.verbs()), 1)

    def test_a_refused_drive_lists_new_and_config(self):
        (self.tmpl / "drive.md").write_text(DEFAULT_DRIVE)
        self.refuse["drive"] = (False, "refused: drive.md changed since you looked: reload it")
        err = self.error(self.create(), 409, "refused")
        self.assertEqual(err["done"], ["new", "config"])
        self.assertIn("drive.md changed", err["message"])

    def test_asked_again_after_a_refused_step_it_finishes_without_a_second_new(self):
        # Bug class: `new` succeeded, `config` was refused, and every later attempt was refused "already exists".
        self.refuse["config"] = (False, "refused: LLM_MODEL: no such model")
        self.assertEqual(self.error(self.create(), 409, "refused")["done"], ["new"])
        del self.refuse["config"]
        self.calls.clear()
        r = self.create()
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(json.loads(r.body), {"instance": "ledger", "done": ["new", "config", "drive"], "paused": True})
        self.assertEqual([a[0] for a in self.verbs()], ["config", "drive"])
        self.assertEqual((self.inst / "ledger" / "drive.md").read_text(), DRIVE + "\n")
        self.assertTrue((self.sup / "first-reply-ledger").exists())          # its choice is left as it was

    def test_the_app_quitting_before_it_heard_is_finished_on_the_next_attempt(self):
        self.assertEqual(self.create().status, 200)                        # the answer the app never read
        self.calls.clear()
        r = self.create()
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual([a[0] for a in self.verbs()], ["config"])          # the drive is already that text
        self.assertEqual(json.loads(r.body)["done"], ["new", "config", "drive"])

    def test_an_existing_instance_not_waiting_for_a_first_reply_is_news_refusal(self):
        self.refuse["new"] = (2, [{"ok": False, "error": "instance 'wisp' already exists"}])
        err = self.error(self.create(name="wisp"), 409, "refused")
        self.assertEqual(err["message"], "instance 'wisp' already exists")
        self.assertEqual(self.verbs(), [["new", "wisp", "--json", "--paused-until-first-reply", "resume"]])

    def test_bad_bodies_run_nothing(self):
        for over in ({"autonomy": "sometimes"}, {"autonomy": []}, {"autonomy": {}}, {"name": 3}, {"drive": ""}, {"drive": "x" * 20001}, {"operator": ""},
                     {"model": ""}):
            with self.subTest(over=over):
                self.error(self.create(**over), 400, "bad_request")
        self.error(self.api("/app/v1/setup/instance", "POST", {"name": "ledger"}), 400, "bad_request")
        self.assertEqual(self.calls, [])


# -- the drive presets ------------------------------------------------------------------------------------------------
class DrivesTest(SetupTest):
    def test_drive_presets_have_four_and_the_improve_text_is_the_template_default(self):
        got = self.get("/app/v1/setup/drives")
        self.assertEqual([d["id"] for d in got["drives"]], ["assistant", "research", "builder", "improve"])
        self.assertEqual([d["title"] for d in got["drives"]],
                         ["Personal assistant", "Research partner", "Builder", "Just improve yourself"])
        self.assertEqual({d["id"]: d["suggests"] for d in got["drives"]},
                         {"assistant": ["mail", "calendar", "drive"], "research": ["web", "drive"],
                          "builder": ["web", "folder"], "improve": []})
        self.assertEqual(got["drives"][3]["text"], DEFAULT_DRIVE)               # read now, from the template
        for d in got["drives"][:3]:
            self.assertTrue(d["text"].startswith("You "), d["text"])          # the instance-facing voice of drive.md
            self.assertLessEqual(len(d["text"]), web_config.DRIVE_MAX)
        self.assertLessEqual({g for d in got["drives"] for g in d["suggests"]}, set(drive_presets.GRANTS))

    @unittest.skipUnless((Path(__file__).resolve().parents[2] / "template" / "drive.md").is_file(),
                         "the template worktree is not checked out")
    def test_the_improve_text_is_the_real_template_drive(self):
        real = Path(__file__).resolve().parents[2] / "template"
        with mock.patch.object(core, "TEMPLATE", real):
            self.assertEqual(drive_presets.presets()[3]["text"], (real / "drive.md").read_text())

    def test_a_missing_template_drive_is_said_not_blank(self):
        (self.tmpl / "drive.md").unlink()
        err = self.error(self.api("/app/v1/setup/drives"), 409, "refused")
        self.assertIn("drive.md", err["message"])


# -- secrets, provider, telegram ------------------------------------------------------------------------------------
class SecretTest(SetupTest):
    def log_text(self) -> str:
        path = self.sup / "supervisor.log"
        return (path.read_text() if path.exists() else "") + repr(app_api.log.call_args_list)

    def test_secret_value_never_in_argv_or_logs(self):
        # the real run_json (subprocess.run faked): what reaches argv, stdin and the supervisor's log
        seen = {}

        def fake_run(argv, **kw):
            seen.update(argv=argv, **kw)
            return mock.Mock(returncode=0, stdout='{"ok": true, "name": "OPENROUTER_API_KEY"}\n', stderr="")
        with mock.patch.object(actions, "run_json", self.real_run_json), \
                mock.patch.object(actions.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(app_api, "log") as app_log:
            r = self.api("/app/v1/setup/secret", "POST", {"name": "OPENROUTER_API_KEY", "value": KEY})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(json.loads(r.body), {"said": "OPENROUTER_API_KEY saved"})
        self.assertEqual(seen["input"], KEY + "\n")
        self.assertEqual(seen["argv"][-4:], ["secrets", "set", "OPENROUTER_API_KEY", "--json"])
        self.assertNotIn(KEY, " ".join(seen["argv"]))
        logged = (self.sup / "supervisor.log").read_text() + repr(app_log.call_args_list)
        self.assertIn("shaft secrets set OPENROUTER_API_KEY --json", logged)
        self.assertNotIn(KEY, logged)
        self.assertNotIn(KEY.encode(), r.body)

    def test_the_value_goes_on_stdin_through_run_json(self):
        r = self.api("/app/v1/setup/secret", "POST", {"name": "SCRAPEBADGER_API_KEY", "value": KEY})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(self.calls, [{"args": ["secrets", "set", "SCRAPEBADGER_API_KEY", "--json"],
                                       "timeout": actions.RUN_NOW_S, "who": "app iphone-app", "input": KEY + "\n"}])

    def test_only_provider_and_tool_keys(self):
        for name in ("SHAFT_API_KEY", "SHAFT_INBOX_KEY", "SHAFT_API_LOGINS", "TELEGRAM_TOKEN_WISP",
                     "GMAIL_WORK_CLIENT_SECRET", "PATH"):
            with self.subTest(name=name):
                err = self.error(self.api("/app/v1/setup/secret", "POST", {"name": name, "value": KEY}), 400,
                                 "bad_request")
                for allowed in app_api.SETUP_SECRETS:
                    self.assertIn(allowed, err["message"])
                self.assertNotIn(KEY, err["message"])
        self.assertEqual(self.calls, [])

    def test_a_multiline_or_empty_value_is_refused_before_the_cli(self):
        for value in ("", "a\nb", 5):
            with self.subTest(value=value):
                self.error(self.api("/app/v1/setup/secret", "POST", {"name": "ANTHROPIC_API_KEY", "value": value}),
                           400, "bad_request")
        self.assertEqual(self.calls, [])

    def test_a_refused_write_says_why_without_the_value(self):
        self.refuse["secrets"] = (1, [{"ok": False, "error": "cannot write .env: denied"}])
        err = self.error(self.api("/app/v1/setup/secret", "POST", {"name": "SHAFT_CUSTOM_API_KEY", "value": KEY}),
                         409, "refused")
        self.assertEqual(err["message"], "cannot write .env: denied")

    def test_telegram_token_on_stdin(self):
        r = self.api("/app/v1/setup/telegram", "POST", {"name": "wisp", "token": TOKEN})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(self.calls, [{"args": ["telegram", "token", "wisp"], "timeout": actions.RUN_NOW_S,
                                       "who": "app iphone-app", "input": TOKEN + "\n"}])
        self.assertNotIn(TOKEN, repr(app_api.log.call_args_list))
        self.refuse["telegram"] = (False, "not saved: Telegram refused the token (getMe: HTTP 401 Unauthorized)")
        err = self.error(self.api("/app/v1/setup/telegram", "POST", {"name": "wisp", "token": TOKEN}), 409, "refused")
        self.assertIn("Telegram refused", err["message"])
        self.error(self.api("/app/v1/setup/telegram", "POST", {"name": "nobody", "token": TOKEN}), 404, "not_found")


class ProviderTest(SetupTest):
    def test_use_passes_every_field_as_its_flag(self):
        body = {"provider": "custom", "model": "big", "judge_model": "small", "base_url": "https://host/v1",
                "price_in": 1.5, "price_out": 4, "judge_price_in": 0, "judge_price_out": 0.25, "vision": True}
        r = self.api("/app/v1/setup/provider", "POST", body)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(self.verbs(), [["provider", "use", "custom", "--json", "--model", "big", "--judge-model", "small",
                                       "--base-url", "https://host/v1", "--price-in", "1.5", "--price-out", "4",
                                       "--judge-price-in", "0", "--judge-price-out", "0.25", "--vision"]])
        self.assertEqual(self.calls[0]["timeout"], actions.SLOW_S)
        self.assertEqual(json.loads(r.body), {
            "provider": "custom", "model": "openrouter/a", "changed": ["SHAFT_JUDGE_MODELS"], "covers": ["instances", "judges"],
            "off": [{"function": "coding", "why": "no openrouter coder is configured"}], "not_live": []})

    def test_use_refusal_and_bad_fields(self):
        self.refuse["provider"] = (2, [{"ok": False, "error": "custom needs --model (the model name on the endpoint)"}])
        err = self.error(self.api("/app/v1/setup/provider", "POST", {"provider": "custom"}), 409, "refused")
        self.assertEqual(err["message"], "custom needs --model (the model name on the endpoint)")
        for body in ({"provider": "grok"}, {"provider": "custom", "price_in": "1"}, {"provider": "custom", "vision": 1},
                     {"provider": "openrouter", "extra": 1}, {"provider": "custom", "price_in": float("nan")}):
            with self.subTest(body=body):
                self.error(self.api("/app/v1/setup/provider", "POST", body), 400, "bad_request")

    def test_check_returns_every_model_row(self):
        r = self.api("/app/v1/setup/provider/check", "POST", {"provider": "openrouter"})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(self.verbs(), [["provider", "check", "openrouter", "--json"]])
        self.assertEqual(self.calls[0]["timeout"], actions.SLOW_S * 2)
        self.assertEqual(json.loads(r.body), {"provider": "openrouter", "ok": False, "models": [
            {"ok": True, "model": "openrouter/a", "error": None},
            {"ok": False, "model": "openrouter/b", "error": "OpenRouter HTTP 401: no key"}]})

    def test_check_that_could_not_run_is_refused_in_its_words(self):
        self.refuse["provider"] = (1, [{"ok": False, "error": "cannot read settings: denied"}])
        err = self.error(self.api("/app/v1/setup/provider/check", "POST", {"provider": "codex"}), 409, "refused")
        self.assertEqual(err["message"], "cannot read settings: denied")


# -- how it works alone (POST /setup/autonomy/<n>) --------------------------------------------------------------------
class AutonomyTest(SetupTest):
    def test_the_choice_runs_first_reply_as_the_device(self):
        for then in ("stay", "resume"):
            with self.subTest(then=then):
                self.calls.clear()
                r = self.api("/app/v1/setup/autonomy/wisp", "POST", {"then": then})
                self.assertEqual(r.status, 200, r.body)
                self.assertEqual(json.loads(r.body), {"instance": "wisp", "then": then})
                self.assertEqual(self.calls, [{"args": ["first-reply", "wisp", then, "--json"],
                                               "timeout": actions.RUN_NOW_S, "who": "app iphone-app", "input": None}])

    def test_after_the_first_reply_it_is_refused_in_the_verbs_words(self):
        self.refuse["first-reply"] = (1, [{"ok": False, "error": "wisp is not waiting for its first reply"}])
        err = self.error(self.api("/app/v1/setup/autonomy/wisp", "POST", {"then": "stay"}), 409, "refused")
        self.assertEqual(err["message"], "wisp is not waiting for its first reply")

    def test_bad_bodies_and_unknown_instances_run_nothing(self):
        for body in ({}, {"then": "own"}, {"then": "talk"}, {"then": True}, {"then": "stay", "extra": 1}):
            with self.subTest(body=body):
                self.error(self.api("/app/v1/setup/autonomy/wisp", "POST", body), 400, "bad_request")
        self.error(self.api("/app/v1/setup/autonomy/nobody", "POST", {"then": "stay"}), 404, "not_found")
        self.assertEqual(self.calls, [])


# -- access ------------------------------------------------------------------------------------------------------------
class AccessTest(SetupTest):
    def grant(self, **body):
        return self.api("/app/v1/access/wisp", "POST", body)

    def test_get_shape(self):
        got = self.get("/app/v1/access/wisp")
        self.assertEqual(got, {"instance": "wisp", "folder": None, "browser": {"installed": "1.64.0", "run": True},
                               "web_search": False, "telegram": False, "mail": [], "calendar": [], "drive": [],
                               "chat": [], "google_accounts": []})

    def test_each_grant_runs_its_verb(self):
        folder = tempfile.mkdtemp(prefix="shaft-share-")
        self.addCleanup(shutil.rmtree, folder, True)
        cases = [({"grant": "folder", "action": "grant", "what": folder}, [["share", "wisp", folder]]),
                 ({"grant": "folder", "action": "revoke"}, [["share", "wisp", "off"]]),
                 ({"grant": "mail", "action": "grant", "alias": "work", "what": "Assistant"},
                  [["mail", "wisp", "grant", "work", "Assistant"]]),
                 ({"grant": "mail", "action": "grant", "alias": "work", "what": "Assistant", "writable": True},
                  [["mail", "wisp", "grant-draft", "work", "Assistant"]]),
                 ({"grant": "mail", "action": "revoke", "alias": "work", "what": "Assistant"},
                  [["mail", "wisp", "revoke", "work", "Assistant"]]),
                 ({"grant": "calendar", "action": "revoke", "alias": "work", "what": "primary"},
                  [["calendar", "wisp", "revoke", "work", "primary"]]),
                 ({"grant": "drive", "action": "grant", "alias": "work"}, [["gdrive", "wisp", "grant", "work"]]),
                 ({"grant": "drive", "action": "grant", "alias": "work", "all": True},
                  [["gdrive", "wisp", "grant", "work", "--all"]]),
                 ({"grant": "drive", "action": "revoke", "alias": "work"}, [["gdrive", "wisp", "revoke", "work"]]),
                 ({"grant": "chat", "action": "grant", "alias": "work"}, [["gchat", "wisp", "grant", "work"]]),
                 ({"grant": "chat", "action": "revoke", "alias": "work"}, [["gchat", "wisp", "revoke", "work"]])]
        for body, want in cases:
            with self.subTest(body=body):
                self.calls.clear()
                r = self.grant(**body)
                self.assertEqual(r.status, 200, r.body)
                self.assertEqual(self.verbs(), want)
                self.assertEqual({c["who"] for c in self.calls}, {"app iphone-app"})
                self.assertEqual(set(json.loads(r.body)), set(self.get("/app/v1/access/wisp")))   # the fresh access

    def google_work(self):
        google._save_account("work", {"address": "me@example.com", "refresh_token": "r", "client": "work",
                                      "scope": f"{google.CALENDAR} {google.CALENDAR_EVENTS}"})
        return mock.patch.object(calendar, "calendars", return_value=CALENDARS)

    def test_a_writable_calendar_is_granted_then_upgraded_by_its_resolved_id(self):
        with self.google_work():
            r = self.grant(grant="calendar", action="grant", alias="work", what="Team, \"core\"", writable=True)
        self.assertEqual(r.status, 200, r.body)
        team = CALENDARS[0]["id"]
        self.assertEqual(self.verbs(), [["calendar", "wisp", "grant", "work", team],
                                        ["calendar", "wisp", "grant-write", "work", team]])
        self.assertEqual(json.loads(r.body)["calendar"], [{"account": "work", "calendar": team,
                                                           "name": "Team, \"core\"", "write": True}])

    def test_a_concurrent_grant_on_the_same_account_never_gets_this_requests_write(self):
        # Bug: the upgrade took the account's LAST stored grant; another request's grant landing between this one's
        # read grant and its upgrade made Family writable instead of Team.
        team, family = CALENDARS[0]["id"], CALENDARS[1]["id"]
        self.during_grant = lambda: self.run_now(["calendar", "wisp", "grant", "work", "Family"], who="app other")
        with self.google_work():
            r = self.grant(grant="calendar", action="grant", alias="work", what="Team, \"core\"", writable=True)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual([a for a in self.verbs() if a[2] == "grant-write"],
                         [["calendar", "wisp", "grant-write", "work", team]])
        self.assertEqual({g["calendar"]: g.get("write", False) for g in core.calendar_grants("wisp")},
                         {team: True, family: False})

    def test_the_web_access_page_upgrades_the_calendar_it_granted_too(self):
        from supervisor import web_config
        team, family = CALENDARS[0]["id"], CALENDARS[1]["id"]
        self.during_grant = lambda: self.run_now(["calendar", "wisp", "grant", "work", "Family"], who="app other")
        with self.google_work():
            status, _ = web_config.run("calendar", "wisp", {"op": "grant", "alias": "work", "what": "Team, \"core\"",
                                                             "writable": "1"})
        self.assertEqual(status, 200)
        self.assertEqual({g["calendar"]: g.get("write", False) for g in core.calendar_grants("wisp")},
                         {team: True, family: False})

    def test_a_writable_calendar_that_does_not_resolve_is_refused_before_any_grant(self):
        with self.google_work():
            err = self.error(self.grant(grant="calendar", action="grant", alias="work", what="Nope", writable=True),
                             409, "refused")
        self.assertIn("has no calendar 'Nope'", err["message"])
        self.assertEqual(self.calls, [])

    def test_grants_show_in_the_access(self):
        self.grant(grant="mail", action="grant", alias="work", what="Assistant", writable=True)
        self.grant(grant="drive", action="grant", alias="work", all=True)
        got = self.get("/app/v1/access/wisp")
        self.assertEqual(got["mail"], [{"account": "work", "label": "Assistant", "draft": True}])
        self.assertEqual(got["drive"], [{"account": "work", "reach": "all"}])

    def test_browser_installs_once_and_needs_run(self):
        self.browser_version = None
        r = self.grant(grant="browser", action="grant")
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(self.verbs(), [["browser", "install"]])          # run is on: nothing to switch
        self.assertEqual(self.calls[0]["timeout"], app_api.BROWSER_INSTALL_S)
        self.browser_version = "1.64.0"
        core.set_tool("wisp", "run", False)
        self.calls.clear()
        self.assertEqual(self.grant(grant="browser", action="grant").status, 200)
        self.assertEqual(self.verbs(), [["tools", "wisp", "on", "run"]])
        self.calls.clear()
        err = self.error(self.grant(grant="browser", action="revoke"), 409, "refused")
        self.assertIn("shared", err["message"])
        self.assertEqual(self.calls, [], "revoking the browser never switches run off")

    def test_a_refused_verb_is_409_in_its_words(self):
        self.refuse["gchat"] = (False, "refused: Google account 'work' was not granted chat.messages.readonly")
        err = self.error(self.grant(grant="chat", action="grant", alias="work"), 409, "refused")
        self.assertIn("chat.messages.readonly", err["message"])

    def test_bad_access_bodies(self):
        for body in ({"grant": "everything", "action": "grant"}, {"grant": [], "action": "grant"},
                     {"grant": {}, "action": "grant"}, {"grant": "drive", "action": [], "alias": "work"}, {"grant": "mail", "action": "grant", "alias": "work"},
                     {"grant": "mail", "action": "grant", "alias": "Bad Alias", "what": "x"},
                     {"grant": "folder", "action": "grant"}, {"grant": "drive", "action": "maybe", "alias": "work"},
                     {"grant": "drive", "action": "grant", "alias": "work", "all": "yes"}):
            with self.subTest(body=body):
                self.error(self.grant(**body), 400, "bad_request")
        self.assertEqual(self.calls, [])
        self.error(self.api("/app/v1/access/nobody"), 404, "not_found")

    def test_google_readers(self):
        err = self.error(self.api("/app/v1/google/work/labels"), 409, "refused")
        self.assertIn("not logged in", err["message"])
        google._save_account("work", {"address": "me@example.com", "refresh_token": "r", "client": "work",
                                      "scope": f"{google.GMAIL} {google.CALENDAR}"})
        with mock.patch.object(mail, "labels", return_value={"Zeta": "L2", "Assistant": "L1"}) as labels:
            self.assertEqual(self.get("/app/v1/google/work/labels"), {"alias": "work", "labels": ["Assistant", "Zeta"]})
        self.assertEqual(labels.call_args.args[0].alias, "work")
        cals = [{"id": "me@example.com", "title": "me@example.com", "summary": "", "primary": True, "access": "owner"}]
        with mock.patch.object(calendar, "calendars", return_value=cals):
            self.assertEqual(self.get("/app/v1/google/work/calendars"), {"alias": "work", "calendars": [
                {"id": "me@example.com", "title": "me@example.com", "primary": True, "access": "owner"}]})
        with mock.patch.object(mail, "labels", side_effect=google.ApiError(403, "insufficient scope")):
            err = self.error(self.api("/app/v1/google/work/labels"), 409, "refused")
        self.assertIn("HTTP 403", err["message"])
        google._save_account("home", {"address": "h@example.com", "refresh_token": "r", "client": "home", "scope": ""})
        err = self.error(self.api("/app/v1/google/home/calendars"), 409, "refused")
        self.assertIn("calendar.readonly", err["message"])


# -- the welcome (POST /setup/hello/<n>) ------------------------------------------------------------------------------
class HelloTest(SetupTest):
    def setUp(self):
        super().setUp()
        (self.inst / "wisp" / "config.env").write_text('OPERATOR_NAME="Ada"\n')
        (self.inst / "wisp" / "drive.md").write_text(DRIVE + "\n")

    def test_opening_text_names_the_operator_the_drive_and_the_access(self):
        core.set_mail_grants("wisp", [{"account": "work", "label": "Assistant"}])
        core.set_gdrive_grants("wisp", [{"account": "work", "reach": "own"}])
        self.env["SCRAPEBADGER_API_KEY"] = KEY
        text = setup.opening_text("wisp")
        self.assertIn("Ada", text)
        self.assertIn(DRIVE, text)
        self.assertIn("Gmail label Assistant (work)", text)
        self.assertIn("Drive of work: its own files", text)
        self.assertIn("web search", text)
        self.assertIn("two or three questions", text)
        self.assertNotIn(KEY, text)                                         # a key's value is never in it

    def test_nothing_granted_is_said(self):
        self.browser_version = None
        text = setup.opening_text("wisp")
        self.assertIn("Nothing else is granted yet", text)

    def test_hello_sends_the_hidden_opening_once(self):
        with mock.patch.object(web_chat, "opening", wraps=web_chat.opening) as opening:
            r = self.api("/app/v1/setup/hello/wisp", "POST", {})
        self.assertEqual(r.status, 200, r.body)
        got = json.loads(r.body)
        chat = web_chat.from_chat("wisp")
        self.assertEqual(got, {"said": "wisp is writing its welcome", "chat": {"id": chat["id"], "title": "wisp"}})
        name, login, text = opening.call_args.args
        self.assertEqual((name, login), ("wisp", ME.lower()))
        self.assertEqual(text, setup.opening_text("wisp"))
        self.assertTrue(chat["turns"][0]["hidden"])
        feed = self.get("/app/v1/from/wisp")                   # the operator never sees the opening in Main
        self.assertNotIn("SHAFT's setup", json.dumps(feed))
        self.assertEqual([i for i in feed["items"] if i["type"] == "user"], [])
        err = self.error(self.api("/app/v1/setup/hello/wisp", "POST", {}), 409, "refused")
        self.assertIn("conversation already", err["message"])
        self.error(self.api("/app/v1/setup/hello/nobody", "POST", {}), 404, "not_found")


# -- who may call them ------------------------------------------------------------------------------------------------
ROUTES = [("POST", "/app/v1/setup/instance"), ("GET", "/app/v1/setup/drives"), ("POST", "/app/v1/setup/secret"),
          ("POST", "/app/v1/setup/provider"), ("POST", "/app/v1/setup/provider/check"),
          ("POST", "/app/v1/setup/telegram"), ("POST", "/app/v1/setup/hello/wisp"),
          ("POST", "/app/v1/setup/autonomy/wisp"), ("GET", "/app/v1/access/wisp"),
          ("POST", "/app/v1/access/wisp"), ("GET", "/app/v1/google/work/labels"),
          ("GET", "/app/v1/google/work/calendars")]


class BearerTest(LocalTest):
    def test_routes_need_the_bearer_on_both_tailnet_and_local(self):
        with mock.patch.object(actions, "run_json") as rj, mock.patch.object(actions, "run_now") as rn:
            for method, path in ROUTES:
                with self.subTest(path=path):
                    self.error(self.api(path, method, data={}, bearer=False), 401, "unpaired")
                    self.error(self.local_api(path, method, data={}, bearer=False), 401, "unpaired")
                    self.error(self.local_api(path, method, data={}, bearer=f"Bearer {self.dev}.wrong"), 401,
                               "unpaired")
            self.assertEqual((rj.call_count, rn.call_count), (0, 0))
        # with the bearer, on loopback, a route answers (as the device)
        self.assertEqual(self.local_api("/app/v1/setup/drives").status, 200)


# -- the fixtures the Swift side decodes -------------------------------------------------------------------------------
class SetupFixturesTest(SetupTest):
    def test_write_or_check_the_setup_fixtures(self):
        out = {}
        r = self.create()
        out["setup-instance"] = json.loads(r.body)
        out["setup-drives"] = self.get("/app/v1/setup/drives")         # the template's drive.md is this test's
        out["setup-provider"] = self.get("/app/v1/setup/provider", method="POST", data={"provider": "openrouter"})
        out["setup-check"] = self.get("/app/v1/setup/provider/check", method="POST", data={"provider": "openrouter"})
        out["setup-autonomy"] = self.get("/app/v1/setup/autonomy/wisp", method="POST", data={"then": "stay"})
        with mock.patch.object(web_chat.secrets, "token_hex", return_value="00000000000000a1"):      # its chat id
            out["setup-hello"] = self.get("/app/v1/setup/hello/wisp", method="POST", data={})
        google._save_account("work", {"address": "me@example.com", "refresh_token": "r", "client": "work",
                                      "scope": f"{google.GMAIL} {google.CALENDAR} {google.DRIVE_READONLY}"})
        core.set_mail_grants("wisp", [{"account": "work", "label": "Assistant", "draft": True}])
        core.set_calendar_grants("wisp", [{"account": "work", "calendar": "me@example.com", "name": "me@example.com",
                                           "write": True}])
        core.set_gdrive_grants("wisp", [{"account": "work", "reach": "own"}])
        core.set_gchat_grants("wisp", [{"account": "work"}])
        out["access"] = self.get("/app/v1/access/wisp")
        with mock.patch.object(mail, "labels", return_value={"Assistant": "L1", "Receipts": "L2"}):
            out["google-labels"] = self.get("/app/v1/google/work/labels")
        with mock.patch.object(calendar, "calendars", return_value=[
                {"id": "me@example.com", "title": "me@example.com", "summary": "", "primary": True, "access": "owner"},
                {"id": "team@group.calendar.google.com", "title": "Team", "summary": "Team", "primary": False,
                 "access": "reader"}]):
            out["google-calendars"] = self.get("/app/v1/google/work/calendars")
        self.calls.clear()
        self.refuse["config"] = (False, "refused: LLM_MODEL: no such model")
        refused = self.create(name="other")
        bad = self.api("/app/v1/setup/secret", "POST", {"name": "SHAFT_API_KEY", "value": "x"})
        out["setup-errors"] = {"refused": {"status": refused.status, "body": json.loads(refused.body)},
                               "bad_request": {"status": bad.status, "body": json.loads(bad.body)}}
        write_or_check_fixtures(self, out)


if __name__ == "__main__":
    unittest.main()
