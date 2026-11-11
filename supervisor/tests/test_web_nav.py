"""Main navigation and instance-wide note contracts through the shipped web routes."""
import html
import json
import re
import unittest
from unittest import mock

from supervisor import app_api, messages, web, web_chat, web_pages, web_views
from supervisor.tests import test_app_api, test_web_chat


class NavigationTest(test_web_chat.ChatRoutesCase):
    def setUp(self):
        super().setUp()
        self.history = []
        patch = mock.patch.object(messages, "history", side_effect=lambda: self.history)
        patch.start()
        self.addCleanup(patch.stop)

    def html(self, path):
        response = self.req(path, cookie=self.cookie)
        self.assertEqual(response.status, 200, response.body[:200])
        return response.body.decode()

    def test_open_thread_docs_poll_and_sort_keep_conversation(self):
        chat = self.chat_with([{'role': 'assistant', 'content': 'done', 'ts': '2026-11-01',
                               'docs': [{'path': 'a.md'}]}])
        chat['saved_docs'] = {'a.md': {'sha': 'old', 'text': 'operator line', 'added': ['operator line']}}
        web_chat.save(chat)
        with mock.patch('supervisor.docs.listing', return_value=[
                {'path': p, 'sha': 'x', 'bytes': 1, 'mtime': 0} for p in ('a.md', 'b.md')]), \
                mock.patch('supervisor.docs.read', return_value=('changed', 'new')), \
                mock.patch('supervisor.doc_comments.threads', return_value=[]), \
                mock.patch('supervisor.doc_comments.open_counts', return_value={}):
            page = self.html(f'/i/wisp/chat?id={chat["id"]}&list=1')
            poll = re.search(r'<aside class="docs"[^>]*data-poll="([^"]+)"', page)[1]
            self.assertIn(f'&amp;id={chat["id"]}', poll)
            panel = self.html(html.unescape(poll))
            self.assertIn(f'id={chat["id"]}', panel)
            self.assertIn(f'href="?doc=a.md&amp;id={chat["id"]}"', panel)
            opened = self.html(f'/fragment/docs/wisp?id={chat["id"]}&doc=a.md')
            self.assertIn('class="overwritten warn"', opened)
            self.main_with([])
            main = self.html('/fragment/docs/wisp?list=1')
            self.assertIn('href="?doc=a.md"', main)

    def test_untargeted_watch_and_away_use_main_before_its_first_message(self):
        patches = (mock.patch.object(web_chat, '_watched', {}), mock.patch.object(web_chat, '_away', {}))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertIsNone(web_chat.from_chat('wisp'))
        web_chat.watching('wisp', 'test-device', 1000, cid='from')
        self.assertEqual(web_chat.watchers('wisp'), {'test-device'})
        web_chat.unwatch('wisp', 'test-device', 2000)
        web_chat.watching('wisp', 'test-device', 1500, cid='from')
        self.assertEqual(web_chat.watchers('wisp', 'from'), set())
        web_chat.watching('wisp', 'test-device', 3000)
        self.assertEqual(web_chat.watchers('wisp', 'from'), {'test-device'})
        web_chat.unwatch('wisp', 'test-device')

    def test_main_is_one_selected_instance_and_threads_have_no_from_row(self):
        """Main fills only the instance; a topic fills only its child row."""
        chat = self.chat_with([])
        for cid in ("from", chat["id"]):
            rail = self.html(f"/fragment/rail?c=i:wisp&t={cid}")
            self.assertEqual(rail.count('aria-current="page"'), 1)
            selected = re.search(r'<a [^>]*aria-current="page"[^>]*>', rail)[0]
            self.assertIn('class="inst on"' if cid == "from" else 'class="thread on"', selected)
            self.assertNotIn("From wisp", rail)
            self.assertNotIn("+ New thread", rail)
            self.assertIn('aria-controls="threads-wisp"', rail)
            self.assertIn('aria-label="New thread with wisp"', rail)

    def test_no_threads_means_one_row_and_no_disclosure(self):
        """An instance without topics has no empty child list or disclosure."""
        rail = self.html('/fragment/rail?c=i:wisp&t=from')
        self.assertNotIn('data-disclose=', rail)
        self.assertNotIn('class="railthreads"', rail)
        self.assertEqual(rail.count('class="inst on"'), 1)

    def test_thread_title_is_escaped_complete_and_state_is_named_once(self):
        """Long titles remain available, and failure and unread have one accessible name."""
        chat = self.chat_with([{"role": "assistant", "content": "", "ts": "2026-11-10T10:00:00", "note": "error", "note_kind": "upstream"}])
        chat["title"] = 'A <long> title'
        web_chat.save(chat)
        with mock.patch('supervisor.web_seen.unread_conversations', return_value={chat['id']: True, 'from': False}):
            rail = self.html(f'/fragment/rail?c=i:wisp&t={chat["id"]}')
        self.assertIn('title="A &lt;long&gt; title"', rail)
        self.assertIn('A &lt;long&gt; title, reply failed, unread</span>', rail)
        self.assertIn('data-unread-main="0" data-unread-threads="1"', rail)

    def test_main_routes_header_and_empty_copy_name_the_instance(self):
        """Legacy From URLs redirect, and Main has the same compose control as a thread."""
        self.assertEqual(self.req('/i/wisp/from', cookie=self.cookie).headers['Location'], '/i/wisp/chat')
        header = self.html('/fragment/chat/wisp/header')
        self.assertNotIn('From wisp', header)
        self.assertIn('Switch conversation"><span>wisp</span><svg', header)
        self.assertIn('aria-label="New thread with wisp"', header)
        empty = self.html('/fragment/chat/wisp')
        self.assertIn('Your conversation with wisp.', empty)
        self.assertIn('data-post="/chat/wisp/new" data-after="open-thread"', empty)
        self.assertIn('aria-label="New thread with wisp"', empty)
        self.assertNotIn('✎', empty)

    def test_close_thread_header_uses_the_existing_action_route(self):
        """Close uses the existing chat-end action rather than inventing a chat route that returns 404."""
        chat = self.chat_with([])
        self.assertIn('data-post="/action/chat-end"', self.html(f'/fragment/chat/wisp/header?id={chat["id"]}'))
        with mock.patch.object(web_chat, 'end', return_value='closed') as end:
            response = self.req('/action/chat-end', 'POST', cookie=self.cookie,
                                headers={'sec-fetch-site': 'same-origin', 'x-csrf-token': self.csrf(self.cookie),
                                         'content-type': 'application/x-www-form-urlencoded'}, body=f'cid={chat["id"]}'.encode())
        self.assertEqual(response.status, 200)
        end.assert_called_once_with(chat['id'])

    def test_pending_notes_are_instance_wide_and_route_ended_topics_to_main(self):
        """The notes collection does not inherit the feed window, read rows, or ended destinations."""
        chat = self.chat_with([])
        ended = self.chat_with([])
        ended['state'] = 'ended'
        web_chat.save(ended)
        def note(file, **kw):
            return {"from": "operator", "to": "wisp", "via": messages.BACKGROUND, "file": file,
                    "mid": file, "ts": "2026-11-10T10:00:00", "text": "raw", "shown": "shown", "kind": "message", **kw}
        self.history[:] = [note('main'), note('topic', thread=chat['id']), note('ended', thread=ended['id']),
                           note('read', read=True), note('cancelled', cancelled='now'), note('decision', settled=True)]
        with mock.patch.object(web_chat, '_cycle_alive', return_value=False):
            notes = web_chat.pending_notes('wisp')
            header = str(web_views.pending_notes_html('wisp'))
        self.assertEqual([n['file'] for n in notes], ['main', 'topic', 'ended', 'decision'])
        self.assertEqual([n['cid'] for n in notes], ['from', chat['id'], 'from', 'from'])
        self.assertEqual([n['text'] for n in notes], ['shown'] * 4)
        self.assertFalse(notes[-1]['cancellable'])
        self.assertIn('☾ 4<span class="pending-label"> for next cycle</span>', header)
        self.assertIn('/chat/wisp/uncycle', header)
        self.assertIn(f'/i/wisp/chat?id={chat["id"]}', header)
        with mock.patch.object(web_chat, '_cycle_alive', return_value=True):
            self.assertTrue(all(not n['cancellable'] and n['reason'] for n in web_chat.pending_notes('wisp')))


