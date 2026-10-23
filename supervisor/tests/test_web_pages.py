"""The web UI's read-only pages (docs/plans/PLAN-webui.md M1): each shows what its `shaft top` tab shows, from the same panel
reader and the same fixtures as test_control_panel (the strings asserted there are asserted here), and
attachments are served only as the plan says — only files a message names, no links, never as a page."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from supervisor import messages, panel, web, web_pages, web_views
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

    def test_issue_61_running_web_version_outside_polled_rail(self):
        """#61: reverting removes the running version and checkout link from shared navigation."""
        from supervisor import version
        rec = {"commit": "abc1234+changes", "describe": "v0.2.0-60-gabc1234+changes"}
        with mock.patch.object(version, "service", return_value=rec), \
             mock.patch.object(version, "commit_describe", return_value="v0.2.0-61-gdef5678"):
            for path in ("/overview", "/services", "/costs", "/i/shaft"):
                body = self.get(path)
                nav = body.split("<nav>", 1)[1].split("</nav>", 1)[0]
                self.assertIn("web: v0.2.0-60-gabc1234+changes", nav)
                self.assertNotIn("checked out:", nav)
                self.assertGreater(nav.index('class="nav-version'), nav.index("</form>"))
            with mock.patch.object(version, "supervisor_commit", return_value="def5678"):
                body = self.get("/overview")
                self.assertIn('<a href="/services">checked out: v0.2.0-61-gdef5678</a>', body)
                rec.update(commit="def5678", describe="v0.2.0-61-gdef5678")
                self.assertNotIn("checked out:", self.get("/overview"))
        self.assertNotIn("nav-version", web_pages.rail([]))
        self.assertIn("web: unknown — restart to record", self.get("/overview"))

    def test_issue_61_services_version_column_and_checkout(self):
        """#61: reverting loses the version column, scheduler version, and missing-record prompt."""
        from supervisor import version
        rows = [{"name": n, "running": True, "version": f"v0.2.0-{k}-gabc1234", "stale": True}
                for k, n in enumerate(("api", "scheduler", "telegram", "web", "model"))]
        with mock.patch.object(panel, "services_detail", return_value=rows), \
             mock.patch.object(version, "commit_describe", return_value="v0.2.0-60-gabc1234"):
            body = self.get("/fragment/services")
        self.assertEqual(body.count("checked out: v0.2.0-60-gabc1234"), 1)
        self.assertIn(">version</th>", body)
        for k in range(5):
            self.assertIn(f"v0.2.0-{k}-gabc1234", body)
        self.assertIn("unknown — restart to record", body)
        self.assertIn("older code than checked out: restart it", body)

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

    def test_issue_29_trials_list_and_report_refresh(self):
        """#29. The trials page had no data-poll, so a trial queued from it stayed off the list until a reload, and
        the open report stayed the text from when the page opened. Reverting removes the poll URL or serves the
        list and the report only as a full page."""
        page = self.get("/trials")
        self.assertIn('data-poll="/fragment/trials"', page)
        self.assertNotIn('data-poll="/fragment/trials?', page)
        frag = self.get("/fragment/trials")
        self.assertIn("planted blatant harm", frag)
        self.assertNotIn("data-poll", frag)
        self.assertNotIn("<h1>", frag)
        seen = []

        def trials(name=None):
            seen.append(name)
            return [TRIAL]

        with mock.patch.object(panel, "trials", side_effect=trials):
            filtered = self.get("/trials?i=wren")
            self.get("/fragment/trials?i=wren")
        self.assertIn('data-poll="/fragment/trials?i=wren"', filtered)
        self.assertIn("wren", seen)                               # the fragment asks for the same instance
        with mock.patch("supervisor.trial.report", return_value="queued: 0/6 replayed") as report:
            one = self.get("/trials/wren/t-1")
            one_frag = self.get("/fragment/trials/wren/t-1")
        self.assertIn('data-poll="/fragment/trials/wren/t-1"', one)
        self.assertIn("queued: 0/6 replayed", one)
        self.assertIn("queued: 0/6 replayed", one_frag)
        self.assertNotIn("data-poll", one_frag)
        self.assertNotIn("<h1>", one_frag)
        self.assertGreaterEqual(report.call_count, 2)            # the page and the fragment each render it
        self.get("/fragment/trials/wren/t-404", 404)
        self.get("/fragment/trials/nobody/t-1", 404)

    def test_issue_29_the_service_log_refreshes(self):
        """#29. Services polled its state and left the log as a snapshot, with nothing saying so. Reverting drops
        the log's poll (or folds the log into /fragment/services, which does not know which log is open). The tail
        is the last 8 KB (panel.log_tail), so the poll is cheap."""
        body = self.get("/services")
        self.assertIn('data-poll="/fragment/services"', body)
        self.assertIn('data-poll="/fragment/services/log?log=model"', body)
        frag = self.get("/fragment/services/log?log=model")
        self.assertIn("line one", frag)
        self.assertIn("com.shaft.model.log", frag)
        self.assertNotIn("data-poll", frag)
        state = self.get("/fragment/services")
        self.assertIn("NOT RUNNING", state)
        self.assertNotIn("line one", state)                       # the state poll does not carry the log
        chosen = self.get("/services?log=scheduler")
        self.assertIn('data-poll="/fragment/services/log?log=scheduler"', chosen)
        self.assertIn("com.shaft.scheduler.log", self.get("/fragment/services/log?log=scheduler"))
        self.assertIn("com.shaft.model.log", self.get("/fragment/services/log?log=nope"))  # unknown: the page's default

    def test_an_idle_model_server_is_idle_not_down(self):
        """main 86b2d72: the model server runs only while used; an idle one is not a fault (shown dim, as in the TUI)."""
        with mock.patch.object(panel, "services_detail", return_value=[
                {"name": "model", "running": False, "idle": True, "detail": "idle · starts on demand"}]):
            body = self.get("/services")
        model_row = next(r for r in body.split("<tr") if "log=model" in r)
        self.assertIn("○ idle", model_row)
        self.assertNotIn("NOT RUNNING", model_row)
        s = snap()
        s["services"] = [{"name": "model", "running": False, "idle": True, "detail": "idle · starts on demand"}]
        self.assertIn('class="svc idle"', web_pages.overview(s))

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

    def test_quoted_lines_are_one_blockquote_escaped(self):
        out = web_views.markdown("> you said **this**\n> and <script>x</script>\n\nmy answer")
        self.assertEqual(out.count("<blockquote>"), 1)
        self.assertIn("<b>this</b>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script>", out)
        self.assertIn("<p>my answer</p>", out)
        self.assertNotIn("&gt; you said", out)                     # the marker is not shown as text

    def test_a_reply_quoting_a_message_shows_the_quote_apart_from_what_he_wrote(self):
        out = web_views._my_text("> it said <b>x</b>\n> and y\n\nwhy *that*?")
        self.assertIn('<blockquote class="wrap">it said &lt;b&gt;x&lt;/b&gt;\nand y</blockquote>', out)
        self.assertIn('<div class="wrap">why *that*?</div>', out)  # his own words as written, not as Markdown
        self.assertEqual(str(web_views._my_text("plain\ntext")), '<div class="wrap">plain\ntext</div>')

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
        (self.inst / "shaft" / "config.env").write_text('OPERATOR_NAME="the operator"\n')

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
                        "<h2>prep</h2>", "&lt;script&gt;x&lt;/script&gt;")     # a .md attachment reads as Markdown
        self.assertNotIn("<script>x", body)
        self.assertNotIn("# prep", body)

    def sent_by_wren(self):
        """m3: wren's message to the operator with a Markdown file, an image, non-UTF-8 text, a link and a big file."""
        (self.inbox / "latin.txt").write_bytes("caff\xe8".encode("latin-1"))
        (self.inbox / "big.txt").write_text("x" * 300)
        atts = [{"name": n, "path": f".supervisor/operator-inbox/attachments/m1/{n}", "bytes": 10}
                for n in ("notes.md", "photo.png", "latin.txt", "link.txt", "big.txt")]
        self.ms.append({"ts": "2026-10-19T10:00:00", "from": "wren", "to": "operator", "kind": "note", "read": True,
                        "file": "m3.md", "text": "the plan", "attachments": atts})

    def test_a_text_attachment_previews_read_only_in_the_documents_panel(self):
        """Reverting the preview leaves ?att= ignored: the panel lists documents and the file only downloads."""
        self.sent_by_wren()
        body = self.has("/fragment/docs/wren?att=m3.md%2F0", "sent by wren", "📎 notes.md", "<h2>prep</h2>",
                        "&lt;script&gt;x&lt;/script&gt;", 'href="/files/m3.md/0" title="Download" aria-label="Download"><svg', "data-back-docs",
                        "data-fullscreen-doc")
        for absent in ("<script>x", "data-edit-doc", "docdelete", "data-comment-doc", "New document"):
            self.assertNotIn(absent, body)          # the message's file, not a document: nothing edits or deletes it

    def test_an_attachment_preview_says_why_it_shows_no_text(self):
        """A preview that silently showed an empty panel, or another instance's file, would be a bug."""
        self.sent_by_wren()
        self.has("/fragment/docs/wren?att=m3.md%2F1", "photo.png is not a text file", 'href="/files/m3.md/1"')
        self.has("/fragment/docs/wren?att=m3.md%2F2", "latin.txt is not UTF-8 text", 'href="/files/m3.md/2"')
        self.has("/fragment/docs/wren?att=m3.md%2F3", "link.txt is not there (any more).")
        with mock.patch.object(web, "TEXT_MAX", 100):
            self.has("/fragment/docs/wren?att=m3.md%2F4", "big.txt is too big to show here", 'href="/files/m3.md/4"')
        for att in ("m3.md%2F9", "m1.md%2F0", "m2.md%2F0", "nope.md%2F0", "m3.md%2Fx", "m3.md"):
            body = self.has(f"/fragment/docs/wren?att={att}", "wren sent you no such attachment.")
            self.assertNotIn("prep", body, att)     # m1 is thelos's message: never shown in wren's panel

    def test_a_text_attachment_in_the_chat_opens_in_the_panel_and_still_downloads(self):
        self.sent_by_wren()
        card = str(web_views._sent_cards("wren", [self.ms[-1]])[0])
        self.assertIn('<a href="/files/m3.md/0" target="_blank" data-open-att="m3.md/0">📎 notes.md</a>', card)
        self.assertIn('<a href="/files/m3.md/2" target="_blank" data-open-att="m3.md/2">📎 latin.txt</a>', card)
        self.assertNotIn('data-open-att="m3.md/1"', card)   # an image keeps its thumbnail
        page = str(web_views.chat_page("wren", None, [self.ms[-1]], [], {}, ["wren"], "csrf", att="m3.md/0",
                                       panel=web_views.attachment_panel("wren", "notes.md", "/files/m3.md/0", "# hi")))
        self.assertIn('data-poll="/fragment/docs/wren?att=m3.md%2F0"', page)
        self.assertIn("<h2>hi</h2>", page)

    def test_a_long_message_shows_whole_in_the_chat_as_markdown(self):
        """The chat card cut a message at 600 characters with no mark (83% of the messages to the operator, 2026-10-23:
        "You got cut out"), and showed its Markdown as raw asterisks."""
        tail = "Whenever you want, I'll review the recap and move its Status."
        text = "Done — **created** the skill.\n\n" + "x " * 400 + tail
        card = str(web_views._sent_cards("thelos", [{**self.ms[0], "text": text}])[0])
        shown = card.split('<div class="who">', 1)[1]           # what is on screen, not the data-raw copy for Copy/Reply
        self.assertIn(tail.replace("'", "&#x27;"), shown)
        self.assertIn("<b>created</b>", shown)

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
    def test_issue_66_reload_in_every_page_nav(self):
        """Reverting #66 removes cache recovery from page navigation."""
        with mock.patch.object(panel, "transcripts", return_value=[]):
            for path in ("/overview", "/messages", "/trials", "/costs", "/services", "/settings",
                         "/i/shaft", "/i/shaft/chat", "/i/shaft/judge", "/i/shaft/changes",
                         "/i/shaft/journal", "/i/shaft/transcripts", "/i/shaft/benchmarks"):
                # not /activity: it reads live jobs, mind and egress data (ActivityTest mocks them); same nav anyway
                body = self.get(path)
                nav = body.split("<nav>", 1)[1].split("</nav>", 1)[0]
                self.assertIn("data-hard-reload", nav, path)
                self.assertIn("Reload / update</button>", nav, path)

    def test_issue_66_reload_in_chat_menu(self):
        """Reverting #66 removes cache recovery from the phone chat menu, including archived chats."""
        for read_only in (False, True):
            header = web_views.chat_header("shaft", {}, None, [], read_only=read_only)
            menu = header.split('data-key="chat-menu"', 1)[1]
            self.assertIn("data-hard-reload", menu)
            self.assertIn("Reload / update</button>", menu)

    def test_every_page_links_every_other(self):
        body = self.get("/i/shaft")
        for href in ("/i/shaft/judge", "/i/shaft/changes", "/i/shaft/journal", "/i/shaft/activity",
                     "/i/shaft/transcripts", "/i/shaft/benchmarks", "/trials", "/messages", "/costs", "/services",
                     "/overview", "/settings", 'href="/i/wren/chat"'):
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
            for path in ("/overview", "/fragment/overview", "/fragment/rail", "/i/shaft", "/i/shaft/chat", "/i/shaft/judge", "/i/shaft/changes",
                         "/i/shaft/benchmarks", "/i/shaft/transcripts", "/i/shaft/journal", "/trials", "/costs",
                         "/services", "/fragment/services", "/fragment/services/log?log=model",
                         "/fragment/trials", "/messages"):
                body = self.get(path)
                for tag in self.OWN:
                    self.assertNotIn(tag, body, f"{path} shows {tag}")

    def test_the_summary_lists_its_journal_headings(self):
        body = self.get("/i/shaft")
        self.assertIn('<li><span class="clip">2026-10-13 17:32 — Conversation with the operator</span></li>', body)

    def test_adding_to_html_escapes_the_text_side_only(self):
        from supervisor.web_pages import Html
        self.assertEqual(Html("<b>") + "<i>", "<b>&lt;i&gt;")
        self.assertEqual("<i>" + Html("<b>"), "&lt;i&gt;<b>")
        self.assertIsInstance("x" + Html("<b>"), Html)
        self.assertEqual(Html("<b>") + Html("<i>"), "<b><i>")


