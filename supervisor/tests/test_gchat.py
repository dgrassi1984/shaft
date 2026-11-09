"""Operator Chat scopes, with no real Google calls or saved accounts."""
import unittest
from unittest import mock

from supervisor import google


class FakeChat:
    def __call__(self, req, timeout=30):
        raise AssertionError(f'unexpected Google request: {req.full_url}')


class GoogleTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeChat()
        patch = mock.patch.object(google, 'urlopen', self.fake)
        patch.start()
        self.addCleanup(patch.stop)

    def test_operator_login_asks_chat_scopes(self):
        with mock.patch.object(google, 'consent', return_value={}) as consent, mock.patch.object(google, '_save_account'):
            google.login('work', 'operator@work.example', {})
        for constant, name in (('CHAT_SPACES', 'chat.spaces.readonly'),
                               ('CHAT_MESSAGES', 'chat.messages.readonly'),
                               ('CHAT_MEMBERS', 'chat.memberships.readonly')):
            with self.subTest(scope=name):
                scope = 'https://www.googleapis.com/auth/' + name
                self.assertIn(scope, consent.call_args.args[2])
                self.assertEqual(getattr(google, constant), scope)
                self.assertEqual(google.SCOPE_NAMES[scope], name)

    def test_instance_key_bundles_unchanged(self):
        self.assertEqual(google.KEY_SCOPES, {
            'gmail.modify': (('https://www.googleapis.com/auth/gmail.modify',), ('gmail.googleapis.com',)),
            'drive': (('https://www.googleapis.com/auth/drive',
                       'https://www.googleapis.com/auth/spreadsheets',
                       'https://www.googleapis.com/auth/documents',
                       'https://www.googleapis.com/auth/presentations'),
                      ('www.googleapis.com', 'sheets.googleapis.com', 'docs.googleapis.com', 'slides.googleapis.com'))})

    def test_account_without_chat_scope_names_login(self):
        saved = {'address': 'operator@work.example', 'client': 'work',
                 'scope': 'https://www.googleapis.com/auth/gmail.readonly'}
        with mock.patch.object(google, 'account', return_value=saved):
            with self.assertRaisesRegex(google.AccountError,
                                        r'chat\.messages\.readonly.*shaft google login work operator@work\.example'):
                google.Account('work', {}).require(google.CHAT_MESSAGES)
