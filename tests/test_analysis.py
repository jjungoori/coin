import unittest
from datetime import datetime, timedelta, timezone

from coin.analysis import analyze


def candles(n=60):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [{"market": "KRW-BTC", "candle_date_time_utc": (start + timedelta(minutes=5*i)).isoformat(),
             "opening_price": 100+i, "high_price": 101+i, "low_price": 99+i,
             "trade_price": 100+i, "candle_acc_trade_volume": 10} for i in range(n)]


class AnalysisTests(unittest.TestCase):
    now = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)

    def test_reference_values_and_sort(self):
        result = analyze(list(reversed(candles())), "KRW-BTC", 5, self.now)
        values = result["indicators"]
        self.assertEqual(values["sma20"], 149.5)
        self.assertEqual(values["rsi14"], 100)
        self.assertAlmostEqual(values["macd"], 7)
        self.assertAlmostEqual(values["macd_signal"], 7)
        self.assertAlmostEqual(values["macd_histogram"], 0)
        self.assertEqual(values["atr14"], 2)
        self.assertEqual(values["volume_ratio20"], 1)
        self.assertEqual(result["quality"]["gap_count"], 0)

    def test_excludes_forming_candle_and_no_lookahead(self):
        result = analyze(candles(61), "KRW-BTC", 5, self.now)
        self.assertEqual(result["closed_candles"], 60)
        self.assertEqual(result["latest_close"], 159)

    def test_insufficient_bars_and_gaps(self):
        rows = candles(10)
        del rows[5]
        result = analyze(rows, "KRW-BTC", 5, self.now)
        self.assertIsNone(result["indicators"]["rsi14"])
        self.assertIsNone(result["indicators"]["macd_signal"])
        self.assertEqual(result["quality"]["gap_count"], 1)
        self.assertTrue(result["quality"]["stale"])

    def test_flat_rsi_and_bad_ohlc(self):
        rows = candles()
        for r in rows:
            r.update(opening_price=100, high_price=101, low_price=99, trade_price=100)
        self.assertEqual(analyze(rows, "KRW-BTC", 5, self.now)["indicators"]["rsi14"], 50)
        rows[0]["trade_price"] = float("nan")
        with self.assertRaises(ValueError):
            analyze(rows, "KRW-BTC", 5, self.now)
