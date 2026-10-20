"""Comment threads in the documents panel (docs/plans/PLAN-comments.md §6.1)."""
from __future__ import annotations

import html
import json
import re
import unittest

from supervisor.web_views import chat_feed, docs_panel, threads_list

HOSTILE = '"><script>x</script>'
LISTING = [{"path": "plan.md", "bytes": 12}, {"path": "notes.md", "bytes": 4}]
TS = "2026-10-20T15:04:03"
_DEFAULT_ANCHOR = object()


def post(pid="p1", by="operator", text="hello", ts=TS, sent="web-1#1", **extra) -> dict:
    row = {"pid": pid, "by": by, "text": text, "ts": ts, "sent": sent}
    row.update(extra)
    return row


def thr(id="c1", state="open", by="operator", sha="abc", anchor=_DEFAULT_ANCHOR, posts=None,
        unsectioned=None) -> dict:
    if anchor is _DEFAULT_ANCHOR:
        anchor = {"exact": "quoted line", "prefix": "pre", "suffix": "suf", "start": 4}
    return {"id": id, "doc": "plan.md", "by": by, "state": state, "sha": sha, "anchor": anchor, "created": TS,
            "posts": [post()] if posts is None else posts, "unsectioned": unsectioned or []}


def panel(**kw) -> str:
    args = dict(name="ada", listing=LISTING, open_path="plan.md", text="Hello\n", sha="abc", marked=set())
    args.update(kw)
    return str(docs_panel(**args))