class AfterHandlersTest(TestCase):
    def test_every_after_a_page_asks_for_has_a_handler(self):
        """Merge of #17 with the kill/delete work, 2026-10-20: #17 removed web.js's data-after="reload" handler while
        Unpair this browser still asked for it, so unpairing left the old page up. Every value a page emits — a
        literal data-after="…" or a button(…, after="…") — must have its handler in web.js."""
        import re
        here = Path(web_pages.__file__).parent
        source = "\n".join(p.read_text() for p in here.glob("*.py"))
        asked = set(re.findall(r'data-after="([a-z-]+)"', source)) | set(re.findall(r'\bafter="([a-z-]+)"', source))
        js = (here / "web_static" / "web.js").read_text()
        handled = set(re.findall(r'dataset\.after === "([a-z-]+)"', js))
        self.assertTrue({"reload", "home", "doc-saved"} <= asked, asked)        # the scan sees what pages emit
        self.assertEqual(asked - handled, set())


class StaticVersionTest(TestCase):
    def test_a_changed_static_file_gets_a_new_url(self):
        """It was a constant bumped by hand: web.js changed without the bump and browsers kept the old script."""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "web.js").write_text("one")
            before = web_pages._static_version(Path(d))
            (Path(d) / "web.js").write_text("two")
            self.assertNotEqual(before, web_pages._static_version(Path(d)))
        self.assertEqual(web_pages.STATIC_VERSION, web_pages._static_version())


