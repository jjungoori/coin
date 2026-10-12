import copy
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from coin.ecosystem import EvolutionSystem, load_config
from coin.feeds import DemoFeed
from coin.generative import CodexCLI, policy_decision, validate_plan, validate_policy
from coin.strategies import default_genomes, decide


CONFIG = Path(__file__).resolve().parents[1] / "agents.toml"


def action(kind="create_environment", **changes):
    return {"kind": kind, "reason": "Test a different hypothesis", "environment": "ai-lab",
            "agent": None, "market": "KRW-BTC", "budget": "200000",
            "genome": default_genomes()[0], "policy": None, **changes}


def plan(*actions):
    return {"assessment": "Regime needs an isolated experiment", "actions": list(actions)}


def policy(field="rsi14", threshold=0):
    return {"buy": [{"left": field, "op": "gte", "right_field": None, "right_value": threshold}], "sell": []}


class GenerativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.planner = Mock()
        self.planner.plan.return_value = plan()
        self.worker = Mock()
        self.worker.review.side_effect = lambda snapshot: {"decisions": [
            {"agent": c["agent"], **c["baseline"]} for c in snapshot["candidates"]]}
        self.system = EvolutionSystem(Path(self.temp.name) / "agents.sqlite3", CONFIG,
                                      ai_backend="codex", planner=self.planner, worker=self.worker)
        self.system.initialize()
        self.feed = DemoFeed(self.system.config)

    def tearDown(self):
        self.system.close()
        self.temp.cleanup()

    def test_ai_creates_isolated_environment_and_spawns_selected_policies(self):
        self.planner.plan.return_value = plan(action(policy=policy()), action("spawn_agent", budget=None))
        frame = self.feed.next_frame()
        self.system.tick(frame)
        status = self.system.status()
        self.assertEqual(status["experiment_reserve"], "800000")
        lab = next(env for env in status["environments"] if env["id"] == "ai-lab")
        self.assertEqual(lab["treasury"], "0")
        agents = [a for a in status["agents"] if a["environment"] == "ai-lab"]
        self.assertEqual(len(agents), 2)
        self.assertEqual(agents[0]["policy"], policy())
        self.assertAlmostEqual(float(status["capital_accounting"]["identity_error"]), 0)
        self.system.tick(frame)
        self.planner.plan.assert_called_once()
        # Neither the rule supervisor's spawn cadence nor environment cadence is used.
        for _ in range(3):
            self.system.tick(self.feed.next_frame())
        self.assertEqual(len(self.system.status()["environments"]), 2)

    def test_strategy_rewrite_is_persistent_and_changes_trading_behavior(self):
        agent = next(a for a in self.system.status()["agents"] if a["genome"]["kind"] == "reversion")
        self.planner.plan.return_value = plan(action("revise_strategy", agent=agent["id"],
                                                    genome=agent["genome"], policy=policy(), budget=None))
        self.system.tick(self.feed.next_frame())
        self.system.tick(self.feed.next_frame())
        decision = self.system.db.execute("SELECT * FROM decisions WHERE tick=2 AND agent=?", (agent["id"],)).fetchone()
        self.assertEqual(decision["action"], "buy")
        self.assertEqual(decision["outcome"], "filled")
        self.assertEqual(decision["reason"], "AI-authored policy conditions")
        self.system.close()
        self.system = EvolutionSystem(Path(self.temp.name) / "agents.sqlite3", CONFIG,
                                      ai_backend="codex", planner=self.planner, worker=self.worker)
        self.system.initialize()
        saved = next(a for a in self.system.status()["agents"] if a["id"] == agent["id"])
        self.assertEqual(saved["policy"], policy())

    def test_invalid_actions_rollback_without_minting_or_moving_capital(self):
        self.planner.plan.return_value = plan(action(budget="900000"), action(environment="main"),
                                              action(environment="valid-lab"))
        self.system.tick(self.feed.next_frame())
        status = self.system.status()
        self.assertEqual(status["experiment_reserve"], "800000")
        self.assertEqual([e["id"] for e in status["environments"]], ["main", "valid-lab"])
        rejected = [e for e in self.system.events(100) if e["kind"] == "supervisor_ai_rejected"]
        self.assertEqual(len(rejected), 2)
        self.assertAlmostEqual(float(status["capital_accounting"]["identity_error"]), 0)

    def test_ai_failure_does_not_trigger_rule_actions(self):
        self.planner.plan.side_effect = subprocess.TimeoutExpired("codex", 1)
        self.system.tick(self.feed.next_frame())
        self.assertEqual(self.system.status()["tick"], 1)
        events = self.system.events(100)
        self.assertTrue(any(e["kind"] == "supervisor_ai_error" for e in events))
        self.assertFalse(any(e["kind"] == "supervisor_observation" for e in events))

    def test_inference_runs_outside_transaction_and_old_state_plan_is_rejected(self):
        def infer(snapshot):
            self.assertFalse(self.system.db.in_transaction)
            self.assertIn("market_context", snapshot)
            return plan(action())
        self.planner.plan.side_effect = infer
        self.system.tick(self.feed.next_frame())
        count = len(self.system.status()["environments"])
        self.system._apply_plan({"base_tick": 0, "plan": plan(action(environment="stale"))}, 3)
        self.assertEqual(len(self.system.status()["environments"]), count)

    def test_policy_cannot_bypass_quality_exit_or_sentiment_guards(self):
        frame = self.feed.next_frame()
        context = frame["contexts"]["KRW-BTC"]
        wallet = {"cash": "100000", "positions": []}
        genome = default_genomes()[0]
        self.assertEqual(policy_decision(policy(), genome, context, wallet,
                         {"action": "sell", "reason": "Stop loss reached"})["action"], "sell")
        context["chart"]["quality"]["stale"] = True
        baseline = decide(genome, context, wallet)
        self.assertEqual(policy_decision(policy(), genome, context, wallet, baseline)["action"], "hold")
        context["chart"]["quality"]["stale"] = False
        genome["sentiment_min"] = 50
        baseline = {"action": "hold", "reason": "No entry signal"}
        self.assertEqual(policy_decision(policy(), genome, context, wallet, baseline)["action"], "hold")
        self.assertEqual(policy_decision(policy("price", 1e20), genome, context, wallet, baseline)["action"], "hold")

    def test_malformed_output_and_nonfinite_rules_are_rejected(self):
        bad = policy()
        bad["buy"][0]["right_value"] = float("nan")
        with self.assertRaises(ValueError):
            validate_policy(bad)
        with self.assertRaises(ValueError):
            validate_plan(plan(action(genome={"kind": "trend", "allocation": 2})))
        with self.assertRaises(ValueError):
            validate_plan({"assessment": "test", "actions": [{"kind": "shell"}]})
        self.planner.plan.return_value = {"assessment": "test", "actions": [{"kind": "shell"}]}
        self.system.tick(self.feed.next_frame())
        self.assertEqual(len(self.system.status()["environments"]), 1)

    def test_legacy_config_store_can_resume_without_reset(self):
        saved = copy.deepcopy(self.system.config)
        del saved["ai"]
        self.system._set_meta("config", json.dumps(saved))
        self.system.initialize()
        self.system.tick(self.feed.next_frame())
        self.assertEqual(self.system.status()["tick"], 1)

    def test_cli_transport_inherits_login_and_uses_schema_stdin(self):
        settings = load_config(CONFIG)["ai"]
        def run(command, **kwargs):
            self.assertIn("--output-schema", command)
            self.assertIn("--ignore-user-config", command)
            self.assertEqual(command[-1], "-")
            self.assertIn("Snapshot:", kwargs["input"])
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertNotIn("CODEX_API_KEY", kwargs["env"])
            self.assertEqual(kwargs["env"]["CODEX_HOME"], "existing-login-home")
            Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(plan()), encoding="utf-8")
            return Mock(returncode=0)
        with patch.dict(os.environ, {"CODEX_HOME": "existing-login-home", "CODEX_API_KEY": "unused"}), \
                patch("coin.generative.shutil.which", return_value="codex"), \
                patch("coin.generative.subprocess.run", side_effect=run):
            self.assertEqual(CodexCLI(settings).plan({"tick": 0}), plan())


if __name__ == "__main__":
    unittest.main()