class CommentViewsTest(unittest.TestCase):
    def test_defaults_are_byte_identical(self):
        listing = [{"path": "plan.md", "bytes": 12}, {"path": "a b.md", "bytes": 1}]
        marked = {"plan.md"}
        bare = docs_panel("ada", listing, "plan.md", "Hello\n", "abc", marked)
        explicit = docs_panel("ada", listing, "plan.md", "Hello\n", "abc", marked, problem="", saved=None,
                              threads=None, counts=None, thread="")
        self.assertEqual(bare, explicit)
        self.assertNotIn("data-threads", str(bare))
        self.assertNotIn("data-comment-doc", str(bare))
        self.assertNotIn("ccount", str(bare))
        self.assertEqual(docs_panel("ada", listing, "plan.md", None, "abc", marked),
                         docs_panel("ada", listing, "plan.md", None, "abc", marked, threads=None, counts=None))
        self.assertEqual(docs_panel("ada", listing, "plan.md", "Hello\n", "abc", marked, problem="nope"),
                         docs_panel("ada", listing, "plan.md", "Hello\n", "abc", marked, problem="nope",
                                    threads=None, counts=None))
        self.assertEqual(str(threads_list("ada", "plan.md", "abc", [])), "")

    def test_counts_appear_on_rows_with_open_comments(self):
        html_out = panel(open_path="", text=None, counts={"plan.md": 3, "notes.md": 0})
        self.assertIn('data-open-doc="plan.md"><span>plan.md '
                      '<span class="ccount" title="open comments">💬 3</span></span>'
                      '<span class="dim docsize">12 bytes</span></a>', html_out)
        self.assertIn('data-open-doc="notes.md"><span>notes.md</span>'
                      '<span class="dim docsize">4 bytes</span></a>', html_out)
        html_out = panel(counts={"plan.md": 3, "notes.md": 0})
        self.assertIn('data-threads="[]"', html_out)
        self.assertIn('data-comment-doc aria-label="Comment on the whole document"', html_out)
        self.assertNotIn('class="threads"', html_out)

    def test_open_thread_shows_quote_resolve_and_sits_under_the_title(self):
        t = thr()
        html_out = panel(threads=[t])
        self.assertIn('data-edit-doc data-focus="doc-edit">Edit</button><button type="button" class="small" data-comment-doc '
                      'aria-label="Comment on the whole document">💬</button><button type="button" class="copy',
                      html_out)
        self.assertIn('data-copy-doc title="copy the document\'s Markdown" aria-label="Copy the document"><svg', html_out)
        self.assertIn('data-delete-refusal></div><details class="threads" data-key="threads-plan.md">'
                      '<summary>💬 1 open</summary>', html_out)
        self.assertNotIn('data-key="threads-plan.md" open>', html_out)
        self.assertIn('<article class="thread" id="thread-c1" data-thread="c1"><b>c1</b> <q>quoted line</q>', html_out)
        self.assertNotIn("by operator", html_out)
        self.assertIn('hidden data-detached-note>text no longer in the document</p>', html_out)
        self.assertNotIn("the document changed since this comment", html_out)
        self.assertIn('class="who">you <span class="dim">15:04</span></div>', html_out)
        self.assertIn('data-reply-thread="c1">Reply</button>', html_out)
        self.assertIn('data-post="/docs/ada/comment/state" data-after="comment-saved"', html_out)
        self.assertIn('name="thread" value="c1"', html_out)
        self.assertIn('name="state" value="resolved"', html_out)
        self.assertIn(">Resolve</button>", html_out)
        self.assertNotIn(">Reopen</button>", html_out)
        payload = json.dumps([{"id": "c1", "state": "open", "anchor": t["anchor"]}], separators=(",", ":"))
        self.assertIn('data-threads="' + html.escape(payload, quote=True) + '"', html_out)
        long = "q" * 301
        cut = str(threads_list("ada", "plan.md", "abc",
                               [thr(anchor={"exact": long, "prefix": "", "suffix": "", "start": 0})]))
        self.assertIn("<q>" + ("q" * 300) + "…</q>", cut)
        self.assertNotIn("q" * 301, cut)

    def test_message_icons_quote_shown_text_and_skip_empty_and_live_turns(self):
        turns = [{"role": "user", "content": "private context", "shown": '> quote\nmy "<text>"', "ts": TS},
                 {"role": "assistant", "content": "**answer**", "ts": TS},
                 {"role": "user", "content": "comment context", "shown": "", "ts": TS},
                 {"role": "assistant", "content": "", "ts": TS}]
        out = str(chat_feed("ada", {"id": "web-1", "turns": turns, "state": "replying"}, []))
        cards = re.split(r'(?=<div class="turn )', out)[1:]
        self.assertEqual(len(cards), 5)
        for card, role, raw in zip(cards[:2], ("me", "it"), ('> quote\nmy "<text>"', "**answer**")):
            self.assertIn(f'class="turn {role}"', card)
            self.assertIn(f'data-raw="{html.escape(raw, quote=True)}"', card)
            self.assertIn('aria-label="Copy the message"><svg', card)
            self.assertIn('aria-label="Reply to the message"><svg', card)
            self.assertNotIn(">Copy</button>", card)
        for card in cards[2:]:
            self.assertNotIn("data-copy", card)
            self.assertNotIn("data-reply-message", card)
        self.assertNotIn("private context", out)
        self.assertNotIn("comment context", out)

    def test_resolved_threads_fold_and_reopen(self):
        html_out = str(threads_list("ada", "plan.md", "abc",
                                    [thr(id="c1"), thr(id="c2", state="resolved", posts=[post(pid="p2")])]))
        self.assertIn("<summary>💬 1 open, 1 resolved</summary>", html_out)
        self.assertLess(html_out.index('id="thread-c1"'), html_out.index('class="resolved"'))
        self.assertLess(html_out.index('data-key="resolved-plan.md"'), html_out.index('id="thread-c2"'))
        self.assertIn("<summary>1 resolved</summary>", html_out)
        self.assertIn('name="state" value="open"', html_out)
        self.assertIn(">Reopen</button>", html_out)
        self.assertIn(">Resolve</button>", html_out)

    def test_whole_document_thread_has_no_quote(self):
        html_out = str(threads_list("ada", "plan.md", "abc", [thr(anchor=None)]))
        self.assertIn('<span class="dim">the whole document</span>', html_out)
        self.assertNotIn("<q>", html_out)
        self.assertNotIn("data-detached-note", html_out)
        page = panel(threads=[thr(anchor=None)])
        self.assertIn("&quot;anchor&quot;:null", page)

    def test_unsent_post_is_private_with_edit_delete_send(self):
        html_out = str(threads_list("ada", "plan.md", "abc", [thr(posts=[post(sent=None, text="draft note")])]))
        self.assertIn('class="post unsent" data-pid="p1"', html_out)
        self.assertIn("only you see this", html_out)
        self.assertIn('data-edit-post="p1">Edit</button>', html_out)
        self.assertIn('data-post="/docs/ada/comment/delete" data-confirm="Delete this comment?" '
                      'data-after="comment-saved"', html_out)
        self.assertIn('name="pid" value="p1"', html_out)
        self.assertIn(">Delete</button>", html_out)
        self.assertIn('data-post="/docs/ada/comment/send" data-after="comment-saved"', html_out)
        self.assertIn(">Send to ada</button>", html_out)
        sent = str(threads_list("ada", "plan.md", "abc", [thr(posts=[post(sent="web-9#2")])]))
        self.assertNotIn("unsent", sent)
        self.assertNotIn("only you see this", sent)
        self.assertNotIn("data-edit-post", sent)
        self.assertNotIn("comment/delete", sent)
        self.assertNotIn("comment/send", sent)
        answered = str(threads_list("ada", "plan.md", "abc",
                                    [thr(posts=[post(by="ada", sent=None, turn="web-9#3")])]))
        self.assertNotIn("unsent", answered)
        self.assertNotIn("only you see this", answered)

    def test_instance_opened_thread_names_the_author(self):
        html_out = str(threads_list("ada", "plan.md", "abc", [thr(
            by="ada", posts=[post(by="ada", text="started", sent="web-1#3", turn="web-1#3")])]))
        self.assertIn('<b>c1</b> <span class="dim">by ada</span> <q>quoted line</q>', html_out)
        self.assertIn('class="who">ada <span class="dim">15:04</span>', html_out)
        self.assertNotIn('class="who">you ', html_out)

    def test_unsectioned_reply_links_the_chat_turn(self):
        html_out = str(threads_list("ada", "plan.md", "abc", [thr(id="c7", unsectioned=["web-1#5", "web-1#6"])]))
        self.assertEqual(html_out.count("data-chat-turn"), 2)
        self.assertIn('<a href="#" data-chat-turn="web-1#5">answered in the chat, without a section for c7 →</a>',
                      html_out)
        self.assertIn('data-chat-turn="web-1#6"', html_out)

    def test_changed_sha_says_the_document_moved(self):
        moved = str(threads_list("ada", "plan.md", "new", [thr(sha="old")]))
        self.assertIn('<p class="dim">the document changed since this comment</p>', moved)
        self.assertIn("data-detached-note", moved)
        same = str(threads_list("ada", "plan.md", "abc", [thr(sha="abc")]))
        self.assertNotIn("the document changed since this comment", same)

    def test_active_thread_is_open_and_marked(self):
        html_out = str(threads_list("ada", "plan.md", "abc", [thr(id="c1"), thr(id="c2")], "c2"))
        self.assertIn('<details class="threads" data-key="threads-plan.md" open>', html_out)
        self.assertIn('<article class="thread" id="thread-c1" data-thread="c1">', html_out)
        self.assertIn('<article class="thread active" id="thread-c2" data-thread="c2">', html_out)
        wired = panel(threads=[thr(id="c1"), thr(id="c2")], thread="c2")
        self.assertIn('data-key="threads-plan.md" open>', wired)
        self.assertIn('class="thread active" id="thread-c2"', wired)

    def test_hostile_text_never_appears_unescaped(self):
        anchor = {"exact": HOSTILE, "prefix": HOSTILE, "suffix": "s", "start": 0}
        t = thr(by=HOSTILE, anchor=anchor, posts=[post(by=HOSTILE, text=HOSTILE, sent="web-1#1")])
        html_out = panel(threads=[t], counts={"plan.md": 1}, thread="c1")
        self.assertNotIn(HOSTILE, html_out)
        self.assertNotIn("<script", html_out)
        self.assertNotIn("</script>", html_out)
        attr = re.search(r'data-threads="([^"]*)"', html_out)
        self.assertIsNotNone(attr)
        self.assertNotIn("<", attr.group(1))
        self.assertNotIn(">", attr.group(1))
        self.assertIn("&lt;script&gt;", attr.group(1))
        self.assertGreaterEqual(html_out.count("&lt;script&gt;"), 3)

    def test_a_post_carries_its_raw_text_escaped_for_edit(self):
        """Edit fills the box from data-text: the note as written, not the rendered Markdown."""
        raw = 'a "quote"\n<tag> & **not bold**'
        html_out = str(threads_list("ada", "plan.md", "abc", [thr(posts=[post(text=raw, sent=None)])]))
        attr = re.search(r'data-text="([^"]*)"', html_out)
        self.assertIsNotNone(attr)
        self.assertNotIn("<", attr.group(1))
        self.assertNotIn(">", attr.group(1))
        self.assertNotIn('"', attr.group(1))
        self.assertEqual(html.unescape(attr.group(1)), raw)
        self.assertIn('class="post unsent" data-pid="p1" data-text="', html_out)
        self.assertIn("**not bold**", attr.group(1))
        self.assertNotIn(raw, html_out)

    def test_chat_feed_chips_for_comments_answered_and_opened(self):
        """PLAN-comments §6.1: 💬 chips, and a comment-only turn (empty shown text) has no empty bubble."""
        chat = {"id": "web-1", "state": "idle", "turns": [
            {"role": "user", "ts": TS, "content": "look", "shown": "look", "edits": ["plan.md"],
             "comments": [{"id": "c7", "doc": "plan.md"}, {"id": "c8", "doc": 'a"b.md'}]},
            {"role": "assistant", "ts": "2026-10-20T15:05:03", "content": "done",
             "docs": [{"path": "plan.md", "created": False}],
             "answered": [{"id": "c7", "doc": "plan.md"}],
             "opened": [{"id": "c9", "doc": "notes.md"}]},
            {"role": "user", "ts": "2026-10-20T15:06:03", "content": "COMMENT BLOCK not shown", "shown": "",
             "comments": [{"id": "c3", "doc": "plan.md"}]},
        ]}
        feed = str(chat_feed("ada", chat, []))
        edited = 'class="editchip" href="?doc=plan.md" data-open-doc="plan.md">✎ edited plan.md</a>'
        comment = ('class="commentchip" href="?doc=plan.md&amp;thread=c7" data-open-doc="plan.md" '
                   'data-open-thread="c7">💬 c7</a>')
        self.assertIn(edited, feed)
        self.assertIn(comment, feed)
        self.assertLess(feed.index(edited), feed.index(comment))
        self.assertIn('href="?doc=a%22b.md&amp;thread=c8" data-open-doc="a&quot;b.md" data-open-thread="c8"', feed)
        reply = feed.split('data-k="a1"', 1)[1].split("data-k=", 1)[0]
        self.assertIn('class="doccard"', reply)
        self.assertIn('class="commentchip" href="?doc=plan.md&amp;thread=c7" data-open-doc="plan.md" '
                      'data-open-thread="c7">💬 answered c7</a>', reply)
        self.assertIn('class="commentchip" href="?doc=notes.md&amp;thread=c9" data-open-doc="notes.md" '
                      'data-open-thread="c9">💬 opened c9</a>', reply)
        self.assertLess(reply.index('class="doccard"'), reply.index("💬 answered c7"))
        self.assertLess(reply.index("💬 answered c7"), reply.index("💬 opened c9"))
        only = feed.split('data-k="u2"', 1)[1].split("data-k=", 1)[0]
        self.assertIn('class="commentchip" href="?doc=plan.md&amp;thread=c3" data-open-doc="plan.md" '
                      'data-open-thread="c3">💬 c3</a>', only)
        self.assertNotIn("<p", only)
        self.assertNotIn('class="wrap"', only)
        self.assertNotIn("COMMENT BLOCK", only)
