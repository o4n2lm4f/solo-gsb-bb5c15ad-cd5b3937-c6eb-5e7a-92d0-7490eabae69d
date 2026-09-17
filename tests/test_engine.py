"""Unit tests that run with the Python standard library only.

Run with:  python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db as db_mod  # noqa: E402
from app.comparison import (  # noqa: E402
    build_reverse_graph,
    compare_versions,
    diff_flags,
    impact_analysis,
)
from app.evaluator import evaluate_flag, hash_bucket  # noqa: E402
from app.validation import ConfigValidationError, validate_config  # noqa: E402


def flag(**kw):
    base = {"enabled": True, "default": False, "prerequisites": [], "rules": []}
    base.update(kw)
    return base


def rule(conditions, value=True, id=None):
    r = {"conditions": conditions, "value": value}
    if id is not None:
        r["id"] = id
    return r


class RuleEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.flags = {
            "beta": flag(
                rules=[
                    rule([{"attr": "tier", "op": "eq", "value": "gold"}], value="gold-banner", id="gold"),
                    rule([{"attr": "tier", "op": "in", "value": ["silver", "bronze"]}], id="tiers"),
                ],
                default="off",
            ),
        }

    def test_first_matching_rule_wins_in_order(self):
        res = evaluate_flag("beta", self.flags, attributes={"tier": "gold"})
        self.assertTrue(res["hit"])
        self.assertEqual(res["value"], "gold-banner")
        self.assertEqual(res["reason"], "rule_match")
        self.assertEqual(res["rule_id"], "gold")

    def test_second_rule_matches(self):
        res = evaluate_flag("beta", self.flags, attributes={"tier": "silver"})
        self.assertEqual(res["rule_id"], "tiers")

    def test_default_when_no_rule_matches(self):
        res = evaluate_flag("beta", self.flags, attributes={"tier": "free"})
        self.assertEqual(res["reason"], "default")
        self.assertEqual(res["value"], "off")
        # "off" is a non-empty string variant; hit is the boolean coercion of
        # the value. Boolean-style flags should use true/false defaults.
        self.assertTrue(res["hit"])

    def test_boolean_default_flag_is_off(self):
        flags = {"f": flag(default=False, rules=[rule([{"attr": "x", "op": "eq", "value": 1}])])}
        res = evaluate_flag("f", flags, attributes={"x": 0})
        self.assertEqual(res["reason"], "default")
        self.assertFalse(res["hit"])

    def test_missing_attribute_is_none(self):
        res = evaluate_flag("beta", self.flags, attributes={})
        self.assertEqual(res["reason"], "default")

    def test_comparison_and_string_ops(self):
        flags = {
            "f": flag(
                rules=[
                    rule([{"attr": "age", "op": "ge", "value": 18}], id="adult"),
                ]
            ),
            "p": flag(
                rules=[rule([{"attr": "email", "op": "suffix", "value": "@acme.com"}], id="acme")]
            ),
        }
        self.assertEqual(evaluate_flag("f", flags, attributes={"age": 21})["rule_id"], "adult")
        self.assertFalse(evaluate_flag("f", flags, attributes={"age": 5})["hit"])
        self.assertTrue(evaluate_flag("p", flags, attributes={"email": "a@acme.com"})["hit"])
        self.assertFalse(evaluate_flag("p", flags, attributes={"email": "a@other.com"})["hit"])

    def test_catch_all_rule_with_empty_conditions(self):
        flags = {"f": flag(rules=[{"conditions": [], "value": True, "id": "all"}])}
        self.assertEqual(evaluate_flag("f", flags, attributes={})["rule_id"], "all")


class DisabledFlagTests(unittest.TestCase):
    def test_disabled_returns_default_even_with_rules(self):
        flags = {"f": flag(enabled=False, default=False, rules=[rule([], id="r")])}
        res = evaluate_flag("f", flags, attributes={"x": 1})
        self.assertEqual(res["reason"], "disabled")
        self.assertFalse(res["hit"])


class PrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.flags = {
            "logged_in": flag(default=False, rules=[rule([{"attr": "uid", "op": "ne", "value": None}], id="has-uid")]),
            "v2_api": flag(
                prerequisites=[{"flag": "logged_in", "expect": True}],
                default=False,
                rules=[rule([{"attr": "tenant", "op": "eq", "value": "acme"}], id="acme")],
            ),
        }

    def test_chain_when_prerequisite_passes(self):
        res = evaluate_flag("v2_api", self.flags, attributes={"uid": "u-1", "tenant": "acme"})
        self.assertTrue(res["hit"])
        names = [n["flag"] for n in res["chain"]]
        self.assertEqual(names, ["logged_in", "v2_api"])
        self.assertEqual(res["chain"][0]["prerequisite_of"], "v2_api")
        self.assertTrue(res["chain"][0]["expected"])
        self.assertEqual(res["chain"][1]["reason"], "rule_match")

    def test_prerequisite_fails_short_circuits(self):
        res = evaluate_flag("v2_api", self.flags, attributes={"tenant": "acme"})
        self.assertFalse(res["hit"])
        self.assertEqual(res["reason"], "prerequisite_failed")
        # v2's rules must never have been evaluated
        self.assertEqual([n["flag"] for n in res["chain"]], ["logged_in", "v2_api"])
        self.assertEqual(res["chain"][1]["reason"], "prerequisite_failed")

    def test_negated_expect(self):
        flags = {
            "kill": flag(default=False),
            "feature": flag(
                default="on",
                prerequisites=[{"flag": "kill", "expect": False}],
            ),
        }
        res = evaluate_flag("feature", flags, attributes={})
        self.assertEqual(res["value"], "on")

    def test_deep_chain_is_leaf_first(self):
        flags = {
            "a": flag(default=True),
            "b": flag(default=True, prerequisites=[{"flag": "a"}]),
            "c": flag(default="yes", prerequisites=[{"flag": "b"}]),
        }
        res = evaluate_flag("c", flags, attributes={})
        self.assertEqual([n["flag"] for n in res["chain"]], ["a", "b", "c"])
        self.assertEqual(res["value"], "yes")

    def test_transitive_failure_chain_has_no_duplicate_flags(self):
        flags = {
            "a": flag(default=False),
            "b": flag(default=True, prerequisites=[{"flag": "a"}]),
            "c": flag(default="yes", prerequisites=[{"flag": "b"}]),
        }
        res = evaluate_flag("c", flags, attributes={})
        names = [n["flag"] for n in res["chain"]]
        self.assertEqual(names, ["a", "b", "c"])
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(res["reason"], "prerequisite_failed")
        self.assertEqual(res["chain"][0]["prerequisite_of"], "b")
        self.assertEqual(res["chain"][1]["reason"], "prerequisite_failed")
        self.assertEqual(res["chain"][2]["reason"], "prerequisite_failed")

    def test_diamond_dependency_evaluates_shared_flag_once(self):
        flags = {
            "a": flag(rules=[rule([{"attr": "tier", "op": "eq", "value": "gold"}], id="gold")]),
            "b": flag(default=True, prerequisites=[{"flag": "a"}]),
            "c": flag(default="on", prerequisites=[{"flag": "a"}, {"flag": "b"}]),
        }
        res = evaluate_flag("c", flags, attributes={"tier": "gold"})
        names = [n["flag"] for n in res["chain"]]
        self.assertEqual(names, ["a", "b", "c"])
        self.assertEqual(res["value"], "on")


class RolloutTests(unittest.TestCase):
    def test_stable_bucket_for_same_flag_and_user(self):
        b1 = hash_bucket("new_checkout", "user-42")
        b2 = hash_bucket("new_checkout", "user-42")
        self.assertEqual(b1, b2)
        self.assertIsInstance(b1, int)
        self.assertGreaterEqual(b1, 0)
        self.assertLess(b1, 100)

    def test_different_flags_distribute_independently(self):
        # Extremely unlikely that all flags collide for the same user.
        user = "user-42"
        buckets = {hash_bucket(f"flag-{i}", user) for i in range(20)}
        self.assertGreater(len(buckets), 1)

    def test_zero_percentage_never_in(self):
        flags = {"f": flag(rollout={"percentage": 0, "key": "user_id", "value": True})}
        for uid in range(50):
            res = evaluate_flag("f", flags, user_id=str(uid))
            self.assertFalse(res["hit"])
            self.assertEqual(res["reason"], "rollout_out")

    def test_hundred_percentage_always_in(self):
        flags = {"f": flag(rollout={"percentage": 100, "key": "user_id", "value": "x"})}
        for uid in range(50):
            res = evaluate_flag("f", flags, user_id=str(uid))
            self.assertEqual(res["reason"], "rollout_in")
            self.assertEqual(res["value"], "x")

    def test_inclusion_is_monotonic_as_percentage_grows(self):
        flags_cfg = lambda p: {"f": flag(rollout={"percentage": p, "key": "user_id", "value": True})}
        for uid in range(300):
            bucket = hash_bucket("f", str(uid))
            in_at = lambda p: evaluate_flag("f", flags_cfg(p), user_id=str(uid))["hit"]
            if bucket < 10:
                self.assertTrue(in_at(10))
            if not in_at(30):
                self.assertFalse(in_at(10))
            self.assertTrue(in_at(100))

    def test_stability_repeated_evaluation(self):
        flags = {"f": flag(rollout={"percentage": 50, "key": "user_id"})}
        first = evaluate_flag("f", flags, user_id="alice")
        for _ in range(20):
            again = evaluate_flag("f", flags, user_id="alice", attributes={"plan": "pro"})
            self.assertEqual(again["hit"], first["hit"])
            self.assertEqual(again["rollout"]["bucket"], first["rollout"]["bucket"])

    def test_no_user_id_falls_out_of_rollout(self):
        flags = {"f": flag(rollout={"percentage": 100, "key": "user_id", "value": True})}
        res = evaluate_flag("f", flags, attributes={})
        self.assertFalse(res["hit"])
        self.assertEqual(res["reason"], "rollout_out")

    def test_numeric_user_id_is_supported(self):
        flags = {"f": flag(rollout={"percentage": 100, "key": "user_id", "value": True})}
        res = evaluate_flag("f", flags, user_id=12345)
        self.assertTrue(res["hit"])
        self.assertEqual(res["rollout"]["bucket"], hash_bucket("f", "12345"))


class ValidationTests(unittest.TestCase):
    def _expect(self, config, code):
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(config)
        codes = [e["code"] for e in ctx.exception.errors]
        self.assertIn(code, codes, msg=f"expected {code} in {codes}")

    def test_cycle_of_two_is_rejected(self):
        config = {
            "flags": {
                "a": {"prerequisites": [{"flag": "b"}], "rules": [], "default": False},
                "b": {"prerequisites": [{"flag": "a"}], "rules": [], "default": False},
            }
        }
        self._expect(config, "circular_dependency")

    def test_self_cycle_is_rejected(self):
        config = {"flags": {"a": {"prerequisites": [{"flag": "a"}]}}}
        self._expect(config, "self_dependency")

    def test_three_node_cycle_is_rejected(self):
        config = {
            "flags": {
                "a": {"prerequisites": [{"flag": "b"}]},
                "b": {"prerequisites": [{"flag": "c"}]},
                "c": {"prerequisites": [{"flag": "a"}]},
            }
        }
        self._expect(config, "circular_dependency")

    def test_dag_is_accepted(self):
        config = {
            "flags": {
                "a": {},
                "b": {"prerequisites": [{"flag": "a"}]},
                "c": {"prerequisites": [{"flag": "a"}, {"flag": "b"}]},
            }
        }
        validate_config(config)  # should not raise

    def test_percentage_bounds(self):
        for bad in (-1, 101):
            self._expect(
                {"flags": {"f": {"rollout": {"percentage": bad}}}},
                "invalid_percentage",
            )

    def test_percentage_must_be_integer(self):
        self._expect(
            {"flags": {"f": {"rollout": {"percentage": "50"}}}},
            "invalid_percentage",
        )
        # bool must not be accepted as int
        self._expect(
            {"flags": {"f": {"rollout": {"percentage": True}}}},
            "invalid_percentage",
        )

    def test_boundary_percentages_accepted(self):
        for good in (0, 50, 100):
            validate_config({"flags": {"f": {"rollout": {"percentage": good}}}})

    def test_unknown_operator_rejected(self):
        self._expect(
            {"flags": {"f": {"rules": [{"conditions": [{"attr": "x", "op": "regex"}]}]}}},
            "unknown_operator",
        )

    def test_missing_prerequisite_rejected(self):
        self._expect(
            {"flags": {"f": {"prerequisites": [{"flag": "ghost"}]}}},
            "missing_prerequisite",
        )

    def test_duplicate_rule_id_rejected(self):
        config = {
            "flags": {
                "f": {
                    "rules": [
                        {"id": "r1", "conditions": []},
                        {"id": "r1", "conditions": [{"attr": "a", "op": "eq", "value": 1}]},
                    ]
                }
            }
        }
        self._expect(config, "duplicate_rule_id")

    def test_unsupported_rollout_key_rejected(self):
        self._expect(
            {"flags": {"f": {"rollout": {"percentage": 10, "key": "email"}}}},
            "invalid_rollout_key",
        )

    def test_empty_config_is_valid(self):
        validate_config({"flags": {}})


class VersionComparisonTests(unittest.TestCase):
    def test_diff_added_removed_modified_sorted(self):
        old = {
            "keep": flag(default=1),
            "mod": flag(default="a"),
            "gone_b": flag(),
            "gone_a": flag(),
        }
        new = {
            "keep": flag(default=1),
            "mod": flag(default="b"),
            "new_b": flag(),
            "new_a": flag(),
        }
        added, removed, modified = diff_flags(old, new)
        self.assertEqual(added, ["new_a", "new_b"])
        self.assertEqual(removed, ["gone_a", "gone_b"])
        self.assertEqual(modified, ["mod"])

    def test_key_order_is_not_a_modification(self):
        a = {"f": {"enabled": True, "default": False, "rules": []}}
        b = {"f": {"default": False, "rules": [], "enabled": True}}
        self.assertEqual(diff_flags(a, b), ([], [], []))

    def test_chain_impact_reports_shortest_paths(self):
        flags = {
            "a": flag(),
            "b": flag(prerequisites=[{"flag": "a"}]),
            "c": flag(prerequisites=[{"flag": "b"}]),
        }
        adj = build_reverse_graph(flags)
        impacts = impact_analysis(["a"], adj)
        self.assertEqual([i["flag"] for i in impacts], ["a"])
        impacted = impacts[0]["impacted"]
        self.assertEqual([i["flag"] for i in impacted], ["b", "c"])
        self.assertEqual(impacted[0]["path"], ["a", "b"])
        self.assertEqual(impacted[1]["path"], ["a", "b", "c"])

    def test_diamond_prefers_direct_shortest_path(self):
        flags = {
            "a": flag(),
            "b": flag(prerequisites=[{"flag": "a"}]),
            "c": flag(prerequisites=[{"flag": "a"}, {"flag": "b"}]),
        }
        adj = build_reverse_graph(flags)
        impacts = impact_analysis(["a"], adj)
        paths = {i["flag"]: i["path"] for i in impacts[0]["impacted"]}
        self.assertEqual(paths["b"], ["a", "b"])
        self.assertEqual(paths["c"], ["a", "c"])

    def test_equal_length_paths_pick_lexicographically_smallest(self):
        flags = {
            "x": flag(),
            "q": flag(prerequisites=[{"flag": "x"}]),
            "p": flag(prerequisites=[{"flag": "x"}]),
            "z": flag(prerequisites=[{"flag": "p"}, {"flag": "q"}]),
        }
        adj = build_reverse_graph(flags)
        impacts = impact_analysis(["x"], adj)
        paths = {i["flag"]: i["path"] for i in impacts[0]["impacted"]}
        self.assertEqual(paths["z"], ["x", "p", "z"])

    def test_impact_uses_union_of_edges_from_both_versions(self):
        old = {"a": flag(), "b": flag(prerequisites=[{"flag": "a"}])}
        new = {"a": flag(default="changed"), "b": flag()}  # edge removed in new
        result = compare_versions(old, new)
        self.assertEqual(result["modified"], ["a", "b"])
        impacts = {i["flag"]: i for i in result["impacts"]}
        # The edge b->a only exists in the old version, yet b is still
        # reported as impacted by the change in a.
        self.assertEqual([x["flag"] for x in impacts["a"]["impacted"]], ["b"])

    def test_union_cycle_tolerated_and_source_not_self_impacted(self):
        old = {"a": flag(prerequisites=[{"flag": "b"}]), "b": flag()}
        new = {"a": flag(), "b": flag(prerequisites=[{"flag": "a"}])}
        result = compare_versions(old, new)
        self.assertEqual(result["modified"], ["a", "b"])
        impacts = {i["flag"]: i for i in result["impacts"]}
        self.assertEqual([x["flag"] for x in impacts["a"]["impacted"]], ["b"])
        self.assertEqual([x["flag"] for x in impacts["b"]["impacted"]], ["a"])

    def test_duplicate_impacts_reported_once(self):
        flags = {
            "a": flag(),
            "b": flag(prerequisites=[{"flag": "a"}]),
            "c": flag(prerequisites=[{"flag": "a"}, {"flag": "b"}]),
            "d": flag(prerequisites=[{"flag": "c"}]),
        }
        adj = build_reverse_graph(flags)
        impacts = impact_analysis(["a"], adj)
        names = [i["flag"] for i in impacts[0]["impacted"]]
        self.assertEqual(names, ["b", "c", "d"])
        self.assertEqual(len(names), len(set(names)))

    def test_changed_flag_without_dependents_has_empty_impacted(self):
        result = compare_versions({}, {"solo": flag()})
        self.assertEqual(result["added"], ["solo"])
        self.assertEqual(result["impacts"], [{"flag": "solo", "impacted": []}])

    def test_impacts_sorted_by_changed_flag_name(self):
        result = compare_versions({}, {"zeta": flag(), "alpha": flag()})
        self.assertEqual([i["flag"] for i in result["impacts"]], ["alpha", "zeta"])


class ImmutableVersionStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLAG_DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        self.conn = db_mod.connect()
        db_mod.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("FLAG_DB_PATH", None)

    def test_versions_are_immutable_and_history_is_preserved(self):
        v1 = {"flags": {"f": {"enabled": True, "default": "v1", "rules": []}}}
        v2 = {"flags": {"f": {"enabled": True, "default": "v2", "rules": []}}}

        ts1 = db_mod.insert_version(self.conn, "v1", v1)
        self.assertIsNotNone(ts1)
        self.assertIsNone(db_mod.insert_version(self.conn, "v1", v2))  # duplicate rejected

        loaded_v1 = db_mod.get_version(self.conn, "v1")
        self.assertEqual(loaded_v1["config"]["flags"]["f"]["default"], "v1")

        db_mod.insert_version(self.conn, "v2", v2)
        self.assertEqual(db_mod.get_version(self.conn, "v2")["config"]["flags"]["f"]["default"], "v2")

        # Publishing v2 must not change evaluations pinned to v1.
        still_v1 = db_mod.get_version(self.conn, "v1")["config"]
        res = evaluate_flag("f", still_v1["flags"], attributes={})
        self.assertEqual(res["value"], "v1")

        versions = db_mod.list_versions(self.conn)
        self.assertEqual([v["version"] for v in versions], ["v1", "v2"])
        self.assertEqual(versions[0]["flag_count"], 1)


class EndToEndPinnedVersionTests(unittest.TestCase):
    def test_pinned_version_result_is_stable(self):
        flags_v1 = {
            "checkout": flag(
                rollout={"percentage": 50, "key": "user_id"},
                prerequisites=[{"flag": "signed_in"}],
            ),
            "signed_in": flag(rules=[rule([{"attr": "user_id", "op": "ne", "value": None}])]),
        }
        for uid in range(100):
            r1 = evaluate_flag("checkout", flags_v1, user_id=str(uid))
            r2 = evaluate_flag("checkout", flags_v1, user_id=str(uid), attributes={"x": "y"})
            self.assertEqual(r1["hit"], r2["hit"])
            self.assertEqual([n["flag"] for n in r1["chain"]], ["signed_in", "checkout"])


class EnvironmentPointerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLAG_DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        self.conn = db_mod.connect()
        db_mod.init_db(self.conn)
        self.config = {"flags": {"f": {"enabled": True, "default": "v1", "rules": []}}}
        db_mod.insert_version(self.conn, "v1", self.config)
        db_mod.insert_version(
            self.conn, "v2", {"flags": {"f": {"enabled": True, "default": "v2", "rules": []}}}
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("FLAG_DB_PATH", None)

    def _upsert(self, name, version, expected_revision):
        env, created = db_mod.upsert_environment(self.conn, name, version, expected_revision)
        self.assertEqual(created, expected_revision == 0)
        return env

    def test_create_requires_revision_zero_and_starts_at_one(self):
        env = self._upsert("prod", "v1", 0)
        self.assertEqual(env["name"], "prod")
        self.assertEqual(env["version"], "v1")
        self.assertEqual(env["revision"], 1)
        self.assertEqual(env["created_at"], env["updated_at"])

        loaded = db_mod.get_environment(self.conn, "prod")
        self.assertEqual(loaded["revision"], 1)
        self.assertEqual(loaded["version"], "v1")

    def test_create_then_update_increments_revision(self):
        self._upsert("prod", "v1", 0)
        env = self._upsert("prod", "v2", 1)
        self.assertEqual(env["version"], "v2")
        self.assertEqual(env["revision"], 2)
        self.assertEqual(db_mod.get_environment(self.conn, "prod")["revision"], 2)

    def test_create_with_nonzero_revision_on_missing_env_is_stale(self):
        with self.assertRaises(db_mod.StaleRevision) as ctx:
            db_mod.upsert_environment(self.conn, "prod", "v1", 1)
        self.assertIsNone(ctx.exception.current)
        self.assertIsNone(db_mod.get_environment(self.conn, "prod"))

    def test_create_on_existing_env_with_revision_zero_is_stale_and_untouched(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        with self.assertRaises(db_mod.StaleRevision):
            db_mod.upsert_environment(self.conn, "prod", "v2", 0)
        # Pointer must not have moved.
        self.assertEqual(db_mod.get_environment(self.conn, "prod")["version"], "v1")
        self.assertEqual(db_mod.get_environment(self.conn, "prod")["revision"], 1)

    def test_stale_update_does_not_move_pointer(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        db_mod.upsert_environment(self.conn, "prod", "v2", 1)  # revision now 2
        with self.assertRaises(db_mod.StaleRevision) as ctx:
            db_mod.upsert_environment(self.conn, "prod", "v1", 1)
        self.assertEqual(ctx.exception.current, 2)
        env = db_mod.get_environment(self.conn, "prod")
        self.assertEqual(env["version"], "v2")
        self.assertEqual(env["revision"], 2)

    def test_missing_version_on_create_returns_not_found_without_row(self):
        with self.assertRaises(db_mod.VersionNotFound):
            db_mod.upsert_environment(self.conn, "prod", "ghost", 0)
        self.assertIsNone(db_mod.get_environment(self.conn, "prod"))

    def test_missing_version_on_update_returns_not_found_and_keeps_revision(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        with self.assertRaises(db_mod.VersionNotFound):
            db_mod.upsert_environment(self.conn, "prod", "ghost", 1)
        env = db_mod.get_environment(self.conn, "prod")
        self.assertEqual(env["version"], "v1")
        self.assertEqual(env["revision"], 1)

    def test_stale_revision_takes_precedence_over_missing_version(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        # Both stale revision and unknown version: revision is checked first.
        with self.assertRaises(db_mod.StaleRevision):
            db_mod.upsert_environment(self.conn, "prod", "ghost", 99)
        self.assertEqual(db_mod.get_environment(self.conn, "prod")["version"], "v1")

    def test_get_missing_environment_returns_none(self):
        self.assertIsNone(db_mod.get_environment(self.conn, "nope"))
        self.assertIsNone(db_mod.get_environment_snapshot(self.conn, "nope"))

    def test_snapshot_returns_pointer_and_pinned_config(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        snap = db_mod.get_environment_snapshot(self.conn, "prod")
        self.assertEqual(snap["version"], "v1")
        self.assertEqual(snap["revision"], 1)
        self.assertEqual(snap["config"]["flags"]["f"]["default"], "v1")

        # Moving the pointer changes the snapshot but never the old version.
        db_mod.upsert_environment(self.conn, "prod", "v2", 1)
        snap2 = db_mod.get_environment_snapshot(self.conn, "prod")
        self.assertEqual(snap2["version"], "v2")
        self.assertEqual(snap2["revision"], 2)
        self.assertEqual(snap2["config"]["flags"]["f"]["default"], "v2")
        # Snapshot captured earlier still describes v1.
        self.assertEqual(snap["config"]["flags"]["f"]["default"], "v1")

    def test_concurrent_updates_exactly_one_wins_per_revision(self):
        """N racing writers all observe revision 1; exactly one must win."""
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        n = 8
        barrier = threading.Barrier(n)
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()

        def race() -> None:
            conn = db_mod.connect()
            try:
                barrier.wait()
                try:
                    db_mod.upsert_environment(conn, "prod", "v2", 1)
                    with outcomes_lock:
                        outcomes.append("ok")
                except db_mod.StaleRevision:
                    with outcomes_lock:
                        outcomes.append("stale")
            finally:
                conn.close()

        threads = [threading.Thread(target=race) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(outcomes), ["ok"] + ["stale"] * (n - 1))
        env = db_mod.get_environment(self.conn, "prod")
        self.assertEqual(env["version"], "v2")
        self.assertEqual(env["revision"], 2)

    def test_concurrent_chain_no_lost_updates(self):
        """Re-read-and-retry writers must produce exactly one increment each."""
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)

        versions = ["v1", "v2"]
        target_revisions = [2, 3]  # worker 0 ends at rev 2, worker 1 at rev 3

        def worker(worker_id: int) -> None:
            conn = db_mod.connect()
            try:
                while True:
                    current = db_mod.get_environment(conn, "prod")
                    try:
                        db_mod.upsert_environment(
                            conn,
                            "prod",
                            versions[worker_id],
                            current["revision"],
                        )
                        return
                    except db_mod.StaleRevision:
                        continue
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        env = db_mod.get_environment(self.conn, "prod")
        self.assertEqual(env["revision"], 3)
        self.assertIn(env["version"], versions)


if __name__ == "__main__":
    unittest.main()
