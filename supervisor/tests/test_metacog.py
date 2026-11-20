"""Synthetic checks of confidence discrimination, bias and paired base-rate ranking."""
from __future__ import annotations

import math
import unittest
from unittest import mock

from supervisor import judge, metacog, metrics


def row(confidence, met, day=1, **extra):
    return {"episode_ts": f"2026-10-{day:02d}T10:00:00", "confidence": confidence,
            "confidence_of": metrics.CONFIDENCE_V2, "criterion_met": "yes" if met else "no",
            "judge": judge.JUDGE_MODELS[0], **extra}


def paired(worse=False):
    return [row(100 * (not met if worse else met), met, day,
                confidence_of=metrics.CONFIDENCE_V3, confidence_first=100 * met)
            for day in range(1, 21) for met in (True, False)]


class MetacogTest(unittest.TestCase):
    def test_auroc_hand_calculations(self):
        self.assertEqual(metacog.auroc([1], [0]), 1)
        self.assertEqual(metacog.auroc([1, 1], [1, 1]), 0.5)
        self.assertEqual(metacog.auroc([0.8, 0.6], [0.6, 0.2]), 0.875)
        self.assertIsNone(metacog.auroc([], [0]))
        self.assertIsNone(metacog.auroc([1], []))

    def test_brier_identity_and_top_bin(self):
        pairs = [(0.2, 0), (0.2, 1), (0.6, 0), (0.6, 1), (1.0, 1)]
        d = metacog.brier_decomposition(pairs)
        self.assertEqual(d['n'], 5)
        self.assertAlmostEqual(d['brier'], 0.24, delta=1e-12)
        self.assertAlmostEqual(d['brier'], d['reliability'] - d['resolution'] + d['uncertainty'], delta=1e-12)
        # At bins=1, 1.0 must share the top bin with every other forecast.
        self.assertAlmostEqual(metacog.brier_decomposition([(0, 0), (1, 1)], bins=1)['resolution'], 0)

    def test_meta_i_independent_and_perfect(self):
        independent = [(p, y) for p in (0.1, 0.9) for y in (0, 1)] * 10
        self.assertEqual(metacog.meta_i(independent)['bits'], 0)
        perfect = [(0.1, 0), (0.9, 1)] * 20
        # H(outcome)=1 bit; two confidence bins and two outcomes give correction -1/(2*n*ln(2)).
        expected = 1 - 1 / (80 * math.log(2))
        d = metacog.meta_i(perfect)
        self.assertAlmostEqual(d['bits'], expected, delta=1e-12)
        self.assertAlmostEqual(d['share'], expected, delta=1e-12)
        self.assertEqual(d['n'], 40)
        self.assertEqual(metacog.meta_i([(1, 1)]), {'bits': 0, 'share': None, 'n': 1})

    def test_empty_statistics(self):
        self.assertIsNone(metacog.brier_decomposition([]))
        self.assertIsNone(metacog.meta_i([]))
        self.assertIsNone(metacog.cluster_ci([], lambda rows: 0))
        s = metacog.summary([])
        self.assertEqual(s['n'], 0)
        self.assertIsNone(s['bias'])
        self.assertEqual(s['h1'], 'not shown')
        self.assertIn('unavailable', metacog.review_line([]))
        self.assertEqual(metacog.paired_v3([])['verdict'], 'not enough data')

    def test_cluster_ci_seed_and_missing_resamples(self):
        rows = [row(c, True, day) for day, c in enumerate((10, 30, 90, 50, 70), 1)]
        stat = lambda rs: sum(r['confidence'] for r in rs) / len(rs)
        ci = metacog.cluster_ci(rows, stat, n_boot=100, seed='test')
        self.assertEqual(ci, metacog.cluster_ci(rows, stat, n_boot=100, seed='test'))
        self.assertLess(ci[0], 50)
        self.assertGreater(ci[1], 50)
        self.assertIsNone(metacog.cluster_ci(rows[:1], stat))
        self.assertIsNone(metacog.cluster_ci(rows, lambda rs: None, n_boot=100))
        self.assertIsNone(metacog.cluster_ci([row(0, False), row(100, True, 2)], metacog._auc))
        self.assertEqual(metacog.cluster_ci(rows, lambda rs: 0.5), (0.5, 0.5))

    def test_no_interval_from_fewer_than_five_days(self):
        """Live, 2026-10-27: shaft's stats.md read "AUROC 0.60 [0.17, 0.64]" from three days of v3 cycles — ten
        distinct resamples, the estimate at the interval's edge. Below MIN_DAYS there is no interval."""
        rows = [row(c, c > 50, day) for day in (1, 2, 3) for c in (20, 40, 60, 80, 30, 70)]
        self.assertIsNone(metacog.cluster_ci(rows, metacog._auc))
        more = rows + [row(c, c > 50, day) for day in (4, 5) for c in (20, 40, 60, 80, 30, 70)]
        self.assertIsNotNone(metacog.cluster_ci(more, metacog._auc))

    def test_domains(self):
        examples = [('coding', 'run', '{}'), ('coding', 'delegate_coding', '{}'),
                    ('coding', 'append_file', '{"path":"plugins/a.py"}'),
                    ('research', 'decide', '{}'), ('research', 'web_search', '{}'),
                    ('upkeep', 'read_file', '{"path":"shaft/a.py"}'),
                    ('upkeep', 'write_file', '{"path":"./self/a.md"}'),
                    ('upkeep', 'remember', '{}'), ('social', 'send_message', '{}'),
                    ('social', 'list_instances', '{}'),
                    ('coding', 'write_file', '{"path": "tests/test_a.py", "content": "cut')]
        for expected, tool, args in examples:
            with self.subTest(tool=tool, args=args):
                self.assertEqual(metacog.domain({'tools': [{'tool': tool, 'args': args}]}), expected)
        self.assertEqual(metacog.domain({'tools': [{'tool': 'run'}, {'tool': 'fetch_page'}]}), 'mixed')
        self.assertEqual(metacog.domain({'tools': [{'tool': 'run'}, {'tool': 'run'}, {'tool': 'fetch_page'}]}), 'coding')
        self.assertEqual(metacog.domain({'domain': 'coding', 'tools': [{'tool': 'finish_cycle'},
                         {'tool': 'confirm_confidence'}, {'tool': 'declare_intent'}, {'tool': 'browser'}]}), 'none')
        self.assertEqual(metacog.domain({}), 'none')

    def test_summary_versions_judges_claims_and_domains(self):
        rows = [row(80, True, claimed='yes'), row(20, False, claimed='no'),
                row(0, True, confidence_of=None, claimed='no'),
                row(100, False, confidence_of=None, claimed='yes'),
                row(0, True, judge='retired', claimed='no'),
                row(0, True, confidence_of='unknown', claimed='no')]
        with mock.patch.object(metrics, 'by_current_judge', side_effect=lambda r: r['judge'] != 'retired'), \
                mock.patch.object(metrics, 'second_counted', side_effect=lambda r: 'second' in r):
            rows[0]['second'] = {'criterion_met': 'yes'}
            rows[1]['second'] = {'criterion_met': 'partial'}
            s = metacog.summary(rows, {rows[0]['episode_ts']: {'tools': [{'tool': 'run'}]}})
        self.assertEqual(s['n'], 2)
        self.assertEqual(s['met'], 1)
        self.assertEqual(s['auroc'], 1)
        self.assertEqual(s['claim_auroc'], 0.5)
        self.assertEqual(s['second_agreement'], (1, 2))
        self.assertEqual(s['mean_confidence'], 50)
        self.assertEqual(s['sd'], 30)
        self.assertEqual(s['bias'], 0)
        self.assertEqual(s['by_domain'], {'coding': {'n': 2, 'hit_rate': 0.5, 'auroc': 1}})

    def test_claims_without_confidence(self):
        s = metacog.summary([row(None, True, claimed='yes'), row(None, False, claimed='partial')])
        self.assertEqual(s['n'], 0)
        self.assertEqual(s['claim_auroc'], 1)

    def test_degenerate_outcomes(self):
        for met in (True, False):
            with self.subTest(met=met):
                s = metacog.summary([row(50, met, day) for day in range(1, 4)])
                self.assertIsNone(s['auroc'])
                self.assertIsNone(s['ci'])
                self.assertEqual(s['sd'], 0)
                self.assertIsNone(s['meta_i']['share'])

    def test_paired_floor_and_verdicts(self):
        self.assertEqual(metacog.paired_v3(paired()[:39])['verdict'], 'not enough data')
        good = metacog.paired_v3(paired())
        self.assertEqual((good['n'], good['failures']), (40, 20))
        self.assertEqual((good['auroc_first'], good['auroc_final'], good['diff'], good['ci']), (1, 1, 0, (0, 0)))
        self.assertEqual(good['verdict'], 'non-inferior')
        bad = metacog.paired_v3(paired(worse=True))
        self.assertEqual((bad['diff'], bad['ci'], bad['verdict']), (-1, (-1, -1), 'worse'))
        one_day = [{**r, 'episode_ts': '2026-10-01T10:00:00'} for r in paired()]
        self.assertEqual(metacog.paired_v3(one_day)['verdict'], 'inconclusive')
        self.assertEqual(metacog.paired_v3([row(50, True, confidence_first=50)])['n'], 0)
        all_met = [row(50, True, confidence_of=metrics.CONFIDENCE_V3, confidence_first=50)] * 40
        self.assertEqual(metacog.paired_v3(all_met)['verdict'], 'not enough data')

    def test_section_and_report(self):
        with mock.patch.object(metrics, 'judgments', return_value=paired()[:9]), \
                mock.patch.object(metrics, 'live_episodes', return_value=[]):
            self.assertEqual(metacog.section('synthetic'), [])
        with mock.patch.object(metrics, 'judgments', return_value=paired()), \
                mock.patch.object(metrics, 'live_episodes', return_value=[]):
            lines = metacog.section('synthetic')
            text = metacog.report(['synthetic'])
        self.assertTrue(lines[0].startswith('## '))
        self.assertLessEqual(len(lines), 11)   # #245 added the weekly line
        self.assertIn('judge sees my claim', '\n'.join(lines))
        self.assertIn('Paired v3: non-inferior', text)
        self.assertIn('H1: above chance', text)
        self.assertIn('none: n 40', text)
        self.assertIn('shaft anchoring', text)
        self.assertEqual(metacog.review_line(paired()),
                         'confidence AUROC 1.00 [1.00, 1.00] (n 40), claim AUROC unavailable, bias +0.00, SD 50.0'
                         '; by week (ending): unavailable')   # #245: never the pooled figure alone

    def test_section_waits_for_five_misses(self):
        """Regression: wisp had 12 met and 1 missed v2/v3 cycle on 2026-10-26, and stats.md would have told it
        "AUROC 0.12" from that one miss. Below MIN_CLASS met or missed cycles the section is left out."""
        rows = [{'episode_ts': f'2026-10-{20 + i % 5:02d}T10:{i:02d}:00', 'confidence': 80,
                 'confidence_of': 'criterion_met=yes', 'criterion_met': 'yes', 'judge': 'j'} for i in range(12)]
        rows.append({**rows[0], 'episode_ts': '2026-10-24T11:00:00', 'confidence': 85, 'criterion_met': 'no'})
        with mock.patch.object(metrics, 'judgments', return_value=rows), \
                mock.patch.object(metrics, 'live_episodes', return_value=[]), \
                mock.patch.object(metrics, 'by_current_judge', return_value=True):
            self.assertEqual(metacog.section('wisp'), [])


if __name__ == '__main__':
    unittest.main()
