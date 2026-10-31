"""M5: notification holding must never delay message storage."""
from datetime import datetime, timedelta
import json
import threading
import unittest
from unittest import mock

from supervisor import broker, core, messages, metrics, pushes, telegram, web_alerts, webpush
from supervisor.tests import test_m5_pushes as fixtures


class PushHoldTest(unittest.TestCase):
    def setUp(self):
        fixtures.PushTest.setUp(self)
        self.enterContext(mock.patch.object(messages, "SUP_DIR", self.sup))
        self.at = datetime(2026, 10, 28, 23)
        for module in (broker, pushes):
            self.enterContext(mock.patch.object(module.datetime, 'now', side_effect=lambda: self.at))
        self.enterContext(mock.patch.object(pushes, 'PUSH_HOLD', 1))
        self.enterContext(mock.patch.object(pushes, 'QUIET_HOURS', (22, 7)))
        self.enterContext(mock.patch.object(pushes, 'DIGEST_AT', 7 * 60 + 15))
        self.open = self.enterContext(mock.patch('supervisor.runner.open_conversations', return_value={}))
        self.desktop = broker.notify
        self.notify = self.enterContext(mock.patch.object(webpush, 'notify', return_value=['phone']))
        self.enterContext(mock.patch.object(webpush, 'subscriptions', return_value={'phone': {}}))
        self.enterContext(mock.patch('supervisor.web_chat.watchers', return_value=set()))
        web_alerts._write_cursor(0)

    def send(self, text='First line\nsecond line', **kwargs):
        result = self.b._send_message('operator', text, **kwargs)
        self.at += timedelta(seconds=1)
        return result

    def forward(self):
        return web_alerts.forward_new()

    def rows(self):
        return pushes.records(self.sup)

    def test_quiet_push_is_stored_immediately_and_notified_only_in_morning_digest(self):
        self.send()
        row = self.rows()[0]
        self.assertTrue((self.sup / 'operator-inbox' / row['file']).is_file())
        self.assertEqual(row['text'], 'First line\nsecond line')
        self.assertFalse(row['urgent'])
        self.assertEqual(self.forward(), 0)
        self.notify.assert_not_called()
        self.desktop.assert_not_called()
        self.at = datetime(2026, 10, 29, 7, 14)
        self.assertEqual(self.forward(), 0)
        self.at = datetime(2026, 10, 29, 7, 15)
        self.assertEqual(self.forward(), 1)
        self.assertEqual(self.notify.call_args.args[0]['body'], 'm: First line')

    def test_urgent_push_bypasses_quiet_hours_and_records_urgency(self):
        self.send(urgent=True)
        self.assertTrue(self.rows()[0]['urgent'])
        self.assertNotIn('push_due', self.rows()[0])
        self.assertEqual(self.forward(), 1)
        self.assertEqual(self.notify.call_args.args[0]['body'], 'First line')
        self.desktop.assert_called_once()
        case = metrics._case({'tools': [{'tool': 'send_message', 'args': json.dumps(
            {'to': 'operator', 'text': 'First line', 'urgent': True}), 'result': 'delivered to the operator'}]})
        self.assertIn('"urgent": true', case)

    def test_seventh_push_is_held_when_daily_budget_is_six(self):
        self.at = datetime(2026, 10, 28, 12)
        for i in range(7):
            self.send(text=f'Message {i}')
        self.assertEqual([bool(r.get('push_due')) for r in self.rows()], [False] * 6 + [True])
        self.assertEqual(self.forward(), 6)
        self.assertEqual(self.desktop.call_count, 6)

    def test_recent_operator_reply_bypasses_quiet_hours_and_exhausted_budget(self):
        with mock.patch.object(pushes, 'PUSH_BUDGET', 1):
            self.send()
            messages.record(self.sup, self.sup / 'operator.md', 'operator', 'm', 'question',
                            self.at - timedelta(hours=2), 'What happened?')
            self.send('Here is the answer')
        self.assertNotIn('push_due', self.rows()[-1])
        self.assertEqual(self.forward(), 1)

    def test_open_conversation_bypasses_quiet_hours(self):
        self.open.return_value = {'talk-1': 'api'}
        self.send()
        self.assertNotIn('push_due', self.rows()[0])
        self.assertEqual(self.forward(), 1)

    def test_digest_claim_survives_second_loop_and_restart(self):
        self.send()
        self.at = datetime(2026, 10, 29, 7, 15)
        self.assertEqual(self.forward(), 1)
        web_alerts._write_cursor(0)  # even replaying the forwarder's cursor cannot resend a held row
        self.assertEqual(self.forward(), 0)
        self.notify.assert_called_once()
        self.assertEqual(json.loads((self.sup / 'push-digest.json').read_text()), [self.rows()[0]['file']])

    def test_hold_off_restores_immediate_notifications_even_over_budget_at_night(self):
        with mock.patch.object(pushes, 'PUSH_HOLD', 0), mock.patch.object(pushes, 'PUSH_BUDGET', 1):
            self.send()
            self.send()
        self.assertEqual(self.forward(), 2)
        self.assertTrue(all('push_due' not in r for r in self.rows()))
        self.assertEqual(self.desktop.call_count, 2)

    def test_digest_combines_senders_and_waits_for_later_quiet_end(self):
        with mock.patch.object(pushes, 'QUIET_HOURS', (22, 9)):
            self.send('One\nextra')
            self.b.name = 'other'
            self.send('Two\nextra')
        self.at = datetime(2026, 10, 29, 7, 15)
        self.assertEqual(self.forward(), 0)
        self.at = datetime(2026, 10, 29, 9)
        self.assertEqual(self.forward(), 1)
        self.assertEqual(self.notify.call_args.args[0]['body'], 'm: One\nother: Two')

    def test_expired_operator_message_does_not_bypass_hold(self):
        messages.record(self.sup, self.sup / 'operator.md', 'operator', 'm', 'question',
                        self.at - timedelta(hours=2, seconds=1), 'Old question')
        self.send()
        self.assertIn('push_due', self.rows()[-1])

    def test_telegram_holds_device_notification_and_dispatches_one_combined_digest(self):
        state = {'sent_offset': 0}
        with mock.patch('supervisor.channel.route', return_value=('telegram', 'test')), \
             mock.patch.object(telegram, 'paired_id', return_value=123), \
             mock.patch.object(telegram, 'load', return_value=state), \
             mock.patch.object(telegram, 'update', side_effect=lambda fn: fn(state)), \
             mock.patch.object(telegram, 'send', return_value=[1]) as send:
            self.send('For Telegram')
            self.assertEqual(telegram.forward_new({'m': 'fake-token'}), 0)
            send.assert_not_called()
            self.at = datetime(2026, 10, 29, 7, 15)
            self.assertEqual(telegram.forward_new({'m': 'fake-token'}), 1)
            self.assertEqual(send.call_args.args, ('fake-token', 123, 'SHAFT digest (1)\n\nm: For Telegram'))
            self.assertEqual(telegram.forward_new({'m': 'fake-token'}), 0)
            # The other forwarding process must honor the same persisted claim.
            self.assertEqual(self.forward(), 0)
            self.notify.assert_not_called()

    def test_zero_budget_and_empty_quiet_hours_do_not_hold(self):
        with mock.patch.object(pushes, 'PUSH_BUDGET', 0), mock.patch.object(pushes, 'QUIET_HOURS', None):
            for i in range(7):
                self.send(f'Unlimited {i}')
        self.assertEqual(self.forward(), 7)
        self.assertTrue(all('push_due' not in r for r in self.rows()))

    def test_digest_time_inside_overnight_quiet_hours_waits_until_their_end(self):
        with mock.patch.object(pushes, 'DIGEST_AT', 23 * 60 + 15):
            self.send()
        self.assertEqual(self.rows()[0]['push_due'], '2026-10-29T07:00:00')

    def test_replay_of_urgent_message_never_stores_or_notifies(self):
        replay = object.__new__(broker.ReplayBroker)
        replay.name, replay.project, replay.as_of, replay.operator_sent = 'm', self.project, self.at, 0
        replay.counts = self.b.counts
        replay._tags = threading.local()
        replay._send_message('operator', 'Simulated emergency', urgent=True)
        self.assertEqual(self.rows(), [])
        self.desktop.assert_not_called()

    def test_overdue_digest_does_not_wake_operator_during_next_quiet_window(self):
        self.send()
        self.at = datetime(2026, 10, 29, 23)
        self.assertEqual(self.forward(), 0)
        self.notify.assert_not_called()
        self.at = datetime(2026, 10, 30, 7, 15)
        self.assertEqual(self.forward(), 1)

    def test_digest_without_paired_destinations_remains_eligible(self):
        self.send()
        self.at = datetime(2026, 10, 29, 7, 15)
        self.notify.return_value = []
        self.assertEqual(self.forward(), 0)
        self.notify.return_value = ['phone']
        self.assertEqual(self.forward(), 1)
        self.assertEqual(self.forward(), 0)


if __name__ == '__main__':
    unittest.main()
