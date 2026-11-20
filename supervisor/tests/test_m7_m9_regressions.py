"""M7 item identity, reply exemptions, cited cycles, blind probes; M9 plan accounting."""
import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import commitments, core, doc_comments, feedback, judge, judge_probe, messages, metrics, outcomes, planchannel, projects, pushes, reply_links
from supervisor.tests.test_judge2 import VERDICT

NOW = datetime(2026, 11, 6, 12)
LEDGER = commitments.ledger


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]))
        self.base = Path(tmp)
        self.sup = self.base / 'sup'
        self.sup.mkdir()
        self.roots = self.base / 'birds'
        (self.roots / 'm').mkdir(parents=True)
        for mod in (core, metrics, commitments):
            self.enterContext(mock.patch.object(mod, 'SUP_DIR', self.sup))
            self.enterContext(mock.patch.object(mod, 'INSTANCES', self.roots))
        self.enterContext(mock.patch.object(feedback, 'FILE', self.sup / 'feedback.jsonl'))
        self.enterContext(mock.patch.object(metrics, 'log'))
        for mod in (metrics, pushes, outcomes, judge_probe, feedback, doc_comments):
            self.enterContext(mock.patch.object(mod, 'datetime', Clock))

    def write(self, rel, rows):
        path = self.sup / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def message(self, sender='m', to='operator', **kw):
        return {'file': 'item.md', 'from': sender, 'to': to, 'ts': NOW.isoformat(),
                'text': 'Should I rewrite the parser?', **kw}

    def outcome(self, replies=(), hours=48, **kw):
        self.write('messages.jsonl', [self.message(**kw), *replies])
        return outcomes.records('m', NOW + timedelta(hours=hours))[0]