class OutcomeTest(unittest.TestCase):
    def test_failed_uses_only_the_last_assistant_and_excludes_stop_and_retry(self):
        """A previous failure cannot mark a successful, stopped, or retried last answer failed."""
        bad = {"role": "assistant", "note": "error", "note_kind": "upstream"}
        self.assertTrue(web_chat.failed({'turns': [bad, {'role': 'user'}]}))
        for last in ({'role': 'assistant'}, {**bad, 'retried': True}, {**bad, 'note_kind': 'stopped'},
                     {'role': 'assistant', 'note': 'stopped by you'}):
            self.assertFalse(web_chat.failed({'turns': [bad, last]}))
        self.assertFalse(web_chat.failed(None))


class NavigationAppTest(test_app_api.AppTest):
    def test_app_notes_route_stamps_times_and_threads_report_failure(self):
        """Native clients receive the shared notes contract and failure outcomes for Main and topics."""
        c = self.chat([test_app_api.turn('assistant', '2026-11-03T14:00:00', '', note='error')])
        main = web_chat.from_chat('wisp', create=True)
        main['turns'] = c['turns']
        web_chat.save(main)
        rows = self.get('/app/v1/threads/wisp')
        self.assertTrue(rows['threads'][0]['failed'])
        self.assertTrue(rows['from']['failed'])
        main['turns'] = []
        web_chat.save(main)
        self.assertNotEqual(rows['threads_rev'], self.get('/app/v1/threads/wisp')['threads_rev'])
        notes = [{'file': 'note.md', 'mid': 'm1', 'text': 'later', 'ts': '2026-11-03T14:00:00',
                  'cid': 'from', 'title': 'wisp', 'cancellable': True, 'reason': None}]
        with mock.patch.object(web_chat, 'pending_notes', return_value=notes):
            got = self.get('/app/v1/notes/wisp')
        self.assertEqual(got, {'notes': [{**notes[0], 'ts': app_api.stamp(notes[0]['ts'])}]})


