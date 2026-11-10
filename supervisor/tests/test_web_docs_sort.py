"""The documents list's size, modification time and order (the operator, 2026-10-25): sizes as 1.5 KB, not "1536 bytes";
each row's modified time; Name / Size / Modified column heads that sort it, remembered by the browser."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from supervisor import web, web_chat
from supervisor.tests import test_web
from supervisor.web_views import docs_panel, size_label, when_label

from supervisor.tests.node_helpers import NODE_TIMEOUT_S

NODE = shutil.which("node")
JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"
NOW = time.mktime((2026, 10, 25, 18, 0, 0, 0, 0, -1))
# a.md: small, oldest; b.md: largest, newest; C.md: middle size and time (capital: the name order ignores case)
LISTING = [{"path": "b.md", "bytes": 5000, "mtime": NOW - 60, "sha": "b"},
           {"path": "C.md", "bytes": 300, "mtime": NOW - 3600, "sha": "c"},
           {"path": "a.md", "bytes": 10, "mtime": NOW - 86400 * 3, "sha": "a"}]


def order(html: str, names=("a.md", "b.md", "C.md")) -> list[str]:
    return sorted(names, key=lambda n: html.index(f'data-open-doc="{n}"'))


class SizeLabelTest(unittest.TestCase):
    def test_shorthand_is_one_decimal_under_ten_and_never_1024_of_a_unit(self):
        cases = {0: "0 B", 1: "1 B", 1023: "1023 B", 1024: "1 KB", 1536: "1.5 KB", 10 * 1024 - 1: "10 KB",
                 256 * 1024: "256 KB", 2**20 - 1: "1 MB", int(1.5 * 2**20): "1.5 MB", 5 * 2**30: "5 GB",
                 3 * 2**40: "3 TB", 5000 * 2**40: "5000 TB"}
        for n, want in cases.items():
            self.assertEqual(size_label(n), want, n)


class WhenLabelTest(unittest.TestCase):
    def test_today_is_a_time_this_year_a_day_and_time_older_a_date(self):
        today = time.mktime((2026, 10, 25, 9, 5, 0, 0, 0, -1))
        earlier = time.mktime((2026, 3, 4, 14, 32, 0, 0, 0, -1))
        old = time.mktime((2025, 12, 31, 23, 59, 0, 0, 0, -1))
        self.assertIn('title="2026-10-25 09:05:00">09:05</time>', str(when_label(today, NOW)))
        self.assertIn('title="2026-03-04 14:32:00">4 Mar 14:32</time>', str(when_label(earlier, NOW)))
        self.assertIn('title="2025-12-31 23:59:00">2025-12-31</time>', str(when_label(old, NOW)))
        self.assertIn('datetime="2026-10-25T09:05:00', str(when_label(today, NOW)))


class DocsSortViewTest(unittest.TestCase):
    def panel(self, sort="modified", listing=LISTING, folder=""):
        with mock.patch("time.time", return_value=NOW):
            return str(docs_panel("ada", listing, "", None, "none", set(), sort=sort, folder=folder))

    def test_each_sort_orders_the_rows(self):
        self.assertEqual(order(self.panel()), ["b.md", "C.md", "a.md"])               # newest first, the default
        self.assertEqual(order(self.panel("modified-asc")), ["a.md", "C.md", "b.md"])
        self.assertEqual(order(self.panel("name")), ["a.md", "b.md", "C.md"])         # A→Z, ignoring case
        self.assertEqual(order(self.panel("name-desc")), ["C.md", "b.md", "a.md"])
        self.assertEqual(order(self.panel("size")), ["b.md", "C.md", "a.md"])         # largest first
        self.assertEqual(order(self.panel("size-asc")), ["a.md", "C.md", "b.md"])
        self.assertEqual(order(self.panel("bogus")), ["b.md", "C.md", "a.md"])        # a hand-made value: the default

    def test_rows_show_shorthand_size_exact_bytes_and_modified_time(self):
        html = self.panel()
        self.assertIn('<span class="dim docsize" title="5,000 bytes">4.9 KB</span><time class="dim docdate"', html)
        self.assertIn('>17:59</time>', html)                                             # b.md, a minute ago: today
        self.assertNotIn(" bytes</span>", html)                                          # never "5000 bytes" any more

    def test_the_column_heads_mark_the_order_and_flip_it(self):
        html = self.panel("name")
        self.assertIn('role="group" aria-label="Sort the documents"', html)
        self.assertIn('<a href="?list=1&amp;sort=name-desc" data-sort-docs="name-desc" data-focus="sort-name" class="on" '
                      'aria-current="true" title="Sort by Name, reversed">Name ↑</a>', html)
        self.assertIn('data-sort-docs="size" data-focus="sort-bytes" class=" docsize"', html)   # first click: largest
        self.assertIn('data-sort-docs="modified" data-focus="sort-mtime" class=" docdate"', html)
        self.assertIn('Modified</a>', html)
        self.assertIn('Size ↓</a>', self.panel("size"))
        self.assertIn('data-sort-docs="size-asc"', self.panel("size"))
        self.assertIn('href="?list=1&amp;sort=modified-asc&amp;dir=ucg"', self.panel(folder="ucg",
                      listing=[dict(d, path="ucg/" + d["path"]) for d in LISTING]))   # the head keeps the folder
        self.assertNotIn("docsort", self.panel(listing=LISTING[:1]))                      # one row: nothing to sort

    def test_an_archived_chats_heads_stay_in_that_chat(self):
        with mock.patch("time.time", return_value=NOW):
            html = str(docs_panel("ada", LISTING, "", None, "none", set(), archived=True, chat_id="web-a1"))
        self.assertIn('href="?list=1&amp;sort=name&amp;id=web-a1"', html)

    def test_equal_keys_fall_back_to_the_name(self):
        same = [dict(d, bytes=7, mtime=NOW - 5) for d in LISTING]
        self.assertEqual(order(self.panel("size", same)), ["a.md", "b.md", "C.md"])
        self.assertEqual(order(self.panel("modified", same)), ["a.md", "b.md", "C.md"])

    def test_folders_come_first_sorted_by_their_documents_total_and_latest(self):
        listing = [{"path": "big/x.md", "bytes": 9000, "mtime": NOW - 7200, "sha": "x"},
                   {"path": "big/y.md", "bytes": 1000, "mtime": NOW - 86400 * 400, "sha": "y"},
                   {"path": "new/z.md", "bytes": 5, "mtime": NOW - 10, "sha": "z"},
                   {"path": "loose.md", "bytes": 99999, "mtime": NOW, "sha": "l"}]
        def folders(html):
            return sorted(["big", "new"], key=lambda f: html.index(f'data-open-dir="{f}"'))
        by_size, by_time = self.panel("size", listing), self.panel("modified", listing)
        self.assertEqual(folders(by_size), ["big", "new"])
        self.assertEqual(folders(by_time), ["new", "big"])
        self.assertLess(by_time.index('data-open-dir="big"'), by_time.index('data-open-doc="loose.md"'))
        self.assertIn('title="10,000 bytes in all">9.8 KB</span>', by_size)            # big/: x + y
        self.assertIn('<span class="dim">· 2 documents</span>', by_size)

    def test_a_document_too_big_to_show_says_so_and_its_refusal_offers_the_download(self):
        listing = [{"path": "huge.md", "bytes": 300 * 1024, "mtime": NOW, "sha": "too-big"}] + LISTING
        self.assertIn('too big to show</span>', self.panel(listing=listing))
        for archived in (False, True):          # an ended chat's panel offered none (Codex's review)
            opened = str(docs_panel("ada", listing, "huge.md", None, "none", set(), problem="too big", archived=archived))
            self.assertIn('<a href="/i/ada/file?path=notes%2Fdocs%2Fhuge.md">Download huge.md</a>', opened)
        self.assertNotIn("Download", str(docs_panel("ada", listing, "a.md", None, "none", set(), problem="refused")))


class DocsSortRouteTest(test_web.WebTest):
    def setUp(self):
        super().setUp()
        self.inst = Path(tempfile.mkdtemp(prefix="shaft-sort-inst-")).resolve()
        self.addCleanup(shutil.rmtree, self.inst, True)
        self.docs = self.inst / "wisp" / "notes" / "docs"
        self.docs.mkdir(parents=True)
        from supervisor import docs
        for p in (mock.patch.object(web, "_names", return_value=["wisp"]),
                  mock.patch.object(web_chat, "SUP_DIR", self.sup), mock.patch.object(web, "_sent_during", return_value=[]),
                  mock.patch("supervisor.core.INSTANCES", self.inst),
                  mock.patch.object(docs, "instance_root", lambda n: self.inst / n)):
            p.start()
            self.addCleanup(p.stop)
        self.cookie = self.paired()
        for k, (name, size) in enumerate([("zeta.md", 10), ("alpha.md", 5000), ("mid.md", 300)]):
            (self.docs / name).write_text("x" * size)
            os.utime(self.docs / name, (NOW - k * 60, NOW - k * 60))       # zeta newest, mid oldest

    def listed(self, query="", cookie=""):
        body = self.req(f"/fragment/docs/wisp?list=1{query}", cookie=self.cookie + cookie).body.decode()
        return order(body, ("zeta.md", "alpha.md", "mid.md"))

    def test_the_cookie_orders_every_listing_and_a_query_outranks_it(self):
        self.assertEqual(self.listed(), ["zeta.md", "alpha.md", "mid.md"])                       # newest first
        self.assertEqual(self.listed(cookie="; shaft_docsort=name"), ["alpha.md", "mid.md", "zeta.md"])
        self.assertEqual(self.listed(cookie="; shaft_docsort=size"), ["alpha.md", "mid.md", "zeta.md"])
        self.assertEqual(self.listed("&sort=size-asc", "; shaft_docsort=name"), ["zeta.md", "mid.md", "alpha.md"])
        self.assertEqual(self.listed(cookie="; shaft_docsort=%3Cscript%3E"), ["zeta.md", "alpha.md", "mid.md"])

    def test_a_sort_link_followed_without_the_script_sorts_the_list_not_opens_the_chats_document(self):
        """Codex's review: ?sort=name alone made the page open what this chat last changed instead of the list."""
        chat = web_chat.new_chat("wisp", test_web.ME)
        chat.update(turns=[{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                           {"role": "assistant", "content": "done", "ts": "2026-10-19T12:00:05",
                            "docs": [{"path": "mid.md", "created": True}]}], state="idle")
        web_chat.save(chat)
        self.assertIn('data-doc="mid.md"', self.req("/i/wisp/chat", cookie=self.cookie).body.decode())  # as before
        page = self.req("/i/wisp/chat?list=1&sort=name", cookie=self.cookie).body.decode()
        self.assertNotIn('class="docview"', page)
        panel = page.split('id="docs"')[1]                                         # past the reply's own mid.md card
        self.assertEqual(order(panel, ("zeta.md", "alpha.md", "mid.md")), ["alpha.md", "mid.md", "zeta.md"])
        aside = panel.split(">")[0]
        self.assertIn("list=1", aside)                                             # and the poll keeps the list
        self.assertIn("sort=name", aside)

    def test_the_chat_page_lists_in_the_cookie_order_and_polls_a_query_order(self):
        page = self.req("/i/wisp/chat?dir=", cookie=self.cookie + "; shaft_docsort=name-desc").body.decode()
        self.assertEqual(order(page, ("zeta.md", "alpha.md", "mid.md")), ["zeta.md", "mid.md", "alpha.md"])
        self.assertNotIn("sort=", page.split('id="docs"')[1].split(">")[0])        # the poll asks the cookie each time
        page = self.req("/i/wisp/chat?sort=size", cookie=self.cookie).body.decode()
        self.assertIn("&amp;sort=size\"", page.split('id="docs"')[1].split(">")[0])  # a followed link keeps its order


@unittest.skipUnless(NODE, "node is not installed")
class DocsSortJsTest(unittest.TestCase):
    def test_a_column_head_remembers_the_order_and_refreshes_the_panel(self):
        script = r"""
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const handlers = {};
let ticks = 0, replaced = "", prevented = false;
const panel = { dataset: { poll: "/fragment/docs/wisp?list=1&dir=ucg&sort=name", etag: "x" }, _tick() { ticks++; } };
const document = { readyState: "loading", cookie: "", addEventListener(k, fn) { (handlers[k] ||= []).push(fn); },
  querySelector: () => null, querySelectorAll: () => [], getElementById: id => id === "docs" ? panel : null };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { document, URL, setInterval() {}, setTimeout() {},
  location: { href: "https://shaft.test/i/wisp/chat?dir=ucg&sort=name" }, navigator: {},
  history: { replaceState(a, b, u) { replaced = String(u); } }, window: { addEventListener() {} } });
const head = { dataset: { sortDocs: "size-asc" } };
const target = { closest: q => q === "[data-sort-docs]" ? head : null };
(async () => {
  for (const fn of handlers.click) await fn({ target, preventDefault() { prevented = true; } });
  assert.ok(prevented);
  assert.match(document.cookie, /^shaft_docsort=size-asc; Path=\/; Max-Age=\d+; Secure; SameSite=Strict$/);
  assert.equal(panel.dataset.poll, "/fragment/docs/wisp?list=1&dir=ucg");     // the cookie decides from now on
  assert.equal(replaced, "https://shaft.test/i/wisp/chat?dir=ucg");
  assert.equal(panel.dataset.etag, undefined);
  assert.equal(ticks, 1);
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        result = subprocess.run([NODE, "-e", script, str(JS)], text=True, capture_output=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
