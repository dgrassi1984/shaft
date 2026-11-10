"""Draft-only Gmail writes through the real broker and Google transport."""
import json
import threading
import urllib.parse
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

from supervisor import actions, broker, core, google, mail
from supervisor.tests import test_mail as read


class DraftGmail(read.FakeGmail):
    def __init__(self):
        super().__init__()
        self.urls, self.uploads, self.drafts = [], [], {}
        self.upload_accounts = []

    def __call__(self, req, timeout=0):
        self.urls.append(req.full_url)
        assert '/send' not in req.full_url, req.full_url
        if req.full_url == google.TOKEN_URL:
            super().__call__(req, timeout)
            form = dict(urllib.parse.parse_qsl(req.data.decode()))
            return self._answer({'access_token': 'at-' + form['refresh_token'], 'expires_in': 3600})
        path = urllib.parse.urlsplit(req.full_url).path
        if '/drafts' not in path:
            return super().__call__(req, timeout)
        if req.get_method() == 'GET':
            did = path.rsplit('/', 1)[1]
            if did not in self.drafts:
                raise self._error(req.full_url, 404, {})
            return self._answer(self.drafts[did])
        assert req.get_method() in ('POST', 'PUT')
        assert path.startswith('/upload/gmail/v1/users/me/drafts')
        assert urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query) == {'uploadType': ['multipart']}
        parsed = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + req.get_header('Content-type') + '\r\n\r\n').encode() + req.data)
        metadata, content = list(parsed.iter_parts())
        message = content.get_payload()[0]
        self.upload_accounts.append(req.get_header('Authorization'))
        self.uploads.append((req.get_method(), json.loads(metadata.get_payload(decode=True)), message))
        did = path.rsplit('/', 1)[1] if req.get_method() == 'PUT' else f'd{len(self.uploads)}'
        self.drafts[did] = {'id': did, 'message': {'id': f'm{len(self.uploads)}'}}
        return self._answer(self.drafts[did])