class HeaderPolishTest(test_web_chat.ChatRoutesCase):
    def header(self, cid=""):
        response = self.req('/fragment/chat/wisp/header' + (f'?id={cid}' if cid else ''), cookie=self.cookie)
        self.assertEqual(response.status, 200)
        return response.body.decode()

    def test_thread_subline_links_main_and_switcher_preserves_server_order(self):
        """The title switches conversations, while line two identifies the parent Main destination."""
        first, second = self.chat_with([]), self.chat_with([])
        first['title'], second['title'] = 'First <topic>', 'Second topic'
        web_chat.save(first)
        web_chat.save(second)
        with mock.patch.object(web_chat, 'threads', return_value=[second, first]):
            header = self.header(first['id'])
        self.assertIn('data-switch-conversation aria-haspopup="true" aria-expanded="false"', header)
        self.assertIn('title="Switch conversation"><span>First &lt;topic&gt;</span><svg', header)
        self.assertIn('<a class="instance-main" href="/i/wisp/chat">wisp</a><span class="subsep"> · </span>', header)
        menu = header.split('class="conversation-switcher menubody" hidden>', 1)[1].split('</div>', 1)[0]
        self.assertLess(menu.index('>wisp</a>'), menu.index('Second topic'))
        self.assertLess(menu.index('Second topic'), menu.index('First &lt;topic&gt;'))
        self.assertIn('class="dim switch-state"> · idle</span>', menu)
        self.assertNotIn('Main (', menu)
        self.assertIn(f'href="/i/wisp/chat?id={first["id"]}" aria-current="page"', menu)
        self.assertIn('data-post="/action/chat-end" data-confirm="Close this thread with wisp?', menu)
        self.assertIn(f'name="cid" value="{first["id"]}"', menu)
        self.assertIn('data-switch-new>New thread</button>', menu)
        self.assertIn('data-switch-rename>Rename…</button>', menu)
        main = self.header()
        self.assertNotIn('class="instance-main"', main)
        self.assertNotIn('data-switch-rename', main)
        self.assertNotIn('data-post="/action/chat-end"', main)
        self.assertIn('href="/i/wisp/chat" aria-current="page">wisp</a>', main)
        for control in ('aria-label="Documents"', 'aria-label="New thread with wisp"', 'class="menu chatmenu"'):
            self.assertIn(control, main)
            self.assertIn(control, header)

    def test_manage_contains_all_links_and_version_controls_with_page_defaults(self):
        """Manage must expose its summary while collapsed and contain version and Reload in its body."""
        for current, opened in [('i:wisp', ''), ('/settings', ' open')]:
            rail = str(web_pages.rail([], current))
            self.assertIn(f'<details class="manage" data-key="manage"{opened}><summary>Manage', rail)
            manage = rail.split('<details class="manage"', 1)[1].split('</details>', 1)[0]
            for href, label in web_pages.MANAGE:
                self.assertIn(f'href="{href}"', manage)
                self.assertIn(label, manage)
            self.assertIn('class="nav-version dim"', manage)
            self.assertIn('data-hard-reload', manage)

    def test_instance_lifecycle_dot_and_main_preview_are_independent_of_topics(self):
        """An idle Main keeps its hidden state slot and preview; a Main reply overrides the preview."""
        for state, cls in [('idle', 'idle'), ('in a cycle', 'cycle'), ('sleeping', 'sleep'), ('paused by you', 'paused')]:
            rail = str(web_pages.rail(['example'], 'i:example', {'instances': [{'name': 'example', 'state': state}]},
                                      {'example': {'line': 'Main last line', 'from_state': 'idle'}}))
            self.assertIn(f'class="dot st-{cls}" title="{state}"', rail)
            self.assertIn('class="last" aria-hidden="true">Main last line</span>', rail)
        rail = str(web_pages.rail(['example'], chats={'example': {'line': 'old', 'from_state': 'replying'}}))
        self.assertIn('class="last" aria-hidden="true">replying…</span>', rail)
        self.assertIn('class="tdot t-replying"', rail)
        failed, _ = web_pages._rail_state('idle', failed=True)
        self.assertIn('width="13" height="13"', failed)
        self.assertIn('stroke="currentColor"', failed)
        self.assertNotIn('>!</span>', failed)

    def test_pending_note_subline_is_a_disclosure_and_main_reply_takes_precedence(self):
        """Notes stay outside the Main link, and a reply replaces their preview with replying."""
        note = {'file': 'note.md', 'mid': 'm1', 'text': 'Later', 'ts': '2026-11-10T10:00:00',
                'cid': 'from', 'title': 'example', 'cancellable': True, 'reason': None}
        with mock.patch.object(web_chat, 'pending_notes', return_value=[note]):
            rail = str(web_pages.rail(['example'], chats={'example': {'notes': [note], 'line': 'old'}}))
            header = str(web_views.pending_notes_html('example'))
            chat_header = str(web_views.chat_header('example', {'state': 'in a cycle'}, None, []))
            replying = str(web_pages.rail(['example'], chats={'example': {'notes': [note], 'from_state': 'replying'}}))
        self.assertIn('class="railpending"><details class="menu pendingnotes"', rail)
        self.assertRegex(rail, r'<div class="railrow instrow">(?:(?!</section>).)*class="railpending".*</details></div></div>')
        self.assertIn('☾ 1<span class="pending-label"> for next cycle</span></summary>', rail)
        self.assertIn('☾ 1<span class="pending-label"> for next cycle</span></summary>', header)
        self.assertIn('<span class="subsep"> · </span><details class="menu pendingnotes"', chat_header)
        self.assertNotIn('class="railpending"', replying)
        self.assertIn('class="last" aria-hidden="true">replying…</span>', replying)

    def test_failure_has_separate_why_action_retry_details_and_no_rating(self):
        """A failed response offers recovery instead of asking the operator to rate a missing answer."""
        chat = self.chat_with([{'role': 'user', 'content': 'Hello', 'ts': '2026-11-10T10:00:00'},
                               {'role': 'assistant', 'content': '', 'ts': '2026-11-10T10:00:01',
                                'note': 'error', 'note_kind': 'upstream'}])
        feed = str(web_views.chat_feed('wisp', chat, []))
        self.assertIn('class="reply-failure"><p class="failure-why"><strong>', feed)
        self.assertIn('class="dim failure-action"', feed)
        self.assertIn('class="small retry" data-retry=', feed)
        self.assertIn('<summary>Technical details</summary>', feed)
        self.assertNotIn('data-thumb=', feed)
        self.assertNotIn('class="feedback"', feed)

    def test_composer_places_send_last_and_shortcuts_in_tooltip(self):
        """The primary arrow anchors the right edge without a permanent shortcut footer."""
        composer = str(web_views._composer('example', cid='from', draft='example/from', replying=False, mids=[], later=''))
        self.assertLess(composer.index('class="menu sendmenu"'), composer.index('class="send iconbtn"'))
        self.assertIn('title="Enter sends · Shift-Enter new line · ⌘-Enter queues', composer)
        self.assertNotIn('class="dim keys"', composer)
        self.assertTrue(composer.endswith('</button></div></div></form>'))

    def test_documents_marker_uses_this_conversation_not_the_instance_listing(self):
        """A document elsewhere must not open the drawer in an untouched thread."""
        chat = self.chat_with([])
        panel = web_views.docs_panel('wisp', [{'path': 'elsewhere.md', 'sha': 'x', 'bytes': 1, 'mtime': 0}], '', None, '', set())
        page = str(web_views.chat_page('wisp', chat, [], [], {}, ['wisp'], 't', panel=panel, selected=True))
        self.assertIn('data-conversation-docs="0"', page)
        chat['turns'] = [{'role': 'assistant', 'content': 'Done', 'ts': '2026-11-10T10:00:00',
                          'docs': [{'path': 'this.md', 'created': True}]}]
        page = str(web_views.chat_page('wisp', chat, [], [], {}, ['wisp'], 't', panel=panel, selected=True))
        self.assertIn('data-conversation-docs="1"', page)

    def test_compact_notes_manage_and_rail_focus_have_explicit_css(self):
        """Generic menu sizing must not turn the pending-note fact back into a toolbar button."""
        from pathlib import Path
        css = (Path(web_views.__file__).parent / 'web_static' / 'web.css').read_text()
        rule = re.search(r'details.pendingnotes > summary \{([^}]+)', css)[1]
        for declaration in ('padding: 0', 'font-size: inherit', 'color: var(--dim)', 'min-height: 0', 'border: 0'):
            self.assertIn(declaration, rule)
        self.assertNotIn('.rail details.manage > summary { display: none;', css)
        self.assertIn('nav .railrow a:focus-visible, .railrow button:focus-visible { outline: 2px solid var(--accent)', css)
        self.assertIn('nav .railrow:hover { background: var(--hover); }', css)
        self.assertIn('nav .railrow:has(> a.on) { background: color-mix(in srgb, var(--accent) 13%, var(--panel)); }', css)


