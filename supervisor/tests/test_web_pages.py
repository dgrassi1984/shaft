"""The web UI's read-only pages (PLAN-webui.md M1): each shows what its `shaft top` tab shows, from the same panel
reader and the same fixtures as test_control_panel (the strings asserted there are asserted here), and
attachments are served only as the plan says — only files a message names, no links, never as a page."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from supervisor import messages, panel, web, web_views
from supervisor.tests.test_control_panel import COSTS, DETAIL, JUDGMENT, TRIAL
from supervisor.tests.test_web import WebTest, snap

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class PagesTest(WebTest):
    def setUp(self):
        super().setUp()
        self.inst = Path(tempfile.mkdtemp(prefix="shaft-web-inst-")).resolve()
        self.addCleanup(shutil.rmtree, self.inst, True)
        (self.inst / "shaft" / "shaft").mkdir(parents=True)
        for p in (mock.patch.object(web, "_names", return_value=["shaft", "wren"]),
                  mock.patch.object(panel, "instance_detail", return_value=DETAIL),
                  mock.patch.object(panel, "judgments", return_value=[JUDGMENT]),
                  mock.patch.object(panel, "traits", return_value={"traits": [{"supported": "yes"}]}),
                  mock.patch.object(panel, "trials", return_value=[TRIAL]),
                  mock.patch.object(panel, "costs", return_value=COSTS),
                  mock.patch.object(panel, "battery_status", return_value={"installed": False, "results": [],
                                                                            "lines": []}),
                  mock.patch.object(panel, "evals", return_value=[
                      {"kind": "distinct", "who": "wren", "at": "2026-10-13T08:39",
                       "data": {"identified": 4, "n": 4, "consistent_with_self": 4, "effectiveness_agent": 7.25,
                                "effectiveness_base": 6.25}}]),
                  mock.patch.object(panel, "surprise_summary", return_value={
                      "tool_calls": {"live": 16}, "flagged": {"live": 1}, "flagged_by_class": {"stderr": 1}}),
                  mock.patch.object(panel, "probation_events", return_value=[
                      {"ts": "2026-10-13 15:00:00", "text": "probation 74f4b7a8 failed"}]),
                  mock.patch.object(panel, "template_merges", return_value=DETAIL["merges"]),
                  mock.patch.object(panel, "services_detail", return_value=[
                      {"name": "model", "running": True, "detail": "abc", "pid": 7, "stale": True},
                      {"name": "scheduler", "running": False, "detail": "", "pid": None, "stale": False}]),
                  mock.patch.object(panel, "scheduled_runs", return_value=[]),
                  mock.patch.object(panel, "log_tail", return_value=["line one"]),
                  mock.patch("supervisor.core.INSTANCES", self.inst),
                  mock.patch("supervisor.core.instance_root", side_effect=lambda n: self.inst / n)):
            p.start()
            self.addCleanup(p.stop)
        self.cookie = self.paired()

    def get(self, path: str, status: int = 200) -> str:
        r = self.req(path, cookie=self.cookie)
        self.assertEqual(r.status, status, f"{path}: {r.body[:400]!r}")
        return r.body.decode(errors="replace")

    def has(self, path: str, *wants: str) -> str:
        body = self.get(path)
        for w in wants:
            self.assertIn(w, body, f"{path}: {w!r} missing")
        return body


class EveryPageShowsWhatItsTabShows(PagesTest):
    """The strings test_control_panel.test_every_tab_renders_its_data asserts for each TUI tab."""

    def test_instance(self):
        self.has("/i/shaft", "Recent journal", "abcdef12", "plugins/weather/main.py", "My measured performance",
                 "bonsai via local · effort medium", "Chromium 141", "watcher every 15 min")

    def test_judge(self):
        self.has("/i/shaft/judge", "Audit the skill file", "traits supported 1/1", "v2", "value", "<svg")

    def test_a_verdict_in_full(self):
        self.has("/i/shaft/judge?ts=2026-10-13T17%3A42%3A56", "Step 2 reads the full skill file.",
                 "a corrected skill file it reads every cycle", "3.0 / 5")

    def test_trials(self):
        self.has("/trials", "planted blatant harm", "worse", "W0 L6", "operator trials are not capped")

    def test_a_trial_report(self):
        with mock.patch("supervisor.trial.report", return_value="Trial t-1: worse (W0 L6)") as report:
            self.has("/trials/wren/t-1", "Trial t-1: worse (W0 L6)")
        report.assert_called_once_with(TRIAL)

    def test_benchmarks(self):
        self.has("/i/shaft/benchmarks", "not installed", "identified 4/4", "effectiveness 7.2", "16 tool calls")

    def test_changes(self):
        self.has("/i/shaft/changes", "plugins/weather/main.py", "RESTORE FAILED", "probation 74f4b7a8 failed",
                 "merge template")

    def test_costs(self):
        self.has("/costs", "Today $3.30", "$7.00 / $8.00", "Judge today $4.60", "trial $4.00")

    def test_services(self):
        self.has("/services", "older code than checked out", "NOT RUNNING", "line one", 'data-poll="/fragment/services"')
        self.has("/fragment/services", "NOT RUNNING")

    def test_an_unknown_instance_or_page_is_404(self):
        self.get("/i/nobody", 404)
        self.get("/i/shaft/nothing", 404)
        self.get("/i/shaft/judge?ts=1999-01-01T00:00:00", 404)
        self.get("/trials/wren/t-404", 404)

    def test_a_reader_that_fails_says_so_on_the_page(self):
        with mock.patch.object(panel, "costs", side_effect=OSError("cloud_usage.jsonl unreadable")):
            body = self.get("/costs", 500)
        self.assertIn("cloud_usage.jsonl unreadable", body)


class JournalTest(PagesTest):
    def journal(self, entries: list[str]) -> None:
        (self.inst / "shaft" / "memory").mkdir(parents=True, exist_ok=True)
        (self.inst / "shaft" / "memory" / "journal.md").write_text("\n---\n".join(entries))

    def test_newest_first_and_paged(self):
        self.journal([f"## 2026-10-{d:02d} entry {d}\nbody {d}" for d in range(1, 26)])
        first = self.has("/i/shaft/journal", "entry 25", "older →", "25 entries")
        self.assertLess(first.index("entry 25"), first.index("entry 24"))
        self.assertNotIn("entry 5<", first)
        self.has("/i/shaft/journal?at=20", "entry 5", "entry 1", "← newer")

    def test_markdown_never_runs_as_markup(self):
        self.journal(["## <script>alert(1)</script> title\n- **bold** and `<b>code</b>`\n"
                      "[ok](https://example.com) [bad](javascript:alert(1)) <img src=x onerror=alert(2)>"])
        body = self.get("/i/shaft/journal")
        self.assertNotIn("<script>alert", body)
        self.assertNotIn("<img src=x", body)
        self.assertNotIn('href="javascript', body)
        self.assertIn('<a href="https://example.com" rel="noopener noreferrer" target="_blank">ok</a>', body)
        self.assertIn("<b>bold</b>", body)
        self.assertIn("<code>&lt;b&gt;code&lt;/b&gt;</code>", body)


class MarkdownTest(PagesTest):
    def test_the_stats_file_renders_as_the_supervisor_wrote_it(self):
        out = web_views.markdown("# My measured performance\n\n*Written by the supervisor.*\n\n## What I achieved\n"
                                 "- Achieved: **2.2** over 20\n- My most valuable cycles:\n  - 22:21 — 4/5: fixed\n"
                                 "  - 21:47 — 4/5: public\n- Novelty: 2.2\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
        self.assertIn("<h2>My measured performance</h2>", out)
        self.assertIn("<i>Written by the supervisor.</i>", out)
        self.assertIn("<b>2.2</b>", out)
        self.assertEqual(out.count("<ul>"), 2)                      # the nested list is its own list
        self.assertIn('<td data-label="a">1</td>', out)
        self.assertEqual(out.count("<ul>"), out.count("</ul>"))

    def test_a_list_item_wrapping_onto_the_next_line_stays_one_item(self):
        out = web_views.markdown("- one long item\n  that wraps\n- two")
        self.assertEqual(out.count("<li>"), 2)
        self.assertIn("one long item that wraps", out)


class TranscriptsTest(PagesTest):
    TALK = {"id": "talk:api-1", "kind": "conversation", "ts": "2026-10-19T10:00:00", "where": "memory/episodes/a.jsonl:3",
            "turns": [{"role": "user", "content": "hi <b>", "ts": "2026-10-19T10:00:00"},
                      {"role": "assistant", "content": "hello", "reasoning": "secret plan", "ts": "2026-10-19T10:00:05"}],
            "writeups": [], "via": "api", "last": "2026-10-19T10:00:05"}
    CYCLE = {"id": "memory/episodes/a.jsonl:1", "kind": "cycle", "ts": "2026-10-19T09:00:00",
             "where": "memory/episodes/a.jsonl:1", "record": {"intent": {"goal": "tidy notes"}, "tools": [], "steps": 3,
                                                              "ended_by": "answer", "finish": {"criterion_met": "yes"}}}

    def setUp(self):
        super().setUp()
        p = mock.patch.object(panel, "transcripts", return_value=[self.TALK, self.CYCLE])
        p.start()
        self.addCleanup(p.stop)
        (self.inst / "shaft" / "config.env").write_text('OPERATOR_NAME="Daniele"\n')

    def test_the_list_and_its_filters(self):
        self.has("/i/shaft/transcripts", "2 transcripts", "tidy notes", "via api")
        only = self.get("/i/shaft/transcripts?f=cycles")
        self.assertIn("tidy notes", only)
        self.assertNotIn("via api", only)

    def test_a_conversation_hides_its_thinking_until_asked(self):
        url = "/i/shaft/transcript?id=talk%3Aapi-1"
        body = self.has(url, "hello", "hi &lt;b&gt;", "show thinking")
        self.assertNotIn("secret plan", body)
        self.has(url + "&thinking=1", "(thinking) secret plan", "hide thinking")


class MessagesTest(PagesTest):
    def setUp(self):
        super().setUp()
        self.inbox = self.sup / "operator-inbox" / "attachments" / "m1"
        self.inbox.mkdir(parents=True)
        (self.inbox / "notes.md").write_text("# prep\n<script>x</script>")
        (self.inbox / "photo.png").write_bytes(PNG)
        (self.inbox / "evil.svg").write_text('<svg onload="alert(1)"/>')
        (self.inbox / "fake.png").write_text("<html><script>alert(1)</script></html>")
        os.symlink("/etc/hosts", self.inbox / "link.txt")
        atts = [{"name": n, "path": f".supervisor/operator-inbox/attachments/m1/{n}", "bytes": 10}
                for n in ("notes.md", "photo.png", "evil.svg", "fake.png", "link.txt")]
        atts += [{"name": "up.txt", "path": ".supervisor/operator-inbox/attachments/../../.env", "bytes": 1},
                 {"name": "abs.txt", "path": "/etc/hosts", "bytes": 1},
                 {"name": "other.txt", "path": ".supervisor/web/devices.json", "bytes": 1}]
        self.ms = [{"ts": "2026-10-19T08:41:25", "from": "thelos", "to": "operator", "kind": "note", "read": True,
                    "file": "m1.md", "text": "prep for tomorrow\nsecond line", "attachments": atts},
                   {"ts": "2026-10-19T09:00:00", "from": "wren", "to": "shaft", "kind": "question", "read": False,
                    "file": "m2.md", "text": "are you <there>?"}]
        for p in (mock.patch.object(messages, "history", return_value=self.ms),
                  mock.patch("supervisor.core.PROJECT", self.sup.parent)):
            p.start()
            self.addCleanup(p.stop)

    def test_the_list_newest_first_with_a_filter(self):
        body = self.has("/messages", "thelos → operator", "wren → shaft", "prep for tomorrow", "are you &lt;there&gt;?",
                        "📎8")
        self.assertLess(body.index("wren → shaft"), body.index("thelos → operator"))
        only = self.get("/messages?who=thelos")
        self.assertNotIn("wren → shaft", only)

    def test_a_message_in_full_with_its_attachments(self):
        body = self.has("/messages/m1.md", "second line", "photo.png", '<img class="attachment" src="/files/m1.md/1"',
                        "# prep", "&lt;script&gt;x&lt;/script&gt;")
        self.assertNotIn("<script>x", body)

    def test_an_image_is_served_inline_by_its_bytes(self):
        r = self.req("/files/m1.md/1", cookie=self.cookie)
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, "image/png"))
        self.assertTrue(r.headers["Content-Disposition"].startswith("inline"))

    def test_anything_else_is_a_download_whatever_its_name_says(self):
        for k in (0, 2, 3):                       # notes.md, evil.svg, fake.png (HTML named .png)
            r = self.req(f"/files/m1.md/{k}", cookie=self.cookie)
            self.assertEqual(r.status, 200, k)
            self.assertEqual(r.headers["Content-Type"], "application/octet-stream", k)
            self.assertTrue(r.headers["Content-Disposition"].startswith("attachment"), k)
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_links_and_paths_outside_the_attachment_folders_are_refused(self):
        for k in (4, 5, 6, 7):                    # a symlink, .., an absolute path, another .supervisor file
            self.assertEqual(self.req(f"/files/m1.md/{k}", cookie=self.cookie).status, 404, k)

    def test_an_absolute_path_inside_the_attachment_folders_is_served_and_any_other_is_not(self):
        """store_attachments records an absolute path when the folder is not under the project (live, 2026-10-19)."""
        a = {"name": "photo.png", "path": str(self.inbox / "photo.png"), "bytes": 40}
        self.assertEqual(web.attachment_path(a), (self.inbox / "photo.png").resolve())
        self.assertIsNone(web.attachment_path({"name": "h", "path": "/etc/hosts"}))
        self.assertIsNone(web.attachment_path({"name": "l", "path": str(self.inbox / "link.txt")}))

    def test_only_a_file_a_message_names_is_served(self):
        self.assertEqual(self.req("/files/m1.md/99", cookie=self.cookie).status, 404)
        self.assertEqual(self.req("/files/nope.md/0", cookie=self.cookie).status, 404)
        self.assertEqual(self.req("/files/m2.md/0", cookie=self.cookie).status, 404)

    def test_files_need_a_paired_browser(self):
        self.assertEqual(self.req("/files/m1.md/1").status, 403)


class ActivityTest(PagesTest):
    def test_plugins_jobs_and_connections(self):
        plug = self.inst / "shaft" / "plugins" / "weather"
        plug.mkdir(parents=True)
        (plug / "plugin.json").write_text(json.dumps({"name": "weather", "description": "the <b>forecast</b>",
                                                     "schedule": {"every_minutes": 60}}))
        with mock.patch("supervisor.jobs.describe", return_value="shaft: jobs on\n  weather: every 60 min"), \
                mock.patch("supervisor.mind.describe", return_value="12 commits, last 1 hour ago"), \
                mock.patch("supervisor.egress.log_path", return_value=self.sup / "egress.jsonl"):
            (self.sup / "egress.jsonl").write_text(json.dumps({"ts": "2026-10-19T10:00:00", "host": "pypi.org",
                                                               "port": 443, "up": 10, "down": 20}) + "\n")
            self.has("/i/shaft/activity", "weather", "the &lt;b&gt;forecast&lt;/b&gt;", "every 60 min",
                     "pypi.org:443", "12 commits")


class NavTest(PagesTest):
    def test_every_page_links_every_other(self):
        body = self.get("/i/shaft")
        for href in ("/i/shaft/judge", "/i/shaft/changes", "/i/shaft/journal", "/i/shaft/activity",
                     "/i/shaft/transcripts", "/i/shaft/benchmarks", "/trials", "/messages", "/costs", "/services",
                     'href="/i/wren"'):
            self.assertIn(href, body)

    def test_the_summary_refreshes_its_live_part(self):
        with mock.patch.object(web, "overview", return_value=snap(name="shaft", state="in a cycle", since="12:00")):
            self.has("/i/shaft", 'data-poll="/fragment/i/shaft/live"')
            self.has("/fragment/i/shaft/live", "in a cycle since 12:00")


class NoMarkupShownAsTextTest(PagesTest):
    """The class of bug: markup escaped twice shows as text (the instance page showed its journal as raw <ul><li>, M2
    2026-10-19). No page may contain escaped tags of its own making."""
    OWN = ("&lt;ul", "&lt;li", "&lt;span", "&lt;div", "&lt;a href", "&lt;table", "&lt;section", "&lt;form", "&lt;p")

    def test_no_page_shows_its_own_markup_escaped(self):
        with mock.patch.object(panel, "transcripts", return_value=[]):
            for path in ("/", "/fragment/overview", "/i/shaft", "/i/shaft/judge", "/i/shaft/changes",
                         "/i/shaft/benchmarks", "/i/shaft/transcripts", "/i/shaft/journal", "/trials", "/costs",
                         "/services", "/fragment/services", "/messages"):
                body = self.get(path)
                for tag in self.OWN:
                    self.assertNotIn(tag, body, f"{path} shows {tag}")

    def test_the_summary_lists_its_journal_headings(self):
        body = self.get("/i/shaft")
        self.assertIn('<li><span class="clip">2026-10-13 17:32 — Conversation with Daniele</span></li>', body)

    def test_adding_to_html_escapes_the_text_side_only(self):
        from supervisor.web_pages import Html
        self.assertEqual(Html("<b>") + "<i>", "<b>&lt;i&gt;")
        self.assertEqual("<i>" + Html("<b>"), "&lt;i&gt;<b>")
        self.assertIsInstance("x" + Html("<b>"), Html)
        self.assertEqual(Html("<b>") + Html("<i>"), "<b><i>")