class OutcomesTest(Fixture):
    def test_explicit_chat_link_window_and_instance(self):
        self.write('messages.jsonl', [self.message()])
        for delta, instance, expected in [(0, 'm', 'followed_up'), (48, 'm', 'followed_up'),
                                          (48 + 1/3600, 'm', 'no_link'), (-1, 'm', 'no_link'),
                                          (1, 'other', 'no_link')]:
            with self.subTest(delta=delta, instance=instance):
                self.write('reply-links.jsonl', [{'instance': instance, 'in_reply_to': 'item.md',
                           'ts': (NOW + timedelta(hours=delta)).isoformat(), 'key': 'app:reply'}])
                result = outcomes.records('m', NOW + timedelta(hours=50))[0]
                self.assertEqual(result['outcome'], expected)
        self.write('reply-links.jsonl', [None, [], {'in_reply_to': []}, {'in_reply_to': 'item.md'}])
        self.assertEqual(outcomes.records('m', NOW + timedelta(hours=50))[0]['outcome'], 'no_link')

    def test_m7a_unrelated_operator_message_never_counts(self):
        reply = self.message('operator', 'm', file='other.md', text='A different topic', ts=(NOW + timedelta(hours=1)).isoformat())
        self.assertEqual(self.outcome([reply])['outcome'], 'no_link')

    def test_m7a_explicit_reply_identity_counts_at_48h_boundary(self):
        reply = self.message('operator', 'm', file='answer.md', text='Yes', in_reply_to='item.md', ts=(NOW + timedelta(hours=48)).isoformat())
        row = self.outcome([reply])
        self.assertEqual((row['outcome'], row['evidence']), ('followed_up', 'message:answer.md'))
        reply['ts'] = (NOW + timedelta(hours=48, seconds=1)).isoformat()
        self.assertEqual(self.outcome([reply], hours=49)['outcome'], 'no_link')

    def test_m7a_quoted_reply_links_only_its_item(self):
        reply = self.message('operator', 'm', file='answer.md', text='> Should I rewrite the parser?\n\nYes')
        self.assertEqual(self.outcome([reply])['outcome'], 'followed_up')
        self.assertEqual(self.outcome([reply], text='An unrelated delivered item')['outcome'], 'no_link')

    def test_m7a_feedback_names_the_file(self):
        rated = feedback.rate('m', 'operator', {'kind': 'message', 'file': 'item.md'}, 'up', '', '', '')
        self.assertEqual(self.outcome()['outcome'], 'followed_up')
        feedback.clear(rated['id'], 'operator')
        self.assertEqual(self.outcome()['outcome'], 'followed_up')

    def test_m7a_document_comment_links_delivered_or_attached_document(self):
        comments = [{'id': 'c1', 'doc': 'report.md', 'posts': [
            {'pid': 'p1', 'by': 'operator', 'ts': NOW.isoformat()}]}]
        with mock.patch.object(doc_comments, 'threads', return_value=comments):
            self.assertEqual(self.outcome(text='Read [report](notes/docs/report.md)')['outcome'], 'edited')
            self.assertEqual(self.outcome(attachments=[{'path': 'birds/m/notes/docs/report.md'}])['outcome'], 'edited')
            self.assertEqual(self.outcome(text='notes/docs/other.md')['outcome'], 'no_link')
            comments[0]['doc'] = 'Quarterly report.md'
            self.assertEqual(self.outcome(text='Read [report](notes/docs/Quarterly report.md)')['outcome'], 'edited')

    def test_m7a_real_comment_store_uses_document_relative_path(self):
        doc_comments.add_thread('m', 'p-0000000000000001', 'notes/docs/report.md', None, 'sha', 'Please revise')
        self.assertEqual(self.outcome(text='[Report](notes/docs/report.md)')['outcome'], 'edited')

    def test_m7a_ambiguous_quote_cannot_identify_one_of_two_items(self):
        reply = self.message('operator', 'm', file='answer.md', text='> Should I rewrite the parser?\nYes')
        self.write('messages.jsonl', [self.message(), self.message(file='duplicate.md'), reply])
        self.assertEqual([r['outcome'] for r in outcomes.records('m', NOW + timedelta(hours=48))], ['no_link', 'no_link'])

    def test_m7a_window_is_open_until_boundary_and_sent_is_missing(self):
        self.assertEqual(self.outcome(hours=47)['outcome'], 'open')
        self.assertEqual(self.outcome(hours=48)['outcome'], 'no_link')
        self.assertIn('sent: missing draft-to-sent record', '\n'.join(outcomes.lines('m', NOW)))

    def test_m7a_reply_header_survives_in_supervisor_record(self):
        path = self.base / 'reply.md'
        path.write_text('From: operator\nIn-Reply-To: item.md\n\nYes')
        messages.record(self.sup, path, 'operator', 'm', 'reply', NOW, 'Yes')
        self.assertEqual(pushes.records(self.sup)[0]['in_reply_to'], 'item.md')

    def test_m7a_weekly_stats_and_operator_view_have_no_per_cycle_outcome(self):
        self.outcome()
        with mock.patch.object(metrics, 'judgments', return_value=[]), mock.patch.object(metrics, '_sections', return_value=[]), \
                mock.patch.object(metrics, '_battery_section', return_value=[]), mock.patch.object(metrics, '_code_section', return_value=[]), \
                mock.patch.object(metrics, '_traits_section', return_value=[]), mock.patch.object(metrics, '_trials_section', return_value=[]), \
                mock.patch.object(metrics, 'plan_lines', return_value=[]):
            text = metrics.stats_text('m')
        self.assertIn('operator outcomes (last 7 days', text)
        self.assertNotIn('item.md', text)

    def test_outcomes_skip_missing_files_null_and_string_attachments_and_refused_comments(self):
        missing = self.message()
        del missing['file']
        self.write('messages.jsonl', [missing, self.message(attachments=None),
                                    self.message(file='other.md', attachments=['bad']),
                                    self.message(file='third.md', attachments='bad')])
        with mock.patch.object(doc_comments, 'threads', side_effect=doc_comments.Refused('bad store')):
            self.assertEqual([(r['file'], r['outcome']) for r in outcomes.records('m', NOW + timedelta(hours=48))],
                             [('item.md', 'no_link'), ('other.md', 'no_link'), ('third.md', 'no_link')])

    def test_push_stats_survive_outcomes_read_failure(self):
        with mock.patch.object(outcomes, 'lines', side_effect=OSError('bad store')):
            text = '\n'.join(pushes.lines('m', [], NOW))
        self.assertIn('Outcomes could not be read: OSError: bad store', text)
        self.assertIn('pushes to the operator today: 0', text)

    def test_short_quote_never_links_and_old_duplicate_does_not_hide_recent_match(self):
        reply = self.message('operator', 'm', file='reply.md', text='> Done', ts=(NOW + timedelta(hours=1)).isoformat())
        self.assertEqual(self.outcome([reply], text='Done')['outcome'], 'no_link')
        reply['text'] = '> Should I rewrite the parser?'
        self.write('messages.jsonl', [self.message(file='old.md', ts=(NOW - timedelta(days=30)).isoformat()),
                                    self.message(), reply])
        self.assertEqual([(r['file'], r['outcome']) for r in outcomes.records('m', NOW + timedelta(hours=48))],
                         [('old.md', 'no_link'), ('item.md', 'followed_up')])
        reply['in_reply_to'] = 'old.md'
        self.write('messages.jsonl', [self.message(file='old.md', ts=(NOW - timedelta(days=30)).isoformat()),
                                    self.message(), reply])
        self.assertEqual([r['outcome'] for r in outcomes.records('m', NOW + timedelta(hours=48))], ['no_link', 'no_link'])

    def test_doc_comment_credits_only_latest_delivery_before_comment(self):
        self.write('messages.jsonl', [self.message(text='notes/docs/report.md', ts=(NOW - timedelta(hours=1)).isoformat()),
                                    self.message(file='new.md', attachments=[{'path': 'notes/docs/report.md'}]),
                                    self.message(file='future.md', text='notes/docs/report.md', ts=(NOW + timedelta(hours=2)).isoformat())])
        comments = [{'id': 'c', 'doc': 'report.md', 'posts': [
            {'pid': 'p', 'by': 'operator', 'ts': (NOW + timedelta(hours=1)).isoformat()}]}]
        with mock.patch.object(doc_comments, 'threads', return_value=comments):
            self.assertEqual([(r['file'], r['outcome']) for r in outcomes.records('m', NOW + timedelta(hours=50))],
                             [('item.md', 'no_link'), ('new.md', 'edited'), ('future.md', 'no_link')])

    def test_outcomes_since_skips_old_bodies_and_weekly_lines_bound_scan(self):
        self.write('messages.jsonl', [self.message(file='old.md', text=None, ts=(NOW - timedelta(days=30)).isoformat()),
                                    self.message()])
        self.assertEqual([r['file'] for r in outcomes.records('m', NOW, since=NOW - timedelta(days=9))], ['item.md'])
        self.assertIn('open 1', '\n'.join(outcomes.lines('m', NOW)))


