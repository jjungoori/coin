import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from coin.ecosystem import EvolutionSystem
from coin.feeds import DemoFeed
from coin.generative import CodexCLI, combine_review, validate_reviews
from coin.strategies import default_genomes

CONFIG = Path(__file__).resolve().parents[1] / "agents.toml"


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.planner = Mock()
        self.planner.plan.return_value = {"assessment": "Observe", "actions": []}
        self.worker = Mock()
        self.worker.review.side_effect = lambda snapshot: {"decisions": [
            {"agent": c["agent"], "action": "buy", "reason": "Test hypothesis", "target_fraction": 0.8}
            for c in snapshot["candidates"]]}
        self.system = EvolutionSystem(Path(self.temp.name) / "agents.sqlite3", CONFIG,
                                      ai_backend="codex", planner=self.planner, worker=self.worker)
        self.system.initialize()
        self.feed = DemoFeed(self.system.config)

    def tearDown(self):
        self.system.close()
        self.temp.cleanup()

    def test_only_hybrid_wallets_receive_review_and_allocation_is_capped(self):
        hybrid = next(a for a in self.system.status()["agents"] if a["decision_mode"] == "hybrid")
        self.system.tick(self.feed.next_frame())
        supervisor_snapshot = self.planner.plan.call_args.args[0]
        self.assertNotIn("wallet", supervisor_snapshot["supervisor"])
        self.assertTrue(all(a["role"] == "trader" for a in supervisor_snapshot["agents"]))
        candidates = self.worker.review.call_args.args[0]["candidates"]
        self.assertEqual([c["agent"] for c in candidates], [hybrid["id"]])
        self.assertNotIn("candles", candidates[0]["market_context"]["chart"])
        row = self.system.db.execute("SELECT * FROM decisions WHERE agent=? AND tick=1", (hybrid["id"],)).fetchone()
        self.assertEqual(row["action"], "buy")
        self.assertTrue(row["reason"].startswith("Hybrid AI:"))
        wallet = self.system.broker.wallet(hybrid["id"])
        self.assertGreaterEqual(float(wallet["cash"]), 75000)
        self.assertEqual(self.system.status()["ai_usage"]["workers"]["calls"], 1)

    def test_cooldown_and_duplicate_frames_do_not_repeat_calls(self):
        frame = self.feed.next_frame()
        self.system.tick(frame)
        self.system.tick(frame)
        for _ in range(5):
            self.system.tick(self.feed.next_frame())
        self.worker.review.assert_called_once()

    def test_mode_change_persists_and_supervisor_cannot_be_worker(self):
        status = self.system.status()
        trader = next(a for a in status["agents"] if a["role"] == "trader" and a["decision_mode"] == "rules")
        self.system.set_decision_mode(trader["id"], "hybrid")
        self.assertEqual(self.system.db.execute("SELECT decision_mode FROM agents WHERE id=?", (trader["id"],)).fetchone()[0], "hybrid")
        boss = status["supervisor"]
        with self.assertRaises(ValueError):
            self.system.set_decision_mode(boss["id"], "hybrid")

    def test_budget_reserves_unknown_usage_and_blocks_next_call(self):
        settings = {"max_daily_calls": 10, "daily_token_budget": 150, "reserve_tokens_per_call": 100}
        provider = Mock()
        first = self.system.ai_budget.invoke("test", settings, "first", 0, provider, lambda: {"ok": True})
        self.assertIn("plan", first)
        second = self.system.ai_budget.invoke("test", settings, "second", 1, provider, lambda: self.fail("Budget should block inference"))
        self.assertEqual(second["skipped"], "daily_call_or_token_budget")
        status = self.system.ai_budget.status("test", settings)
        self.assertEqual((status["calls"], status["accounted_tokens"], status["unknown_usage_calls"]), (1, 100, 1))

    def test_reported_usage_and_failed_calls_survive_restart(self):
        settings = {"max_daily_calls": 2, "daily_token_budget": 1000, "reserve_tokens_per_call": 100}
        provider = Mock()
        def measured():
            provider.last_usage = {"input_tokens": 30, "output_tokens": 20, "cached_input_tokens": 10}
            return {"ok": True}
        self.system.ai_budget.invoke("test", settings, "one", 0, provider, measured)
        def failed():
            raise RuntimeError("offline")
        self.system.ai_budget.invoke("test", settings, "two", 0, provider, failed)
        status = self.system.ai_budget.status("test", settings)
        self.assertEqual((status["calls"], status["accounted_tokens"]), (2, 150))
        again = self.system.ai_budget.invoke("test", settings, "two", 0, provider, lambda: self.fail("No retries for same frame"))
        self.assertIn("error", again)
        self.system.close()
        self.system = EvolutionSystem(Path(self.temp.name) / "agents.sqlite3", CONFIG,
                                      ai_backend="codex", planner=self.planner, worker=self.worker)
        self.assertEqual(self.system.ai_budget.status("test", settings)["accounted_tokens"], 150)
        blocked = self.system.ai_budget.invoke("test", settings, "three", 0, provider, lambda: self.fail("Call cap"))
        self.assertIn("skipped", blocked)
        with patch.object(self.system.ai_budget, "day", return_value="2099-01-01"):
            self.assertEqual(self.system.ai_budget.status("test", settings)["calls"], 0)

    def test_failed_worker_falls_back_to_rules(self):
        self.worker.review.side_effect = RuntimeError("offline")
        self.system.tick(self.feed.next_frame())
        self.assertTrue(any(e["kind"] == "worker_ai_error" for e in self.system.events(100)))
        hybrid = next(a for a in self.system.status()["agents"] if a["decision_mode"] == "hybrid")
        reason = self.system.db.execute("SELECT reason FROM decisions WHERE agent=?", (hybrid["id"],)).fetchone()[0]
        self.assertFalse(reason.startswith("Hybrid AI:"))

    def test_invalid_worker_output_and_guard_bypasses_are_rejected(self):
        candidate = [{"agent": "one"}]
        with self.assertRaises(ValueError):
            validate_reviews({"decisions": []}, candidate)
        with self.assertRaises(ValueError):
            validate_reviews({"decisions": [{"agent": "other", "action": "buy", "reason": "x", "target_fraction": 0.2}]}, candidate)
        genome = default_genomes()[0]
        review = {"action": "buy", "reason": "Enter", "target_fraction": 0.8}
        context = {"market": "KRW-BTC"}
        wallet = {"positions": [{"market": "KRW-BTC", "quantity": "1"}]}
        self.assertEqual(combine_review(review, {"action": "hold", "reason": "Holding"}, genome, context, wallet)["action"], "hold")
        baseline = {"action": "sell", "reason": "Stop loss reached", "target_fraction": 0}
        self.assertEqual(combine_review(review, baseline, genome, context, wallet), baseline)

    def test_cli_collects_usage_and_selects_lightweight_model(self):
        settings = {**self.system.config["workers"], "command": "codex"}
        snapshot = {"candidates": [{"agent": "one"}]}
        def run(command, **kwargs):
            self.assertEqual(command[command.index("--model") + 1], "gpt-6-luna")
            self.assertIn('model_reasoning_effort="low"', command)
            self.assertIn("--json", command)
            output = {"decisions": [{"agent": "one", "action": "hold", "reason": "Wait", "target_fraction": 0}]}
            Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(output), encoding="utf-8")
            return Mock(returncode=0, stdout=json.dumps({"type": "turn.completed", "usage": {"input_tokens": 80, "output_tokens": 20}}))
        provider = CodexCLI(settings)
        with patch("coin.generative.shutil.which", return_value="codex"), patch("coin.generative.subprocess.run", side_effect=run):
            provider.review(snapshot)
        self.assertEqual(provider.last_usage, {"input_tokens": 80, "output_tokens": 20})

    def test_input_limit_prevents_cli_execution(self):
        settings = {**self.system.config["workers"], "command": "codex", "max_input_chars": 10}
        with patch("coin.generative.subprocess.run") as transport:
            with self.assertRaises(ValueError):
                CodexCLI(settings).review({"candidates": [{"agent": "one"}]})
            transport.assert_not_called()

    def test_tick_rollback_reuses_worker_result_without_new_token_spend(self):
        frame = self.feed.next_frame()
        with patch.object(self.system, "_trade", side_effect=RuntimeError("abort tick")):
            with self.assertRaises(RuntimeError):
                self.system.tick(frame)
        self.assertEqual(self.system.status()["tick"], 0)
        self.system.tick(frame)
        self.worker.review.assert_called_once()
        self.planner.plan.assert_called_once()
        self.assertEqual(self.system.status()["ai_usage"]["workers"]["calls"], 1)
        self.assertTrue(any(e["kind"] == "worker_ai_review" for e in self.system.events(100)))

    def test_lowering_budget_preserves_wallets_and_blocks_future_inference(self):
        self.system.tick(self.feed.next_frame())
        before = self.system.status()["ai_usage"]["workers"]["calls"]
        self.system.config["workers"]["daily_token_budget"] = 1
        self.system.config["workers"]["every_ticks"] = 1
        self.system.initialize()
        self.system.tick(self.feed.next_frame())
        self.assertEqual(self.system.status()["ai_usage"]["workers"]["calls"], before)
        self.worker.review.assert_called_once()
        self.assertTrue(any(e["kind"] == "worker_ai_skipped" for e in self.system.events(100)))


if __name__ == "__main__":
    unittest.main()