class DraftTest(read.MailCase):
    def fake_gmail(self):
        self.gmail = DraftGmail()
        return self.gmail

    def setUp(self):
        super().setUp()
        for alias in ('work', 'home'):
            google._save_account(alias, {'address': f'operator@{alias}.example', 'client': 'work',
                                        'refresh_token': 'rt-' + alias, 'scope': google.GMAIL + ' ' + google.GMAIL_COMPOSE})
        self.grants = [{'account': a, 'label': 'AssistantAccess', 'draft': True} for a in ('work', 'home')]
        core.set_mail_grants('wisp', self.grants)
        self.gmail.threads['aaa111'][-1]['payload']['headers'] += [
            {'name': 'Reply-To', 'value': 'reply@example.com'}, {'name': 'Message-ID', 'value': '<last@example.com>'},
            {'name': 'References', 'value': '<first@example.com>'}]
        self.args = {'account': 'work', 'to': ['reader@example.com'], 'subject': 'PRIVATE SUBJECT',
                     'body': 'PRIVATE BODY'}

    def tearDown(self):
        # Every test, including refusals and replay, checks every URL observed by the transport.
        self.assertTrue(all('/send' not in u for u in self.gmail.urls), self.gmail.urls)
        super().tearDown()

    def draft(self, b=None, **args):
        return self.ok(b or self.broker(), 'mail_draft', args or self.args)

    def test_in_thread_reply(self):
        out = self.draft(reply_to='work/aaa111', body='reply')
        method, metadata, msg = self.gmail.uploads[-1]
        self.assertEqual((method, metadata), ('POST', {'message': {'threadId': 'aaa111'}}))
        self.assertEqual(str(msg['To']), 'reply@example.com')
        self.assertEqual(str(msg['Subject']), 'Re: Project notes')
        self.assertEqual(str(msg['In-Reply-To']), '<last@example.com>')
        self.assertEqual(str(msg['References']), '<first@example.com> <last@example.com>')
        self.assertIn('in thread work/aaa111', out)
        self.assertIn('work/draft/d1', out)
        # bug guard: /mail/u/<address>/ links answered Gmail's 404; authuser= opens the draft
        self.assertIn('https://mail.google.com/mail/?authuser=operator%40work.example#drafts?compose=m1', out)
        self.assertNotIn('/mail/u/', out)

    def test_cross_account_and_overrides(self):
        core.set_mail_grants('wisp', [{**self.grants[0], 'draft': False}, self.grants[1]])
        out = self.draft(account='home', reply_to='work/aaa111', body='reply', to=['other@example.com'],
                         cc=['copy@example.com'], subject='Explicit')
        _, metadata, msg = self.gmail.uploads[-1]
        self.assertEqual(metadata, {'message': {}})
        self.assertEqual(str(msg['From']), 'operator@home.example')
        self.assertEqual(self.gmail.upload_accounts[-1], 'Bearer at-rt-home')
        self.assertEqual(str(msg['To']), 'other@example.com')
        self.assertEqual(str(msg['Cc']), 'copy@example.com')
        self.assertEqual(str(msg['Subject']), 'Explicit')
        self.assertEqual(str(msg['In-Reply-To']), '<last@example.com>')
        self.assertIn("in home's Drafts with reply headers to work/aaa111", out)

    def test_operator_followup_uses_its_to_cc(self):
        headers = self.gmail.threads['aaa111'][-1]['payload']['headers']
        for h in headers:
            if h['name'] == 'From':
                h['value'] = 'operator@home.example'
            if h['name'] == 'To':
                h['value'] = 'guest@example.com'
        headers.append({'name': 'Cc', 'value': 'copy@example.com'})
        self.draft(reply_to='work/aaa111', body='again')
        msg = self.gmail.uploads[-1][2]
        self.assertEqual(str(msg['To']), 'guest@example.com')
        self.assertEqual(str(msg['Cc']), 'copy@example.com')

    def test_grants_and_visibility(self):
        b = self.broker()
        core.set_mail_grants('wisp', [{**self.grants[0], 'draft': False}, self.grants[1]])
        err = self.refused(b, 'mail_draft', self.args)
        self.assertIn("allowed: home", err)
        self.assertIn('not visible', self.refused(b, 'mail_draft', {'reply_to': 'home/bbb222', 'body': 'x'}))
        core.set_mail_grants('wisp', [self.grants[0], {'account': 'work', 'label': 'Other'}])
        self.gmail.threads['bbb222'][0]['labelIds'] = [read.OTHER]
        self.assertIn('no label granted for drafts', self.refused(b, 'mail_draft',
                                                               {'reply_to': 'work/bbb222', 'body': 'x'}))
        self.assertEqual(self.gmail.uploads, [])

    def test_attachments_paths_and_total(self):
        b = self.broker()
        (self.root / 'safe.txt').write_text('file')
        (self.root / 'link.txt').symlink_to(self.root / 'safe.txt')
        (self.root / 'linked').symlink_to(self.root / 'notes', target_is_directory=True)
        for path, error in [('secrets/private.txt', 'reserved'), ('logs/log.txt', 'reserved'),
                            ('link.txt', 'link'), ('linked/file', 'link')]:
            self.assertIn(error, self.refused(b, 'mail_draft', {**self.args, 'attachments': [path]}))
        with mock.patch.object(mail, 'FILE_MAX', 7):
            self.assertIn('25 MB', self.refused(b, 'mail_draft', {**self.args, 'attachments': ['safe.txt', 'safe.txt']}))
        for folder in ('mail', 'gdrive'):
            (self.root / folder).mkdir()
            (self.root / folder / 'file.txt').write_text('text')
        out = self.draft(attachments=['mail/file.txt', 'gdrive/file.txt'], **self.args)
        self.assertIn('4 bytes', out)
        parts = list(self.gmail.uploads[-1][2].iter_attachments())
        self.assertEqual([p.get_content_type() for p in parts], ['text/plain', 'text/plain'])
        self.assertEqual([p.get_payload(decode=True) for p in parts], [b'text', b'text'])

    def test_reply_to_a_sender_whose_encoded_name_holds_a_comma(self):
        # bug (Gemini review): the decoded name "Doe, Jane" split at its comma, and the default reply was refused
        headers = self.gmail.threads['aaa111'][-1]['payload']['headers']
        headers[:] = [h for h in headers if h['name'] != 'Reply-To']
        headers.append({'name': 'Reply-To', 'value': '=?utf-8?Q?Doe=2C_Jane?= <jane@example.com>'})
        self.draft(reply_to='work/aaa111', body='reply')
        to = self.gmail.uploads[-1][2]['To']
        self.assertEqual([(a.display_name, a.addr_spec) for a in to.addresses], [('Doe, Jane', 'jane@example.com')])

    def test_empty_recipient_entry_and_bad_argument_types_refused(self):
        # bug (Gemini review): getaddresses skipped "" silently, leaving a malformed To header
        for to in ([''], ['a@example.com', ''], ['  ']):
            self.assertIn('is not an email address', self.refused(self.broker(), 'mail_draft', {**self.args, 'to': to}))
        self.assertIn('must be text', self.refused(self.broker(), 'mail_draft', {**self.args, 'body': 123}))
        self.assertIn('must be text', self.refused(self.broker(), 'mail_draft_update', {'id': 123, 'body': 'x'}))
        self.assertEqual(self.gmail.uploads, [])

    def test_attachment_count_capped(self):
        # bug (Gemini review): only bytes were capped, so thousands of empty files built one huge message
        (self.root / 'empty.txt').write_text('')
        self.assertIn('at most 10', self.refused(self.broker(), 'mail_draft',
                                                 {**self.args, 'attachments': ['empty.txt'] * 11}))
        self.assertEqual(self.gmail.uploads, [])

    def test_new_message_without_account_is_an_input_error_not_a_refused_action(self):
        b = self.broker()
        with mock.patch.object(broker.Broker, '_action_denied') as denied:
            error = self.refused(b, 'mail_draft', {'to': ['a@example.com'], 'subject': 'S', 'body': 'x'})
        self.assertIn('needs account, to and subject', error)
        denied.assert_not_called()

    def test_header_injection_and_recipient_validation(self):
        for patch in ({'subject': 'hello\r\nBcc: intruder@example.com'}, {'to': ['a@example.com\nBcc: b@example.com']},
                      {'cc': ['missing-at']}, {'to': []}, {'bcc': ['bad\raddress@example.com']}):
            self.refused(self.broker(), 'mail_draft', {**self.args, **patch})
        self.assertFalse(self.gmail.uploads)

    def test_update_ownership_and_replacement(self):
        b = self.broker()
        self.draft(b)
        self.ok(b, 'mail_draft_update', {'id': 'work/draft/d1', 'body': 'replacement',
                                       'to': ['new@example.com'], 'subject': 'new'})
        method, metadata, msg = self.gmail.uploads[-1]
        self.assertEqual(method, 'PUT')
        self.assertEqual(str(msg['To']), 'new@example.com')
        self.assertEqual(str(msg['Subject']), 'new')
        self.assertEqual(msg.get_body().get_content().strip(), 'replacement')
        rows = json.loads((self.sup / 'mail-drafts/wisp.json').read_text())
        self.assertEqual(rows['d1']['message_id'], 'm2')
        self.assertIn('unknown draft', self.refused(b, 'mail_draft_update', {'id': 'work/draft/no', 'body': 'x'}))
        self.gmail.drafts['d1']['message']['id'] = 'operator-edit'
        self.assertIn('theirs now', self.refused(b, 'mail_draft_update', {'id': 'work/draft/d1', 'body': 'x'}))
        del self.gmail.drafts['d1']
        self.assertIn('theirs now', self.refused(b, 'mail_draft_update', {'id': 'work/draft/d1', 'body': 'x'}))
        self.assertEqual(json.loads((self.sup / 'mail-drafts/wisp.json').read_text()), {})
        self.assertEqual(len(self.gmail.uploads), 2)

    def test_update_replaces_whole_draft_with_reply_defaults_not_old_content(self):
        # an update with only a body: a reply's recipients and subject come back from the thread, never the old
        # draft's explicit values; cc, bcc and attachments not given are gone
        self.draft(reply_to='work/aaa111', body='first', to=['explicit@example.com'], cc=['copy@example.com'],
                   bcc=['blind@example.com'], subject='Explicit')
        self.ok(self.broker(), 'mail_draft_update', {'id': 'work/draft/d1', 'body': 'second'})
        method, metadata, msg = self.gmail.uploads[-1]
        self.assertEqual((method, metadata), ('PUT', {'message': {'threadId': 'aaa111'}}))
        self.assertEqual(str(msg['To']), 'reply@example.com')
        self.assertEqual(str(msg['Subject']), 'Re: Project notes')
        self.assertEqual(str(msg['In-Reply-To']), '<last@example.com>')
        self.assertIsNone(msg['Cc'])
        self.assertIsNone(msg['Bcc'])
        self.assertEqual(list(msg.iter_attachments()), [])

    def test_update_of_new_message_needs_to_and_subject(self):
        self.draft()
        self.refused(self.broker(), 'mail_draft_update', {'id': 'work/draft/d1', 'body': 'no subject',
                                                          'to': ['a@example.com']})
        self.refused(self.broker(), 'mail_draft_update', {'id': 'work/draft/d1', 'body': 'no recipients',
                                                          'subject': 'S'})
        self.assertEqual(len(self.gmail.uploads), 1)
        self.ok(self.broker(), 'mail_draft_update', {'id': 'work/draft/d1', 'body': 'b', 'subject': 'S',
                                                     'to': ['a@example.com']})
        self.assertEqual(len(self.gmail.uploads), 2)

    def test_demoted_reversible_class_refuses_draft_instead_of_writing(self):
        # bug guard: _action_route's staged mode was ignored, so a demoted instance still drafted
        b = self.broker()
        staged = {'class': 'reversible', 'id': 'a1', 'mode': 'staged'}
        with mock.patch.object(broker.Broker, '_action_route', return_value=staged):
            self.assertIn('restore reversible', self.refused(b, 'mail_draft', self.args))
        self.assertEqual(self.gmail.uploads, [])

    def test_update_rechecks_draft_grant(self):
        self.draft()
        core.set_mail_grants('wisp', [self.grants[1]])
        self.assertIn('allowed: home', self.refused(self.broker(), 'mail_draft_update',
                                                  {'id': 'work/draft/d1', 'body': 'x'}))

    def test_limits_include_updates_and_mail_reads(self):
        b = self.broker()
        self.draft(b)
        for _ in range(4):
            self.ok(b, 'mail_draft_update', {'id': 'work/draft/d1', 'body': 'x', 'to': ['a@example.com'], 'subject': 'S'})
        self.assertIn('at most 5', self.refused(b, 'mail_draft', self.args))
        b = self.broker()
        with mock.patch.object(broker, 'MAIL_CALLS_PER_RUN', 1):
            self.ok(b, 'mail_read', {'id': 'work/aaa111'})
            self.assertIn('mail calls per run', self.refused(b, 'mail_draft', self.args))

    def test_offer_and_action_class(self):
        b = self.broker()
        specs = {s['function']['name']: s['function'] for s in b.tool_specs()}
        self.assertIn('Draft from: home (operator@home.example), work (operator@work.example)', specs['mail_draft']['description'])
        self.assertIn('work/AssistantAccess [drafts]', b.mail_env())
        for tool in broker.MAIL_DRAFT_TOOLS:
            self.assertEqual(actions.classify(tool, {}), 'reversible')
        core.set_mail_grants('wisp', [{**self.grants[0], 'draft': False}])
        self.assertFalse(set(broker.MAIL_DRAFT_TOOLS) & {s['function']['name'] for s in b.tool_specs()})
        self.assertEqual(self.refused(b, 'mail_draft', self.args), 'no mail granted to you for drafts')

    def test_log_redacts_success_and_errors(self):
        self.draft()
        self.refused(self.broker(), 'mail_draft', {**self.args, 'subject': 'PRIVATE SUBJECT\nInjected'})
        self.refused(self.broker(), 'mail_draft', {**self.args, 'body': 123, 'to': 123})
        self.refused(self.broker(), 'mail_draft', {**self.args, 'account': 'reader@example.com'})
        text = (self.sup / 'broker.log').read_text()
        for secret in ('PRIVATE BODY', 'PRIVATE SUBJECT', 'reader@example.com', 'operator@work.example'):
            self.assertNotIn(secret, text)
        self.assertIn('body_length', text)

    def test_replay_reads_and_updates_without_writes(self):
        state = self.project / 'replay'
        state.mkdir()
        b = broker.ReplayBroker('wisp', self.project, self.secrets, self.sup / 'broker.log', state_root=state,
                                start_commit='0', as_of=read.T0.replace(hour=12), cache={}, cache_lock=threading.Lock(),
                                billed='wisp@trial:t', tools_log=self.sup / 't.log')
        self.assertIn('mail_draft', {s['function']['name'] for s in b.tool_specs()})
        out = self.ok(b, 'mail_draft', {'reply_to': 'work/aaa111', 'body': 'simulated'})
        self.assertIn('work/draft/simulated-1', out)
        self.assertIn('simulated: not drafted', b.trace[-1]['tags'])
        self.ok(b, 'mail_draft_update', {'id': 'work/draft/simulated-1', 'body': 'next', 'to': ['a@example.com']})
        self.assertIn('simulated: not drafted', b.trace[-1]['tags'])
        self.assertFalse(self.gmail.uploads)
        self.assertFalse((self.sup / 'mail-drafts').exists())
        self.assertTrue(b.cache)

    def test_real_size_limit_and_outside_path(self):
        with (self.root / 'large.bin').open('wb') as f:
            f.truncate(25 * 1024 ** 2 + 1)
        self.assertIn('25 MB', self.refused(self.broker(), 'mail_draft',
                                          {**self.args, 'attachments': ['large.bin']}))
        self.assertIn('in your folder', self.refused(self.broker(), 'mail_draft',
                                                   {**self.args, 'attachments': ['../outside']}))
        self.assertFalse(self.gmail.uploads)

    def test_upload_refresh_and_quota_use_same_bytes(self):
        bodies = []

        def transport(req, timeout=0):
            if '/upload/' in req.full_url:
                bodies.append(req.data)
                if len(bodies) < 3:
                    raise self.gmail._error(req.full_url, 401 if len(bodies) == 1 else 429, {})
            return self.gmail(req, timeout)
        with mock.patch.object(google, 'urlopen', transport), mock.patch.object(google, 'sleep') as sleep:
            self.draft()
        self.assertEqual(len(bodies), 3)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[1], bodies[2])
        sleep.assert_called_once_with(2.0)

    def test_replay_existing_draft_rechecks_operator_edit(self):
        self.draft()
        path = self.sup / 'mail-drafts/wisp.json'
        before = path.read_bytes()
        state = self.project / 'replay'
        state.mkdir()
        b = broker.ReplayBroker('wisp', self.project, self.secrets, self.sup / 'broker.log', state_root=state,
                                start_commit='0', as_of=read.T0.replace(hour=12), cache={}, cache_lock=threading.Lock(),
                                billed='wisp@trial:t', tools_log=self.sup / 't.log')
        self.gmail.drafts['d1']['message']['id'] = 'operator-edit'
        self.assertIn('theirs now', self.refused(b, 'mail_draft_update',
                                               {'id': 'work/draft/d1', 'body': 'x', 'to': ['a@example.com']}))
        del self.gmail.drafts['d1']
        self.assertIn('theirs now', self.refused(b, 'mail_draft_update',
                                               {'id': 'work/draft/d1', 'body': 'x', 'to': ['a@example.com']}))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.gmail.uploads), 1)

    def test_no_send_endpoint_in_supervisor(self):
        # the modules that build Google requests (a judge prompt may name the endpoint; it never calls it)
        for path in (Path(mail.__file__).parent / f for f in ('google.py', 'mail.py', 'broker.py', 'calendar.py',
                                                              'gdrive.py', 'gchat.py', 'calendar_proposals.py')):
            text = path.read_text()
            for endpoint in ('drafts/send', 'messages/send'):
                self.assertFalse(endpoint in text, f"{path}: forbidden endpoint {endpoint}")