class JudgingTest(Fixture):
    def setUp(self):
        super().setUp()
        self.ep = {'kind': 'live', 'ts': NOW.isoformat(), 'intent': {'goal': 'deliver'},
                   'finish': {'delivered_via': 'message'}, 'tools': [
                       {'tool': 'send_message', 'args': '{"to":"operator","text":"result"}', 'result': 'delivered'}]}
        for key, value in (('live_episodes', [self.ep]), ('_written_files', ''), ('_provenance_evidence', ''),
                           ('_followed_surprise', ''), ('own_models', []), ('second_sampled', False), ('cycle_model', 'cloud')):
            self.enterContext(mock.patch.object(metrics, key, return_value=value))
        self.ledger = self.enterContext(mock.patch.object(commitments, 'ledger', return_value={}))
        self.enterContext(mock.patch.object(judge, 'available', return_value=True))
        self.enterContext(mock.patch.object(judge, 'pick', return_value='judge'))
        self.ask = self.enterContext(mock.patch.object(judge, 'ask', side_effect=lambda *a, **k: dict(VERDICT, should_have_waited='yes', delivery_supported='yes')))
        self.enterContext(mock.patch.object(judge, 'JUDGE_EVERY', 1))

    def test_m7b_operator_reply_fact_does_not_overwrite_unrelated_push_verdict(self):
        self.ep['inbox'] = ['20261106-110000-000001-from-operator.md']
        self.assertEqual(metrics.judge_pending('m'), 1)
        self.assertEqual(metrics.judgments('m')[0]['should_have_waited'], 'yes')
        self.assertIn('answers_operator: true', self.ask.call_args.args[1])

    def test_m7b_deferred_operator_message_does_not_exempt_push(self):
        self.ep['inbox'] = ['20261106-110000-000001-from-operator.md']
        self.ep['inbox_deferred'] = list(self.ep['inbox'])
        metrics.judge_pending('m')
        self.assertEqual(metrics.judgments('m')[0]['should_have_waited'], 'yes')
        self.assertTrue(metrics.answers_operator({'intent': {'origin': 'operator'}}))
        self.assertFalse(metrics.answers_operator({'kind': 'talk', 'role': 'assistant', 'speaker': 'other'}))

    def test_m7c_unsampled_cited_judged_once_and_excluded_from_sampled_mean(self):
        self.write('judgments/m-unsampled.jsonl', [{'episode_ts': self.ep['ts']}])
        self.ledger.return_value = {'c1': {'status': 'kept', 'evidence': self.ep['ts'] + '#1'}}
        self.assertEqual(metrics.judge_pending('m'), 1)
        self.assertEqual(metrics.judge_pending('m'), 0)
        self.assertEqual(self.ask.call_count, 1)
        self.assertTrue(metrics.judgments('m')[0]['cited'])
        self.assertEqual(metrics.judgments_for_stats('m'), [])
        self.assertIsNone(metrics.mean_value(metrics.judgments_for_stats('m')))

    def test_m7c_real_kept_commitment_citation_reopens_unsampled_episode(self):
        self.ledger.side_effect = LEDGER
        path = self.roots / 'm/memory/commitments.jsonl'
        path.parent.mkdir()
        made = {'event': 'made', 'id': 'c2026-1106-1', 'at': (NOW - timedelta(hours=2)).isoformat(),
                'due': (NOW + timedelta(hours=2)).isoformat(), 'promise': 'deliver', 'criterion': 'sent',
                'made_to': 'operator', 'source': 'cycle'}
        settled = {'event': 'settled', 'id': made['id'], 'at': NOW.isoformat(), 'status': 'kept', 'evidence': self.ep['ts']}
        path.write_text(json.dumps(made) + '\n' + json.dumps(settled) + '\n')
        self.write('judgments/m-unsampled.jsonl', [{'episode_ts': self.ep['ts']}])
        self.assertEqual(metrics.judge_pending('m'), 1)
        self.assertTrue(metrics.judgments('m')[0]['cited'])

    def test_m7c_cited_high_grade_does_not_move_sampled_mean(self):
        self.write('judgments/m.jsonl', [dict(VERDICT, episode_ts='sample', value=1, confidence_of=None),
                                       dict(VERDICT, episode_ts='cited', value=5, cited=True, confidence_of=None)])
        self.assertEqual(metrics.mean_value(metrics.judgments_for_stats('m')), 1)

    def test_m7c_unsampled_uncited_stays_out(self):
        self.write('judgments/m-unsampled.jsonl', [{'episode_ts': self.ep['ts']}])
        self.assertEqual(metrics.judge_pending('m'), 0)
        self.ask.assert_not_called()

    def test_m7c_citation_does_not_remove_randomly_selected_cycle(self):
        self.ledger.return_value = {'c1': {'status': 'kept', 'evidence': self.ep['ts']}}
        metrics.judge_pending('m')
        self.assertEqual(len(metrics.judgments_for_stats('m')), 1)

    def test_m7d_yes_is_missed_no_or_unsupported_is_caught_and_copy_isolated(self):
        original = copy.deepcopy(self.ep)
        self.write('judgments/m.jsonl', [{'episode_ts': self.ep['ts'], 'delivery_supported': 'yes', 'confidence_of': None}])
        before = (self.sup / 'judgments/m.jsonl').read_bytes()
        for delivery, supported, caught in [('yes', True, False), ('no', True, True), ('yes', False, True)]:
            path = judge_probe._path('m')
            path.unlink(missing_ok=True)
            self.ask.side_effect = lambda *a, **k: dict(VERDICT, delivery_supported=delivery, claim_supported=supported)
            self.assertEqual(judge_probe.run('m'), 1)
            self.assertEqual(judge_probe.records('m')[0]['caught'], caught)
            self.assertIn('ERROR: delivery refused', self.ask.call_args.args[1])
            self.assertNotIn('planted', self.ask.call_args.args[1])
            self.assertEqual(self.ep, original)
            self.assertEqual((self.sup / 'judgments/m.jsonl').read_bytes(), before)

    def test_244_cycles_that_reach_the_operator_are_always_judged_and_kept_out_of_the_sample(self):
        """#244: with SHAFT_JUDGE_EVERY=2, 29 of 58 cycles that messaged the operator were never judged, including
        one he called unnecessary. Reverting reaches_operator in judge_pending leaves them unjudged again."""
        quiet = {'kind': 'live', 'ts': (NOW - timedelta(hours=1)).isoformat(), 'intent': {'goal': 'tidy notes'},
                 'finish': {'delivered_via': 'none'}, 'tools': [{'tool': 'write_file', 'args': '{}', 'result': 'ok'}]}
        claimed = {**quiet, 'ts': (NOW - timedelta(hours=2)).isoformat(), 'finish': {'delivered_via': 'mail'}}
        metrics.live_episodes.return_value = [claimed, quiet, self.ep]
        with mock.patch.object(judge, 'JUDGE_EVERY', 2), \
                mock.patch.object(metrics.secrets, 'randbelow', return_value=1):   # the draw leaves every run out
            self.assertEqual(metrics.judge_pending('m'), 2)
        judged = {j['episode_ts']: j for j in metrics.judgments('m')}
        self.assertEqual(set(judged), {self.ep['ts'], claimed['ts']})
        self.assertTrue(all(j['operator_selected'] for j in judged.values()))
        self.assertEqual(metrics.unsampled('m'), {quiet['ts']})          # only the quiet one is left out
        self.assertEqual(metrics.judgments_for_stats('m'), [])           # the random sample stays unbiased
        self.assertTrue(metrics.reaches_operator({'tools': [{'tool': 'send_message',
                                                             'result': 'delivered to the operator (note)'}]}))
        self.assertFalse(metrics.reaches_operator(quiet))

    def test_244_missed_the_ask_is_planted_and_its_catch_rate_reported_apart(self):
        """#244: the catch rate (6/6) measured only refused deliveries; "delivered, but not what was asked" was never
        planted. The swapped text comes from a message for another goal; the rate is per kind, never pooled."""
        other = {'kind': 'live', 'ts': (NOW - timedelta(hours=3)).isoformat(), 'intent': {'goal': 'expense categories'},
                 'finish': {'delivered_via': 'message'}, 'tools': [
                     {'tool': 'send_message', 'args': '{"to":"operator","text":"Here are the categories."}',
                      'result': 'delivered'}]}
        metrics.live_episodes.return_value = [other, self.ep]
        self.write('judgments/m.jsonl', [{'episode_ts': ep['ts'], 'delivery_supported': 'yes', 'criterion_met': 'yes',
                                          'confidence_of': None} for ep in (other, self.ep)])
        original = copy.deepcopy(self.ep)
        verdicts = iter([dict(VERDICT, delivery_supported='yes', criterion_met='yes'),     # refused: missed
                         dict(VERDICT, delivery_supported='yes', criterion_met='no')])     # swapped: caught
        self.ask.side_effect = lambda *a, **k: next(verdicts)
        self.assertEqual([judge_probe.run('m'), judge_probe.run('m')], [1, 1])   # one probe each
        refused, missed = judge_probe.records('m')
        self.assertEqual((refused['defect'], refused['caught'], missed['defect'], missed['caught']),
                         ('delivery refused', False, 'missed the ask', True))
        case = self.ask.call_args.args[1]
        self.assertIn('Here are the categories.', case)
        self.assertNotIn('ERROR: delivery refused', case)
        self.assertEqual(self.ep, original)
        self.assertEqual(judge_probe.line('m'), 'judge catch rate over 30 days: delivery refused 0/1; missed the ask 1/1')
        self.assertIsNone(judge_probe.plant_missed(self.ep, [self.ep]))   # nothing sent for another goal: not planted

    def test_244_review_a_retry_keeps_the_draw_and_the_probe_never_swaps_in_the_same_ask(self):
        """Codex's review of #244: a run drawn at random whose judge was down drew again on the retry and could come
        back operator-selected (out of the stats sample); and a goal worded differently for the same ask was used as
        the "other" message, planting a correct answer."""
        draws = iter([0, 1])   # the first draw selects it at random; a redraw would leave it out
        with mock.patch.object(judge, 'JUDGE_EVERY', 2), \
                mock.patch.object(metrics.secrets, 'randbelow', side_effect=lambda n: next(draws)):
            with mock.patch.object(judge, 'available', return_value=False), \
                    mock.patch.object(judge, 'unavailable', return_value='down'):
                self.assertEqual(metrics.judge_pending('m'), 0)
            self.assertEqual(metrics.judge_pending('m'), 1)
        verdict, = metrics.judgments('m')
        self.assertNotIn('operator_selected', verdict)
        self.assertEqual(len(metrics.judgments_for_stats('m')), 1)
        same = {'kind': 'live', 'ts': (NOW - timedelta(hours=3)).isoformat(),
                'intent': {'goal': 'Send the operator the number of trees', 'success_criterion': 'Operator has the tree count'},
                'finish': {'delivered_via': 'message'}, 'tools': [
                    {'tool': 'send_message', 'args': '{"to":"operator","text":"Eight trees."}', 'result': 'delivered'}]}
        target = {**self.ep, 'intent': {'goal': 'Report the tree count', 'success_criterion': 'Operator has the tree count'}}
        self.assertIsNone(judge_probe.plant_missed(target, [same]))

    def test_245_review_a_past_review_computes_its_weeks_at_its_own_time(self):
        """Codex's review of #245: review_text(now=...) passed no time to the weekly line, which used the clock."""
        past = datetime(2026, 11, 10, 12)
        self.write('judgments/m.jsonl', [dict(VERDICT, episode_ts=(past - timedelta(hours=1)).isoformat(),
                                              judged_at=(past - timedelta(hours=1)).isoformat())])
        with mock.patch('supervisor.metacog.review_line', return_value='x') as line:
            metrics.review_text(['m'], now=past)
        self.assertEqual(line.call_args.args[1:], (past,))

    def test_m7d_cap_is_three_per_rolling_seven_days(self):
        episodes = [dict(self.ep, ts=(NOW - timedelta(hours=i)).isoformat()) for i in range(5)]
        metrics.live_episodes.return_value = episodes
        self.write('judgments/m.jsonl', [{'episode_ts': ep['ts'], 'delivery_supported': 'yes', 'confidence_of': None} for ep in episodes])
        self.assertEqual([judge_probe.run('m') for _ in range(5)], [1, 1, 1, 0, 0])
        self.assertEqual(self.ask.call_count, 3)
        self.assertIn('delivery refused 0/3', judge_probe.line('m'))

    def test_m7d_cap_expires_after_seven_days_and_report_after_thirty(self):
        self.write('judge-catch/m.jsonl', [{'at': (NOW - timedelta(days=8)).isoformat(), 'episode_ts': f'old{i}', 'caught': True}
                                          for i in range(3)])
        self.write('judgments/m.jsonl', [{'episode_ts': self.ep['ts'], 'delivery_supported': 'yes', 'confidence_of': None}])
        self.assertEqual(judge_probe.run('m'), 1)
        self.assertEqual(judge_probe.line('m', NOW + timedelta(days=31)), 'judge catch rate over 30 days: delivery refused unmeasured; missed the ask unmeasured')

    def test_m7d_refusal_does_not_plant_an_already_failed_call(self):
        self.ep['tools'][0]['result'] = 'ERROR: refused'
        self.assertIsNone(judge_probe.plant(self.ep))

    def test_m7d_catch_cli_reports_without_judging(self):
        from supervisor import __main__ as cli
        with mock.patch.object(cli, 'instance_root'), mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['judge', 'catch', 'm']), 0)
        self.assertEqual(output.getvalue(), 'm: judge catch rate over 30 days: delivery refused unmeasured; missed the ask unmeasured\n')
        self.ask.assert_not_called()

    def test_m7d_unavailable_and_no_rows_are_unmeasured(self):
        with mock.patch.object(judge, 'available', return_value=False):
            self.assertEqual(judge_probe.run('m'), 0)
        self.assertEqual(judge_probe.line('m'), 'judge catch rate over 30 days: delivery refused unmeasured; missed the ask unmeasured')
        self.ask.assert_not_called()

    def test_m7d_scheduler_measurement_hook_runs_probe(self):
        with mock.patch.object(metrics, 'judge_pending', return_value=0), mock.patch.object(judge_probe, 'run') as run, \
                mock.patch('supervisor.tom.score'), mock.patch.object(metrics, 'write_stats'), mock.patch.object(metrics, 'check_probation'):
            metrics.after_cycle('m')
        run.assert_called_once_with('m')

    def test_unreadable_commitment_ledger_does_not_stop_judging(self):
        self.ledger.side_effect = IsADirectoryError('ledger is a directory')
        self.assertEqual(metrics.judge_pending('m'), 1)
        self.assertEqual(metrics.judgments('m')[0]['should_have_waited'], 'yes')
        self.assertEqual(metrics.log.call_count, 1)
        self.assertIn('commitment citations could not be read', metrics.log.call_args.args[0])

    def test_cited_only_episode_does_not_spend_second_judge(self):
        self.write('judgments/m-unsampled.jsonl', [{'episode_ts': self.ep['ts']}])
        self.ledger.return_value = {'c': {'status': 'kept', 'evidence': self.ep['ts']}}
        with mock.patch.object(metrics, 'second_sampled', side_effect=AssertionError('paid sampling')):
            self.assertEqual(metrics.judge_pending('m'), 1)
        self.assertEqual(metrics.judgments('m')[0]['cited'], True)

    def test_operator_origin_without_inbox_adds_fact_but_keeps_judge_verdict(self):
        self.ep['intent']['origin'] = 'operator'
        self.assertEqual(metrics.judge_pending('m'), 1)
        self.assertIn('answers_operator: true', self.ask.call_args.args[1])
        self.assertEqual(metrics.judgments('m')[0]['should_have_waited'], 'yes')

    def test_stats_inspection_never_runs_paid_probe(self):
        from supervisor import __main__ as cli
        with mock.patch.object(cli, 'instance_root'), mock.patch.object(metrics, 'judge_pending', return_value=0), \
                mock.patch.object(metrics, 'stats_text', return_value='stats'), \
                mock.patch.object(judge_probe, 'run', side_effect=AssertionError('paid probe')), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['stats', 'm']), 0)
        self.assertEqual(output.getvalue(), 'stats\n')

    def test_probe_skips_torn_json_nonobjects_and_invalid_dates(self):
        path = judge_probe._path('m')
        path.parent.mkdir()
        path.write_text('torn\nnull\n{}\n{"at": "invalid"}\n{"at": 123}\n'
                        '{"at": "2026-11-06T12:00:00garbage"}\n' + json.dumps(
            {'at': NOW.isoformat(), 'episode_ts': 'old', 'caught': True}) + '\n')
        self.assertEqual(judge_probe.line('m'), 'judge catch rate over 30 days: delivery refused 1/1; missed the ask unmeasured')
        self.write('judgments/m.jsonl', [{'episode_ts': self.ep['ts'], 'delivery_supported': 'yes', 'confidence_of': None}])
        self.assertEqual(judge_probe.run('m'), 1)
        self.assertEqual(judge_probe.line('m'), 'judge catch rate over 30 days: delivery refused 1/2; missed the ask unmeasured')

    def test_failed_probe_attempts_count_toward_cap_but_not_catch_rate(self):
        episodes = [dict(self.ep, ts=(NOW - timedelta(hours=i)).isoformat()) for i in range(5)]
        metrics.live_episodes.return_value = episodes
        self.write('judgments/m.jsonl', [{'episode_ts': ep['ts'], 'delivery_supported': 'yes', 'confidence_of': None} for ep in episodes])
        self.ask.side_effect = judge.JudgeError('paid failure')
        self.assertEqual([judge_probe.run('m') for _ in range(5)], [0, 0, 0, 0, 0])
        rows = judge_probe.records('m')
        self.assertEqual([r['error'] for r in rows], ['paid failure'] * 3)
        self.assertEqual(self.ask.call_count, 3)
        self.assertTrue(all('caught' not in r for r in rows))
        self.assertEqual(judge_probe.line('m'), 'judge catch rate over 30 days: delivery refused unmeasured; missed the ask unmeasured')