class VisualStateTest(unittest.TestCase):
    def test_rail_state_outcomes_and_lifecycle_shapes(self):
        for chat, expected in [({'from_state': 'replying'}, 'tdot t-replying'),
                               ({'from_state': 'waiting'}, 'tdot t-waiting'),
                               ({'from_failed': True}, 'tdot t-failed')]:
            rail = str(web_pages.rail(['example'], chats={'example': chat}))
            self.assertIn(f'class="{expected}"', rail)
        for state, glyph in [('sleeping', 'sleep'), ('paused by you', 'pause'),
                             ('in a conversation (waiting for you)', 'clock')]:
            rail = str(web_pages.rail(['example'], snap={'instances': [{'name': 'example', 'state': state}]}))
            self.assertIn(str(web_pages.icon(glyph, 12 if glyph == 'clock' else 13)), rail)

    def test_compact_header_and_secondary_retry_rules(self):
        from pathlib import Path
        css = (Path(web_views.__file__).parent / 'web_static/web.css').read_text()
        for declaration in ['.railrow .st-idle { visibility: hidden; }',
                            '@media (prefers-reduced-motion: no-preference)',
                            '.chathead .pending-label { display: none; }',
                            '.chathead .sub details.busystate { flex-shrink: 0; }',
                            '.conversation-switcher .switch-state { display: inline-block; white-space: nowrap; }',
                            '.reply-failure button.retry::after { content: ""; position: absolute; inset: -6px; }']:
            self.assertIn(declaration, css)
        retry = re.search(r'\.reply-failure button.retry \{([^}]+)', css)[1]
        for declaration in ['width: auto', 'height: 32px', 'min-height: 32px',
                            'border: 1px solid var(--line)', 'background: var(--panel)', 'color: var(--text)']:
            self.assertIn(declaration, retry)

    def test_cycle_message_heading_has_plain_label_and_linked_topic_chip(self):
        item = {'type': 'message', 'kind': 'note', 'key': 'sent-m1', 'file': 'm1', 'text': 'Done',
                'attachments': [], 'rating': None, 'read': False, 'ts': '2026-11-10T10:00:00',
                're': {'cid': 'web-ended', 'title': 'A <topic>'}}
        card = str(web_views._message_item('example', item, False))
        self.assertIn('example sent you a message <a class="laterchip reply-chip"', card)
        self.assertIn('href="/i/example/chat?id=web-ended"', card)
        self.assertIn('re: «A &lt;topic&gt;»</a>', card)
        self.assertNotIn('href="/messages/', card)
        self.assertIn('data-reply-message="m1"', card)