class StylesTest(TestCase):
    def test_issue_71_the_chat_takes_the_room_of_a_closed_drawer(self):
        """#71: .chatpage and .chathead were capped at 72rem exactly when the documents were closed, so on 2560 px
        closing the drawer narrowed the chat (1141 -> 1080 px) and left 1214 px empty. Messages keep their own cap."""
        import re
        css = (Path(web_pages.__file__).parent / "web_static" / "web.css").read_text()
        for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
            if ".chatpage" in selectors or ".chathead {" in selectors + " {":
                if "hidedocs > .chatpage" in selectors or selectors.strip() == ".chathead":
                    self.assertNotIn("max-width", body, selectors.strip())
        self.assertRegex(css, r"\.turn \{[^}]*max-width: 52rem")

    def test_issue_69_touch_rules_keep_the_sidebar_rail_a_list(self):
        """#69, iPad: a (pointer: coarse) rule gave nav a.inst and the Manage links display: inline-flex, meant for the
        phone's chip bar. On a touch iPad the rail is a sidebar list: each chat's name and last line ran into one row
        and Manage ran sideways. Touch may size them (44 px), never change their display, outside the phone layout."""
        import re
        css = (Path(web_pages.__file__).parent / "web_static" / "web.css").read_text()
        for block in re.findall(r"@media \(pointer: coarse\) \{(.*?)\n\}", css, re.S):
            for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", block):
                rail = [x.strip() for x in selectors.split(",") if x.strip() in ("nav a.inst", ".rail details.manage > a")]
                self.assertFalse(rail and "display" in body, f"touch rule sets display on {rail}: {body.strip()}")
        ipad = re.search(r"@media \(pointer: coarse\) and \(min-width: 721px\) and \(min-height: 521px\) \{(.*?)\n\}",
                         css, re.S)
        self.assertRegex(ipad.group(1), r"\.rail details\.manage > a \{[^}]*display: flex")

    def test_issue_61_nav_version_wraps_at_phone_width(self):
        """#61: reverting removes the small footer style and lets long version labels overflow the phone sheet."""
        css = (Path(web_pages.__file__).parent / "web_static" / "web.css").read_text()
        self.assertRegex(css, r"nav \.nav-version \{[^}]*font-size: \.8rem;[^}]*overflow-wrap: anywhere;[^}]*order: 4;")
        self.assertRegex(css, r"nav \.nav-version a \{[^}]*display: block;")
        self.assertRegex(css, r"body\.navopen:has\(\.chatgrid\) nav \{[^}]*overflow-y: auto;")

    def test_what_the_page_hides_stays_hidden(self):
        """Found live: form.docedit { display: grid } showed the document editor the page had hidden."""
        css = (Path(web_pages.__file__).parent / "web_static" / "web.css").read_text()
        self.assertIn("[hidden] { display: none !important; }", css)

    def test_at_phone_width_the_documents_never_force_two_columns(self):
        """Codex review: 📄 Documents with no documents yet (.showdocs) kept two columns wider than a phone."""
        css = (Path(web_pages.__file__).parent / "web_static" / "web.css").read_text()
        phone = css[css.index("@media (max-width: 720px)"):]
        # on a phone the chat is one column (a flex column), and the documents a sheet over it (docs/plans/PLAN-pwa P2)
        self.assertRegex(phone, r"\.chatgrid \{ display: flex; flex-direction: column;")
        self.assertRegex(phone, r"\.chatgrid\.showdocs > aside\.docs \{ display: block; position: fixed; inset: 0;")