class PlanTest(Fixture):
    def config(self, model):
        (self.roots / 'm/config.env').write_text(f'LLM_BASE_URL=broker\nLLM_MODEL={model}\n')

    def test_m9_plan_uses_shared_saved_rate_and_local_reset_date(self):
        self.config('codex/gpt-5.4')
        with mock.patch.object(planchannel, 'saved_rate', return_value={'used_percent': 42, 'resets_at': NOW.timestamp()}) as rate:
            self.assertEqual(metrics.plan_lines('m'), ['Plan: 42% of weekly allowance used (resets 2026-11-06)'])
        rate.assert_called_once_with('codex')

    def test_m9_plan_allowance_reaches_instance_stats_text(self):
        self.config('codex/gpt-5.4')
        with mock.patch.object(planchannel, 'saved_rate', return_value={'used_percent': 42, 'resets_at': NOW.timestamp()}), \
                mock.patch.object(metrics, 'judgments', return_value=[]), mock.patch.object(metrics, '_sections', return_value=[]), \
                mock.patch.object(metrics, '_battery_section', return_value=[]), mock.patch.object(metrics, '_code_section', return_value=[]), \
                mock.patch.object(metrics, '_traits_section', return_value=[]), mock.patch.object(metrics, '_trials_section', return_value=[]):
            self.assertIn('Plan: 42% of weekly allowance used (resets 2026-11-06)', metrics.stats_text('m'))
            self.config('openrouter/model')
            self.assertNotIn('Plan:', metrics.stats_text('m'))

    def test_m9_missing_rate_unknown_cloud_has_no_plan_line(self):
        self.config('codex/gpt-5.4')
        with mock.patch.object(planchannel, 'saved_rate', return_value=None):
            self.assertEqual(metrics.plan_lines('m'), ['Plan: unknown of weekly allowance used (resets unknown)'])
        self.config('openrouter/model')
        with mock.patch.object(planchannel, 'saved_rate') as rate:
            self.assertEqual(metrics.plan_lines('m'), [])
            rate.assert_not_called()

    def test_m9_project_stats_label_plan_cost_without_altering_cloud_cost(self):
        root = self.roots / 'm'
        (root / 'memory').mkdir()
        (root / 'memory/projects.jsonl').write_text(json.dumps({'event': 'opened', 'id': 'p2026-1106-1', 'at': '2026-11-01T00:00:00',
            'name': 'report', 'success': 'delivered', 'deadline': '2026-12-01T00:00:00'}) + '\n')
        with mock.patch.object(metrics, 'live_episodes', return_value=[]), mock.patch.object(metrics, 'judgments', return_value=[]):
            self.config('codex/gpt-5.4')
            self.assertIn('plan: no per-call cost', '\n'.join(projects.section('m')))
            self.config('openrouter/model')
            self.assertIn('USD 0.000000/unbounded', '\n'.join(projects.section('m')))
