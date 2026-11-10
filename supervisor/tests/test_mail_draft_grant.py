"""Mail draft grants preserve reads and require separate compose consent."""
import copy
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from supervisor import core, google, web, web_config
from supervisor import __main__ as cli
from supervisor.tests.test_mail import FakeGmail
from supervisor.tests.test_web_config import ACCESS


class Forms(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = (attrs, {})
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and "name" in attrs:
            self.current[1][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


class MailDraftGrantTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-draft-grants-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.gmail = FakeGmail()
        self.env = {"GMAIL_WORK_CLIENT_ID": "cid", "GMAIL_WORK_CLIENT_SECRET": "csecret",
                    "GMAIL_WORK_WEB_CLIENT_ID": "web-id", "GMAIL_WORK_WEB_CLIENT_SECRET": "web-secret"}
        for patch in (mock.patch.object(core, "SUP_DIR", self.root / ".supervisor"),
                      mock.patch.object(core, "secrets_env", return_value=self.env),
                      mock.patch.object(cli, "instance_root", return_value=self.root),
                      mock.patch.object(cli, "log"),
                      mock.patch.object(google, "urlopen", self.gmail),
                      mock.patch.object(web, "url", return_value="https://shaft.test")):
            patch.start()
            self.addCleanup(patch.stop)
        self.saved = {"address": "operator@work.example", "client": "work", "refresh_token": "rt",
                      "scope": " ".join(google.SCOPES)}
        google._save_account("work", self.saved)
        self.read = {"account": "work", "label": "AssistantAccess"}
        self.draft = {**self.read, "draft": True}

    def command(self, action=None, label="AssistantAccess"):
        out = io.StringIO()
        args = ["mail", "example"] + ([action, "work", label] if action else [])
        with redirect_stdout(out), redirect_stderr(out):
            status = cli.main(args)
        return status, out.getvalue()

    def test_grant_draft_implies_read_and_show_marks_drafts(self):
        status, output = self.command("grant-draft")
        self.assertEqual(status, 0, output)
        self.assertEqual(core.mail_grants("example"), [self.draft])
        self.assertIn("example may read: work/AssistantAccess [drafts]", self.command()[1])

    def test_grant_draft_requires_existing_label(self):
        status, output = self.command("grant-draft", "MissingLabel")
        self.assertEqual(status, 2, output)
        self.assertIn("no label", output)
        self.assertEqual(core.mail_grants("example"), [])

    def test_missing_compose_refuses_without_changing_read(self):
        self.saved["scope"] = google.GMAIL
        google._save_account("work", self.saved)
        core.set_mail_grants("example", [self.read])
        status, output = self.command("grant-draft")
        self.assertEqual(status, 2)
        self.assertIn(google.missing_scope_message("work", self.saved, google.GMAIL_COMPOSE), output)
        self.assertIn("gmail.compose", output)
        self.assertEqual(core.mail_grants("example"), [self.read])
        self.assertEqual(self.gmail.requests, [])

    def test_revoke_draft_keeps_read_without_google(self):
        core.set_mail_grants("example", [self.draft])
        self.assertEqual(self.command("revoke-draft")[0], 0)
        self.assertEqual(core.mail_grants("example"), [self.read])
        self.assertEqual(self.gmail.requests, [])
        self.assertNotIn("[drafts]", self.command()[1])

    def test_plain_grant_keeps_draft(self):
        core.set_mail_grants("example", [self.draft])
        self.assertEqual(self.command("grant")[0], 0)
        self.assertEqual(core.mail_grants("example"), [self.draft])

    def test_revoke_removes_drafted_grant_only(self):
        other = {"account": "work", "label": "Other"}
        core.set_mail_grants("example", [self.draft, other])
        self.assertEqual(self.command("revoke")[0], 0)
        self.assertEqual(core.mail_grants("example"), [other])
        self.assertEqual(self.gmail.requests, [])

    def test_web_toggles_submit_cli_operation_and_label(self):
        for grant, op, label, state in ((self.read, "grant-draft", "Allow drafts", "read only"),
                                        (self.draft, "revoke-draft", "Read only", "drafts")):
            with self.subTest(op=op):
                access = copy.deepcopy(ACCESS)
                access["mail"] = [grant]
                body = str(web_config.access_page("example", access))
                self.assertIn(f">{label}</button>", body)
                self.assertIn(f"<span>{state}</span>", body)
                attrs, form = next((attrs, form) for attrs, form in Forms(body).forms
                                   if attrs.get("data-post") == "/action/mail" and form.get("op") == op)
                self.assertTrue(attrs["data-confirm"])
                with mock.patch.object(web_config.actions, "run_now", return_value=(True, "ok")) as run:
                    self.assertEqual(web_config.run("mail", "example", form), (200, "ok"))
                self.assertEqual(run.call_args.args[0], ["mail", "example", op, "work", "AssistantAccess"])

    def test_missing_only_compose_keeps_reads_and_explains_drafting(self):
        self.saved["scope"] = " ".join(s for s in google.SCOPES if s != google.GMAIL_COMPOSE)
        google._save_account("work", self.saved)
        account = google.Account("work", self.env)
        for scope in (google.GMAIL, google.CALENDAR, google.DRIVE_READONLY,
                      google.CHAT_SPACES, google.CHAT_MESSAGES, google.CHAT_MEMBERS):
            self.assertIs(account.require(scope), account)
        self.assertEqual(self.command("grant")[0], 0)
        body = str(web_config.google_accounts("example", {"work": self.saved}))
        self.assertIn("Drafting needs Log in again.", body)
        self.assertIn("lacks gmail.compose", body)
        for scope in ("gmail.readonly", "calendar.readonly", "drive.readonly", "chat.messages.readonly"):
            self.assertNotIn("lacks " + scope, body)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(google, "login", return_value=self.saved), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.cmd_google("login", "work", self.saved["address"], "work"), 0)
        self.assertIn("Drafting needs Log in again.", err.getvalue())
        self.assertNotIn("the tools that need it refuse", err.getvalue())
