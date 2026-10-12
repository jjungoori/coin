"""Requirement-level checks of paper-agent lifecycles and capital accounting."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from coin.ecosystem import EvolutionSystem, load_config
from coin.feeds import DemoFeed


class EcosystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp.name) / "agents.toml"
        self.config_path.write_text('''
[system]
markets = ["KRW-BTC"]
[survival]
max_drawdown = 0.05
grace_ticks = 1000
inactivity_ticks = 1000
[evolution]
every_ticks = 2
min_parent_age = 2
[supervisor]
every_ticks = 1
spawn_every_ticks = 3
environment_every_ticks = 4
''', encoding="utf-8")
        self.path = str(Path(self.temp.name) / "agents.sqlite3")
        self.system = EvolutionSystem(self.path, str(self.config_path))
        self.system.initialize()
        self.feed = DemoFeed(self.system.config)

    def tearDown(self):
        self.system.close()
        self.temp.cleanup()

    def frame(self, price=10000, bid=None):
        frame = self.feed.next_frame()
        chart = frame["contexts"]["KRW-BTC"]["chart"]
        chart["latest_close"] = price
        chart["quality"].update(stale=False, gap_count=0, latest_close_age_seconds=0)
        chart["indicators"].update(sma20=9900, sma50=9800, rsi14=55,
                                   bollinger20={"lower": 9000, "middle": 9500, "upper": 11000},
                                   volume_ratio20=2)
        for row in chart["candles"]:
            row.update(open=9500, close=9500, high=9900, low=9000)
        chart["candles"][-1].update(open=price, close=price, high=price, low=price)
        frame["books"]["KRW-BTC"]["orderbook_units"] = [
            {"ask_price": price, "bid_price": bid if bid is not None else price * 0.999,
             "ask_size": 10000, "bid_size": 10000}]
        return frame

    def rows(self, table):
        return [dict(row) for row in self.system.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def capital(self, frame):
        status = self.system.status()
        total = Decimal(status["experiment_reserve"])
        total += sum((Decimal(env["treasury"]) for env in status["environments"]), Decimal(0))
        for agent in status["agents"]:
            total += Decimal(agent["wallet"]["cash"])
            for position in agent["wallet"]["positions"]:
                bid = max(Decimal(str(level["bid_price"])) for level in frame["books"][position["market"]]["orderbook_units"])
                total += Decimal(position["quantity"]) * bid
        return total

    def assert_conserved_tick(self, frame):
        before = self.capital(frame)
        before_ids = {row["id"] for row in self.rows("trades")}
        result = self.system.tick(frame)
        expected = before
        for trade in self.rows("trades"):
            if trade["id"] in before_ids:
                continue
            bid = max(Decimal(str(level["bid_price"])) for level in frame["books"][trade["market"]]["orderbook_units"])
            difference = Decimal(trade["quantity"]) * bid - Decimal(trade["notional"])
            expected += (difference if trade["side"] == "buy" else -difference) - Decimal(trade["fee"])
        self.assertLess(abs(self.capital(frame) - expected), Decimal("0.000000000001"))
        return result

    def test_independent_trading_wallets_and_walletless_supervisor(self):
        agents = self.system.status()["agents"]
        traders = [agent for agent in agents if agent["role"] == "trader"]
        self.assertEqual({agent["genome"]["kind"] for agent in traders}, {"trend", "breakout", "reversion"})
        self.assertEqual(len({agent["id"] for agent in agents}), 3)
        self.assertEqual(self.capital(self.frame()), Decimal("2000000"))
        self.assert_conserved_tick(self.frame())
        agents = self.system.status()["agents"]
        reversion = next(agent for agent in agents if agent["genome"]["kind"] == "reversion")
        self.assertEqual(reversion["wallet"]["cash"], "100000")
        self.assertEqual(reversion["trade_count"], 0)
        state = self.system.status()
        self.assertNotIn("wallet", state["supervisor"])
        self.assertTrue(all(agent["role"] == "trader" for agent in agents))
        self.assertEqual(self.system.db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0], 3)
        self.assertEqual(self.system.db.execute("SELECT COUNT(*) FROM agents WHERE role='supervisor'").fetchone()[0], 0)
        observation = next(event for event in self.system.events(100) if event["kind"] == "supervisor_observation")
        self.assertEqual({row["id"] for row in observation["detail"]["agents"]}, {agent["id"] for agent in traders})

    def test_supervisor_cannot_spawn_a_wallet(self):
        before = self.system.status()
        with self.assertRaisesRegex(ValueError, "Only trading agents"):
            self.system._spawn("main", before["agents"][0]["genome"], 0, role="supervisor")
        self.assertEqual(self.system.status(), before)

    def test_main_budget_only_needs_to_fund_trading_population(self):
        config = Path(self.temp.name) / "exact-budget.toml"
        config.write_text('[population]\nmain_budget = "300000"\n'
                          '[supervisor]\ncash = "200000"\n', encoding="utf-8")
        self.assertNotIn("cash", load_config(config)["supervisor"])
        system = EvolutionSystem(Path(self.temp.name) / "exact-budget.sqlite3", config)
        try:
            status = system.initialize()
            self.assertEqual(status["environments"][0]["treasury"], "0")
            self.assertEqual(len(status["agents"]), 3)
            self.assertEqual(Decimal(status["capital_accounting"]["identity_error"]), 0)
        finally:
            system.close()

    def test_legacy_supervisor_account_is_archived_once_without_losing_assets(self):
        # Reconstruct the old store with a funded and invested supervisor wallet.
        legacy = self.system._spawn("main", self.system.status()["agents"][0]["genome"],
                                    0, amount="200000")
        self.system.db.execute("UPDATE agents SET role='supervisor',age=1000,score=999 WHERE id=?", (legacy,))
        saved = copy.deepcopy(self.system.config)
        saved["supervisor"]["cash"] = "200000"
        self.system._set_meta("config", json.dumps(saved))
        frame = self.frame()
        self.system.broker.execute(legacy, "KRW-BTC", "buy", 10000,
                                   frame["books"]["KRW-BTC"], now_ms=frame["timestamp_ms"])
        wallet = self.system.broker.wallet(legacy)
        accounting = self.system.capital_accounting()
        treasury = Decimal(self.system.status()["environments"][0]["treasury"])
        self.system.close()
        self.system = EvolutionSystem(self.path, self.config_path)
        status = self.system.initialize()
        archived = status["archived_accounts"][0]
        self.assertEqual(archived["id"], legacy)
        self.assertEqual(archived["status"], "dead")
        self.assertEqual(archived["wallet"]["cash"], "0")
        self.assertEqual(archived["wallet"]["positions"], wallet["positions"])
        self.assertEqual(archived["trade_count"], 1)
        self.assertEqual(status["capital_accounting"], accounting)
        self.assertEqual(Decimal(status["environments"][0]["treasury"]), treasury + Decimal(wallet["cash"]))
        self.assertNotIn(legacy, [a["id"] for a in status["agents"]])
        self.assertNotIn(legacy, [a["id"] for a in self.system._parents("main")])
        with self.assertRaisesRegex(ValueError, "Dead agent wallet"):
            self.system.broker.execute(legacy, "KRW-BTC", "sell", 1,
                                       frame["books"]["KRW-BTC"], now_ms=frame["timestamp_ms"])
        self.system.close()
        self.system = EvolutionSystem(self.path, self.config_path)
        self.assertEqual(self.system.initialize(), status)
        self.assertEqual(len([e for e in self.system.events(100) if e["kind"] == "supervisor_wallet_archived"]), 1)
        self.system.tick(frame)
        self.assertEqual(self.system.db.execute("SELECT COUNT(*) FROM decisions WHERE agent=?", (legacy,)).fetchone()[0], 0)

    def test_death_liquidation_recycles_cash_and_freezes_lifecycle(self):
        self.assert_conserved_tick(self.frame())
        self.assert_conserved_tick(self.frame(price=7000))
        deaths = [agent for agent in self.system.status()["agents"] if agent["status"] == "dead"]
        self.assertGreaterEqual(len(deaths), 2)
        history = {agent["id"]: self.system.broker.history(agent["id"]) for agent in deaths}
        for agent in deaths:
            self.assertEqual(agent["death_reason"], "maximum_drawdown")
            self.assertGreater(Decimal(agent["terminal_equity"]), 0)
            self.assertEqual(Decimal(agent["wallet"]["cash"]), 0)
            self.assertTrue(all(Decimal(p["quantity"]) == 0 for p in agent["wallet"]["positions"]))
            self.assertEqual(history[agent["id"]][-1]["side"], "sell")
            frame = self.frame()
            with self.assertRaisesRegex(ValueError, "Dead agent"):
                self.system.broker.execute(agent["id"], agent["market"], "buy", 10000,
                                           frame["books"][agent["market"]], now_ms=frame["timestamp_ms"])
        self.assert_conserved_tick(self.frame())
        for agent in deaths:
            self.assertEqual(self.system.broker.history(agent["id"]), history[agent["id"]])
            self.assertEqual(self.system.db.execute("SELECT COUNT(*) FROM decisions WHERE agent=? AND tick>=2", (agent["id"],)).fetchone()[0], 0)

    def test_failed_dust_liquidation_preserves_frozen_assets(self):
        self.assert_conserved_tick(self.frame())
        self.assert_conserved_tick(self.frame(price=1000))
        deaths = [event for event in self.system.events(100) if event["kind"] == "agent_died"]
        self.assertTrue(deaths)
        for event in deaths:
            self.assertTrue(event["detail"]["frozen_positions"])
            wallet = self.system.broker.wallet(event["agent"])
            self.assertEqual(Decimal(wallet["cash"]), 0)
            self.assertGreater(Decimal(wallet["positions"][0]["quantity"]), 0)
            terminal = Decimal(event["detail"]["terminal_equity"])
            recycled = Decimal(event["detail"]["recycled_cash"])
            self.assertGreater(terminal, recycled)

    def test_evolution_lineage_mutation_and_delayed_child_activity(self):
        self.assert_conserved_tick(self.frame())
        self.assert_conserved_tick(self.frame())
        event = next(event for event in self.system.events(100) if event["kind"] == "evolution")
        child = next(agent for agent in self.system.status()["agents"] if agent["id"] == event["agent"])
        self.assertEqual(len(child["parents"]), 2)
        self.assertEqual(child["generation"], 1)
        self.assertEqual(child["age"], 0)
        self.assertEqual(child["trade_count"], 0)
        parents = [next(agent for agent in self.system.status()["agents"] if agent["id"] == parent) for parent in child["parents"]]
        # At least one numeric gene is novel, rather than simply copied.
        self.assertTrue(any(child["genome"][key] not in [parent["genome"][key] for parent in parents]
                            for key in child["genome"] if key != "kind"))
        self.assert_conserved_tick(self.frame())
        child = next(agent for agent in self.system.status()["agents"] if agent["id"] == child["id"])
        self.assertEqual(child["age"], 1)

    def test_supervisor_spawns_probe_and_isolated_environment_without_minting(self):
        for _ in range(4):
            self.assert_conserved_tick(self.frame())
        events = self.system.events(1000)
        probe = next(event for event in events if event["kind"] == "supervisor_spawn")
        agent = next(agent for agent in self.system.status()["agents"] if agent["id"] == probe["agent"])
        self.assertEqual(agent["parents"], [])
        self.assertEqual(agent["genome"]["allocation"], 0.1)
        env = next(event for event in events if event["kind"] == "supervisor_environment")
        state = self.system.status()
        self.assertEqual(len(state["environments"]), 2)
        self.assertEqual(Decimal(state["experiment_reserve"]), Decimal("600000"))
        isolated = [agent for agent in state["agents"] if agent["environment"] == env["environment"]]
        self.assertEqual(len(isolated), 3)
        self.assertTrue(all(agent["age"] == 0 and agent["trade_count"] == 0 for agent in isolated))
        before = {agent["id"]: copy.deepcopy(agent["wallet"]) for agent in state["agents"] if agent["environment"] == "main"}
        test_agent = isolated[0]
        frame = self.frame()
        self.system.broker.execute(test_agent["id"], "KRW-BTC", "buy", 10000,
                                   frame["books"]["KRW-BTC"], now_ms=frame["timestamp_ms"])
        for identifier, wallet in before.items():
            self.assertEqual(self.system.broker.wallet(identifier), wallet)

    def test_restart_duplicate_candle_does_not_reset_wallets_or_trade(self):
        frame = self.frame()
        self.assert_conserved_tick(frame)
        before = self.system.status()
        events_before = self.system.events(1000)
        self.system.close()
        self.system = EvolutionSystem(self.path, str(self.config_path))
        self.assertEqual(self.system.initialize(), before)
        self.assertEqual(self.system.tick(frame)["status"], "duplicate")
        self.assertEqual(self.system.status(), before)
        self.assertEqual(self.system.events(1000), events_before)
        # A fresh orderbook/context time for the same closed bars is still duplicate.
        retry = copy.deepcopy(frame)
        retry["timestamp_ms"] += 1000
        retry["books"]["KRW-BTC"]["timestamp"] += 1000
        self.assertEqual(self.system.tick(retry)["status"], "duplicate")
        self.assertEqual(self.system.status(), before)
        self.assert_conserved_tick(self.frame())
        self.assertEqual(self.system.status()["tick"], 2)

    def test_tick_fault_rolls_back_fills_and_retries_same_frame(self):
        tables = [row[0] for row in self.system.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {table: self.rows(table) for table in tables}
        frame = self.frame()
        original = self.system._event
        def fail_after_fill(tick, kind, detail, agent=None, environment=None):
            if kind == "trade":
                raise RuntimeError("injected after broker fill")
            return original(tick, kind, detail, agent, environment)
        with patch.object(self.system, "_event", side_effect=fail_after_fill):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.system.tick(frame)
        self.assertEqual({table: self.rows(table) for table in tables}, before)
        self.assertFalse(self.system.db.in_transaction)
        self.assertEqual(self.assert_conserved_tick(frame)["status"], "processed")

    def test_other_survival_gates_retire_without_rescuing_trade(self):
        cases = [
            ("capital_floor", "max_drawdown = 1\nmin_equity_ratio = 0.95\ngrace_ticks = 1000\ninactivity_ticks = 1000", 7000),
            ("minimum_return", "max_drawdown = 1\ngrace_ticks = 2\nevaluation_every = 1\nmin_return = 0.01\ninactivity_ticks = 1000", 10000),
            ("benchmark_underperformance", "max_drawdown = 1\ngrace_ticks = 2\nevaluation_every = 1\nmin_return = -1\nmin_excess_return = 0.01\ninactivity_ticks = 1000", 10000),
            ("inactivity", "max_drawdown = 1\ngrace_ticks = 1000\ninactivity_ticks = 2", 10000),
        ]
        for reason, rules, price in cases:
            with self.subTest(reason=reason):
                self.system.close()
                config = Path(self.temp.name) / (reason + ".toml")
                config.write_text('[system]\nmarkets = ["KRW-BTC"]\n[survival]\n' + rules,
                                  encoding="utf-8")
                self.system = EvolutionSystem(str(Path(self.temp.name) / (reason + ".sqlite3")), str(config))
                self.system.initialize()
                self.feed = DemoFeed(self.system.config)
                initial = self.frame()
                if reason == "inactivity":
                    initial["contexts"]["KRW-BTC"]["chart"]["indicators"]["rsi14"] = 95
                self.assert_conserved_tick(initial)
                second = self.frame(price=price)
                if reason == "inactivity":
                    second["contexts"]["KRW-BTC"]["chart"]["indicators"]["rsi14"] = 95
                self.assert_conserved_tick(second)
                deaths = [agent for agent in self.system.status()["agents"] if agent["death_reason"] == reason]
                self.assertTrue(deaths)
                for agent in deaths:
                    self.assertEqual(self.system.db.execute("SELECT COUNT(*) FROM decisions WHERE tick=2 AND agent=?", (agent["id"],)).fetchone()[0], 0)

    def test_bad_frames_and_source_errors_do_not_advance_lifecycles(self):
        frame = self.frame()
        self.assert_conserved_tick(frame)
        before = self.system.status()
        next_frame = self.frame()
        for bad in ["stale_book", "future_candle", "mixed_source", "wrong_close"]:
            candidate = copy.deepcopy(next_frame)
            if bad == "stale_book":
                candidate["books"]["KRW-BTC"]["timestamp"] -= 31000
            elif bad == "future_candle":
                candidate["contexts"]["KRW-BTC"]["chart"]["candles"][-1]["time"] = "2099-01-01T00:00:00+00:00"
            elif bad == "mixed_source":
                candidate["source"] = "live"
            else:
                candidate["contexts"]["KRW-BTC"]["chart"]["latest_close"] += 1
            with self.subTest(frame=bad), self.assertRaises(ValueError):
                self.system.tick(candidate)
            self.assertEqual(self.system.status(), before)
        self.system.record_source_error("temporary network failure")
        self.assertEqual(self.system.status(), before)
        self.assertEqual(self.system.events(1)[0]["kind"], "source_error")

    def test_restart_changed_settings_is_rejected_without_resetting_capital(self):
        self.assert_conserved_tick(self.frame())
        before = self.system.status()
        self.system.close()
        config = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(config.replace('[system]', '[system]\nfee_rate = "0.01"'), encoding="utf-8")
        self.system = EvolutionSystem(self.path, str(self.config_path))
        with self.assertRaisesRegex(ValueError, "different saved config"):
            self.system.initialize()
        self.assertEqual(self.system.status(), before)

    def test_live_context_submillisecond_precision_is_accepted(self):
        frame = self.frame()
        frame["source"] = "live"
        # LiveFeed retains clock microseconds in context strings while its frame
        # timestamp is an integer number of milliseconds.
        precise = datetime.fromtimestamp(frame["timestamp_ms"] / 1000, timezone.utc) + timedelta(microseconds=500)
        context = frame["contexts"]["KRW-BTC"]
        context["created_at"] = context["chart"]["as_of"] = precise.isoformat()
        self.assertEqual(self.assert_conserved_tick(frame)["status"], "processed")
        before = self.system.status()
        for field in ["as_of", "created_at"]:
            candidate = self.frame()
            candidate["source"] = "live"
            future = datetime.fromtimestamp(candidate["timestamp_ms"] / 1000, timezone.utc) + timedelta(seconds=1)
            context = candidate["contexts"]["KRW-BTC"]
            if field == "as_of":
                context["chart"][field] = future.isoformat()
            else:
                context[field] = future.isoformat()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Future context timestamp"):
                self.system.tick(candidate)
            self.assertEqual(self.system.status(), before)

    def test_changed_config_cannot_bypass_initialize_or_rewrite_budget_accounting(self):
        self.assert_conserved_tick(self.frame())
        before = self.system.status()
        frame = self.frame()
        self.system.close()
        config = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(config + '\n[population]\nmain_budget = "2000000"\n', encoding="utf-8")
        self.system = EvolutionSystem(self.path, str(self.config_path))
        self.assertEqual(self.system.capital_accounting(), before["capital_accounting"])
        self.assertEqual(self.system.capital_accounting()["initial_virtual_capital"], "2000000")
        with self.assertRaisesRegex(ValueError, "different saved config"):
            self.system.tick(frame)
        self.assertEqual(self.system.status(), before)
        self.assertFalse(self.system.db.in_transaction)

    def test_identical_demo_runs_repeat_population_and_conserve_cost_accounting(self):
        other = EvolutionSystem(str(Path(self.temp.name) / "repeated.sqlite3"), str(self.config_path))
        try:
            other.initialize()
            first_feed, second_feed = DemoFeed(self.system.config, seed=19), DemoFeed(other.config, seed=19)
            for _ in range(40):
                first_frame, second_frame = first_feed.next_frame(), second_feed.next_frame()
                self.assertEqual(first_frame, second_frame)
                self.system.tick(first_frame)
                other.tick(second_frame)
                first, second = self.system.status(), other.status()
                self.assertEqual(first, second)
                self.assertLess(abs(Decimal(first["capital_accounting"]["identity_error"])), Decimal("1e-12"))
                self.assertLess(abs(Decimal(second["capital_accounting"]["identity_error"])), Decimal("1e-12"))
            self.assertTrue(any(agent["parents"] for agent in first["agents"]))
            self.assertGreater(len(first["environments"]), 1)
        finally:
            other.close()

    def test_agents_do_not_age_or_repeat_decisions_when_only_other_market_advances(self):
        self.system.close()
        config = self.config_path.read_text(encoding="utf-8")
        config = config.replace('markets = ["KRW-BTC"]', 'markets = ["KRW-BTC", "KRW-ETH"]')
        config = config.replace("every_ticks = 2", "every_ticks = 100")
        config = config.replace("spawn_every_ticks = 3", "spawn_every_ticks = 100")
        config = config.replace("environment_every_ticks = 4", "environment_every_ticks = 100")
        self.config_path.write_text(config, encoding="utf-8")
        self.path = str(Path(self.temp.name) / "multi-market.sqlite3")
        self.system = EvolutionSystem(self.path, str(self.config_path))
        self.system.initialize()
        feed = DemoFeed(self.system.config)
        first = feed.next_frame()
        self.assertEqual(self.system.tick(first)["status"], "processed")
        original_btc = copy.deepcopy(first["contexts"]["KRW-BTC"])
        last_bar = original_btc["chart"]["candles"][-1]["time"]
        for expected_eth_age in (2, 3):
            frame = feed.next_frame()
            context = copy.deepcopy(original_btc)
            context["created_at"] = frame["contexts"]["KRW-BTC"]["created_at"]
            context["chart"]["as_of"] = frame["contexts"]["KRW-BTC"]["chart"]["as_of"]
            context["chart"]["quality"]["latest_close_age_seconds"] = (frame["timestamp_ms"] - first["timestamp_ms"]) / 1000
            frame["contexts"]["KRW-BTC"] = context
            self.assertEqual(self.system.tick(frame)["status"], "processed")
            for agent in self.system.status()["agents"]:
                if agent["market"] == "KRW-BTC":
                    self.assertEqual(agent["age"], 1)
                    self.assertEqual(agent["last_bar"], last_bar)
                    self.assertEqual(self.system.db.execute("SELECT COUNT(*) FROM decisions WHERE agent=?", (agent["id"],)).fetchone()[0], 1)
                else:
                    self.assertEqual(agent["age"], expected_eth_age)
                    self.assertEqual(self.system.db.execute("SELECT COUNT(*) FROM decisions WHERE agent=?", (agent["id"],)).fetchone()[0], expected_eth_age)
            if expected_eth_age == 2:
                before_restart = self.system.status()
                self.system.close()
                self.system = EvolutionSystem(self.path, str(self.config_path))
                self.assertEqual(self.system.initialize(), before_restart)


if __name__ == "__main__":
    unittest.main()
