"""Judgment links name only shown goals and preserve older stored verdicts."""
from datetime import timedelta
from unittest import mock

from supervisor import families, judge, metrics
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
    def test_only_main_schema_requires_links_and_old_rows_still_load_into_families(self):
        """Link questions must not alter control schemas or break older stored verdicts."""
        for key in ("resembles", "unsupported_claim"):
            self.assertNotIn(key, metrics.JUDGE_SCHEMA["properties"])
            self.assertNotIn(key, metrics.JUDGE_SCHEMA["required"])
            self.assertIn(key, metrics.MAIN_SCHEMA["required"])
            self.assertIn(key, metrics.LINK_PROPS)
        self.assertEqual(metrics.MAIN_SCHEMA["properties"],
                         {**metrics.JUDGE_SCHEMA["properties"], **metrics.LINK_PROPS})
        self.assertEqual(set(metrics.MAIN_SCHEMA["required"]),
                         set(metrics.JUDGE_SCHEMA["required"]) | set(metrics.LINK_PROPS))
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
            self.assertIs(ask.call_args.args[2], metrics.MAIN_SCHEMA)
        return metrics.judgments("ember")[-1]

    def test_judge_persists_resemblance_and_families_join_cycles(self):
        self.write(self.sup / "judgments/ember.jsonl", [{"episode_ts": NOW.isoformat(), "goal": "Check report"}])
        row = self.judge_one(verdict(resembles="2026-11-11 12:00"))
        self.assertEqual(row["resembles"], "2026-11-11 12:00")
        family, = families.families("ember")
        self.assertEqual(family["members"], [NOW.isoformat(), row["episode_ts"]])

    def test_judge_persists_unsupported_claim(self):
        path = self.sup / "judgments/ember.jsonl"
        self.write(path, [{"episode_ts": NOW.isoformat(), "claim_supported": False,
                           "unsupported_claim": "Earlier claim", "unsourced_fact": "Old match"}])
        row = self.judge_one(verdict(claim_supported=False, unsupported_claim="Report exists"))
        self.assertFalse(row["claim_supported"])
        self.assertEqual(row["unsupported_claim"], "Report exists")
        self.assertEqual(row["judge"], "example-judge")
        earlier = metrics.judgments("ember")[0]
        self.assertEqual(earlier["unsupported_claim"], "Earlier claim")
        self.assertNotIn("unsourced_fact", earlier)
        self.assertNotIn("unsourced_fact", path.read_text().splitlines()[-1])

    def test_missing_link_fields_are_defaulted_without_discarding_verdict(self):
        """Older judge responses without links must still produce a stored judgment."""
        v = verdict()
        del v["resembles"], v["unsupported_claim"]
        row = self.judge_one(v)
        self.assertEqual((row["resembles"], row["unsupported_claim"]), ("", ""))

    def test_second_judge_uses_schema_without_main_only_links(self):
        """Adding main-judge links must not change the blind second-judge question."""
        with mock.patch.object(judge, "ask_second", return_value=verdict()) as ask:
            self.assertEqual(metrics.second_opinion("ember", "example case")["criterion_met"], "yes")
        self.assertIs(ask.call_args.args[2], metrics.JUDGE_SCHEMA)

    def test_baseline_and_control_keep_their_original_judge_schemas(self):
        """Main-only link fields must not alter either arm of the surprise comparison."""
        ep = {"kind": "live", "ts": NOW.isoformat(), "intent": {"goal": "Check report"}, "tools": []}
        self.write(self.root / "memory/episodes/day.jsonl", [ep])
        self.write(self.sup / "judgments/ember.jsonl", [{"episode_ts": ep["ts"], "judge": "example-judge"}])
        with mock.patch.object(metrics, "own_models", return_value=["example-model"]), \
             mock.patch.object(metrics, "_judgment_cost", return_value=(0, "fixture")), \
             mock.patch.object(metrics, "_written_files", return_value=""), \
             mock.patch.object(metrics, "_provenance_evidence", return_value=""), \
             mock.patch.object(metrics, "_followed_surprise", return_value=""), \
             mock.patch.object(metrics, "cycle_model", return_value="example-model"), \
             mock.patch.object(judge, "ask", side_effect=lambda *a, **kw: verdict()) as ask:
            self.assertEqual(metrics.surprise_baseline("ember")["done"], 1)
            self.assertIs(ask.call_args.args[2], metrics.JUDGE_SCHEMA)
            ask.reset_mock()
            self.assertEqual(metrics.surprise_control("ember")["done"], 1)
            ask.assert_called_once()
            self.assertIs(ask.call_args.args[2], metrics.CONTROL_SCHEMA)
        for schema in (metrics.JUDGE_SCHEMA, metrics.CONTROL_SCHEMA):
            self.assertTrue(set(metrics.LINK_PROPS).isdisjoint(schema["properties"]))
