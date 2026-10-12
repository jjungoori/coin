import copy
import random
import unittest

from coin.strategies import breed, decide, default_genomes, validate_genome


def context():
    return {"market": "KRW-BTC", "created_at": "2026-10-12T00:00:00+00:00",
            "external": {"items": []}, "chart": {
                "market": "KRW-BTC", "as_of": "2026-10-12T00:00:00+00:00",
                "unit_minutes": 5, "latest_close": 105, "closed_candles": 60,
                "quality": {"stale": False, "gap_count": 0, "latest_close_age_seconds": 0},
                "indicators": {"rsi14": 60, "sma20": 102, "sma50": 100,
                               "bollinger20": {"lower": 98, "middle": 100, "upper": 102},
                               "volume_ratio20": 1.5},
                "candles": [{"high": 103, "low": 99, "close": 101}] * 59
                           + [{"high": 106, "low": 100, "close": 105}]}}


def wallet(quantity=0, cost=0):
    return {"cash": "1000000", "initial_cash": "1000000",
            "positions": [{"market": "KRW-BTC", "quantity": str(quantity), "cost": str(cost)}]}


class StrategyTests(unittest.TestCase):
    def test_independent_default_policies(self):
        trend, reversion, breakout = default_genomes()
        sample = context()
        self.assertEqual(decide(trend, sample, wallet())["action"], "buy")
        self.assertEqual(decide(breakout, sample, wallet())["action"], "buy")
        self.assertEqual(decide(reversion, sample, wallet())["action"], "hold")
        sample["chart"]["latest_close"] = 97
        sample["chart"]["indicators"]["rsi14"] = 25
        self.assertEqual(decide(reversion, sample, wallet())["action"], "buy")
        self.assertEqual(decide(trend, sample, wallet())["action"], "hold")

    def test_risk_exit_precedes_unavailable_indicators(self):
        for genome in default_genomes():
            sample = context()
            sample["chart"]["indicators"] = {}
            sample["chart"]["latest_close"] = 80
            result = decide(genome, sample, wallet(10, 1000))
            self.assertEqual(result["action"], "sell")
            self.assertEqual(result["reason"], "Stop loss reached")
            sample["chart"]["latest_close"] = 200
            self.assertEqual(decide(genome, sample, wallet(10, 1000))["reason"], "Take profit reached")

    def test_no_accumulating_buys_and_wallet_isolation(self):
        trend = default_genomes()[0]
        self.assertEqual(decide(trend, context(), wallet(10, 1000))["action"], "hold")
        self.assertEqual(decide(trend, context(), wallet())["action"], "buy")
        other = wallet()
        other["positions"][0]["market"] = "KRW-ETH"
        other["positions"][0]["quantity"] = "10"
        self.assertEqual(decide(trend, context(), other)["action"], "buy")

    def test_strategy_exits_before_risk_thresholds(self):
        trend, reversion, breakout = default_genomes()
        sample = context()
        sample["chart"]["indicators"]["sma20"] = 99
        self.assertEqual(decide(trend, sample, wallet(10, 1050))["action"], "sell")
        self.assertEqual(decide(reversion, sample, wallet(10, 1050))["action"], "sell")
        sample["chart"]["latest_close"] = 98
        self.assertEqual(decide(breakout, sample, wallet(10, 990))["action"], "sell")

    def test_breakout_excludes_signal_bar_and_needs_history(self):
        genome = default_genomes()[2]
        sample = context()
        self.assertEqual(decide(genome, sample, wallet())["action"], "buy")
        sample["chart"]["candles"] = sample["chart"]["candles"][-20:]
        self.assertEqual(decide(genome, sample, wallet())["action"], "hold")
        sample = context()
        sample["chart"]["indicators"]["volume_ratio20"] = 0.5
        self.assertEqual(decide(genome, sample, wallet())["action"], "hold")

    def test_invalid_stale_and_gapped_chart_fail_closed(self):
        genome = default_genomes()[0]
        variations = [({}, "stale", True), ({}, "gap_count", 1),
                      ({}, "latest_close_age_seconds", -1),
                      ({}, "latest_close_age_seconds", 601)]
        for _, key, value in variations:
            sample = context()
            sample["chart"]["quality"][key] = value
            self.assertEqual(decide(genome, sample, wallet())["action"], "hold")
        for field, value in (("latest_close", float("nan")), ("market", "KRW-ETH")):
            sample = context()
            sample["chart"][field] = value
            self.assertEqual(decide(genome, sample, wallet())["action"], "hold")
        sample = context()
        sample["created_at"] = "2026-10-12T00:30:00+00:00"
        self.assertEqual(decide(genome, sample, wallet())["action"], "hold")
        sample = context()
        sample["chart"]["indicators"]["rsi14"] = None
        self.assertEqual(decide(genome, sample, wallet())["action"], "hold")

    def test_optional_sentiment_is_fresh_and_blocks_entries_only(self):
        genome = dict(default_genomes()[0], sentiment_min=40)
        sample = context()
        self.assertEqual(decide(genome, sample, wallet())["action"], "hold")
        item = {"kind": "sentiment", "value": 50, "published_at": "2026-10-11T00:00:00+00:00"}
        sample["external"]["items"] = [item]
        self.assertEqual(decide(genome, sample, wallet())["action"], "buy")
        item["value"] = 20
        self.assertEqual(decide(genome, sample, wallet())["action"], "hold")
        item.update(value=50, published_at="2026-10-01T00:00:00+00:00")
        self.assertEqual(decide(genome, sample, wallet())["action"], "hold")
        item["published_at"] = "2026-10-13T00:00:00+00:00"
        self.assertEqual(decide(genome, sample, wallet())["action"], "hold")
        sample["chart"]["latest_close"] = 80
        self.assertEqual(decide(genome, sample, wallet(10, 1000))["action"], "sell")

    def test_genome_validation_and_copying(self):
        genome = default_genomes()[0]
        normalized = validate_genome(genome)
        normalized["allocation"] = 0.8
        self.assertEqual(genome["allocation"], 0.25)
        for fields in ({"kind": "unknown"}, {"allocation": 0.99}, {"stop_loss": float("inf")},
                       {"rsi_buy": 65, "rsi_sell": 40}, {"breakout_lookback": 5.5},
                       {"allocation": True}, {"injected": 1}):
            with self.assertRaises(ValueError):
                validate_genome(dict(genome, **fields))

    def test_evolution_reproducible_bounded_and_does_not_modify_parents(self):
        a, b = default_genomes()[:2]
        original = copy.deepcopy((a, b))
        self.assertEqual(breed(a, b, random.Random(10)), breed(a, b, random.Random(10)))
        rng = random.Random(25)
        for _ in range(1000):
            child = breed(a, b, rng, mutation_rate=1)
            self.assertEqual(validate_genome(child), child)
            a = child
        self.assertEqual(b, original[1])
        parent = default_genomes()[0]
        for seed in range(30):
            self.assertNotEqual(breed(parent, parent, random.Random(seed), mutation_rate=0), parent)
        self.assertEqual(parent, original[0])
        with self.assertRaises(ValueError):
            breed(parent, b, rng, mutation_rate=2)
