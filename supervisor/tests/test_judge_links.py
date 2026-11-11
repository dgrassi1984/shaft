"""Judgment links name only shown goals and preserve older stored verdicts."""
from datetime import timedelta
from unittest import mock

from supervisor import factledger, families, judge, metrics
from supervisor.tests.test_idle_exit import IsolatedCase, NOW


def verdict(**updates):
    return {"criterion_met": "yes", "claim_supported": True, "criterion_quality": "checkable",
            "failure_mode": "", "reasoning": "Report checked", "origin": "self", "origin_supported": True,
            "origin_evidence": "Own goal", "difficulty": 3, "difficulty_reason": "Checking report", "novelty": 2,
            "value": 3, "value_for": "own_capability", "value_reason": "Checked report", "delivery_supported": "na",
            "deliverable_met": "yes", "self_imposed_clauses": "none", "self_audit": False,
            "should_have_waited": "na", "pushback": "none", "first_error_step": 0, "first_error": "No error",
            "expectations": [], "unreported_surprises": [], "reported_surprises_supported": True, "followup_useful": "na",
            "resembles": "", "unsupported_claim": "", **updates}


class JudgeLinksTest(IsolatedCase):
    def test_schema_requires_links_but_old_stored_rows_load_into_families(self):
        for key in ("resembles", "unsupported_claim"):
            self.assertIn(key, metrics.JUDGE_SCHEMA["required"])
            self.assertIn(key, metrics.LINK_PROPS)
        old = {"episode_ts": NOW.isoformat(), "goal": "Check report"}
        self.write(self.sup / "judgments/ember.jsonl", [old])
        self.assertEqual(metrics.judgments("ember"), [old])
        family, = families.families("ember")
        self.assertEqual(family["members"], [NOW.isoformat()])

    def test_prior_keys_drop_empty_goals_limit_and_keep_newest_last_space_keys(self):
        rows = [{"episode_ts": (NOW + timedelta(minutes=i)).isoformat(), "goal": f"Goal {i}"}
                for i in range(metrics.PRIOR_GOALS + 3)]
        rows.insert(2, {"episode_ts": NOW.isoformat(), "goal": "  ", "judge": "rule"})
        before = (NOW + timedelta(days=1)).isoformat()
        keys = metrics._prior_keys(rows, before)
        self.assertEqual(keys, [(r["episode_ts"][:16].replace("T", " "), r["goal"]) for r in rows[4:]])
        self.assertEqual(metrics._prior_goals(rows, before), "\n".join(f"- {k}: {g}" for k, g in keys))
        self.assertEqual(metrics._prior_keys(rows, NOW.isoformat()), [])

    def test_links_accept_only_shown_keys_and_unsupported_false_claim_quotes(self):
        rows = [{"episode_ts": NOW.isoformat(), "goal": "Check report"}]
        before = (NOW + timedelta(hours=1)).isoformat()
        for named, expected in [("2026-11-11T12:00", "2026-11-11 12:00"),
                                ("2026-11-11 12:00", "2026-11-11 12:00"), ("2026-11-10 12:00", "")]:
            v = verdict(resembles=named, claim_supported=False, unsupported_claim="  claim\n " + "word " * 80)
            metrics._links(v, rows, before)
            self.assertEqual(v["resembles"], expected)
            self.assertEqual(v["unsupported_claim"], ("claim " + "word " * 80).strip()[:200])
        for supported in (True, None, 0):
            v = verdict(claim_supported=supported, unsupported_claim="claim")
            metrics._links(v, rows, before)
            self.assertEqual(v["unsupported_claim"], "")

    def judge_one(self, v):
        later = (NOW + timedelta(hours=1)).isoformat()
        self.write(self.root / "memory/episodes/day.jsonl", [{"kind": "live", "ts": later,
            "intent": {"goal": "Continue checking report", "origin": "self"}, "tools": [], "steps": 1}])
        for name, value in [("second_sampled", False), ("cycle_model", "example-model"),
                            ("own_models", ["example-model"]), ("_written_files", ""),
                            ("_provenance_evidence", ""), ("_followed_surprise", "")]:
            self.enterContext(mock.patch.object(metrics, name, return_value=value))
        with mock.patch.object(judge, "JUDGE_EVERY", 1), mock.patch.object(judge, "available", return_value=True), \
             mock.patch.object(judge, "pick", return_value="example-judge"), mock.patch.object(judge, "ask", return_value=v) as ask:
            self.assertEqual(metrics.judge_pending("ember"), 1)
            ask.assert_called_once()
        return metrics.judgments("ember")[-1]

    def test_judge_persists_resemblance_and_families_join_cycles(self):
        self.write(self.sup / "judgments/ember.jsonl", [{"episode_ts": NOW.isoformat(), "goal": "Check report"}])
        row = self.judge_one(verdict(resembles="2026-11-11 12:00"))
        self.assertEqual(row["resembles"], "2026-11-11 12:00")
        family, = families.families("ember")
        self.assertEqual(family["members"], [NOW.isoformat(), row["episode_ts"]])

    def test_unsupported_claim_names_unsourced_fact(self):
        with mock.patch.object(factledger, "match_claim", return_value={"date": "2026-11-10", "text": "Report exists"}) as match:
            row = self.judge_one(verdict(claim_supported=False, unsupported_claim="Report exists"))
        match.assert_called_once_with("ember", "Report exists")
        self.assertEqual(row["unsourced_fact"], "[2026-11-10] Report exists")

    def test_fact_match_failure_does_not_discard_judgment(self):
        with mock.patch.object(factledger, "match_claim", side_effect=ValueError("broken facts")):
            row = self.judge_one(verdict(claim_supported=False, unsupported_claim="Report exists"))
        self.assertEqual(row["unsupported_claim"], "Report exists")
        self.assertEqual(row["judge"], "example-judge")
