"""Main navigation and instance-wide note contracts through the shipped web routes."""
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
        self.assertIn('Switch conversation">wisp</button>', header)
        self.assertIn('aria-label="New thread with wisp"', header)
        self.assertIn('Your conversation with wisp.', self.html('/fragment/chat/wisp'))

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
        self.assertIn('☾ 4 for next cycle', header)
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
