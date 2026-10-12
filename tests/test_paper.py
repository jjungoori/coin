import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from coin.paper import PaperBroker


def book():
    return {"market": "KRW-BTC", "timestamp": int(time.time() * 1000), "orderbook_units": [
        {"ask_price": 10000, "ask_size": 1, "bid_price": 9900, "bid_size": 1},
        {"ask_price": 11000, "ask_size": 10, "bid_price": 9800, "bid_size": 10}]}


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "paper.sqlite3")
        self.broker = PaperBroker(self.path, fee_rate="0.001")
        self.broker.create_wallet("alpha", "100000")
        self.broker.create_wallet("beta", "100000")

    def tearDown(self):
        self.broker.close()
        self.temp.cleanup()

    def test_depth_fee_isolation_and_persistence(self):
        trade = self.broker.execute("alpha", "KRW-BTC", "buy", "21021", book())
        self.assertEqual(Decimal(trade["quantity"]), 2)
        self.assertEqual(Decimal(trade["fee"]), 21)
        self.assertEqual(Decimal(self.broker.wallet("alpha")["cash"]), 78979)
        self.assertEqual(self.broker.wallet("beta")["positions"], [])
        self.broker.close()
        self.broker = PaperBroker(self.path, fee_rate="0.001")
        sold = self.broker.execute("alpha", "KRW-BTC", "sell", "2", book())
        self.assertEqual(Decimal(sold["notional"]), 19700)
        self.assertEqual(Decimal(sold["realized_pnl"]), Decimal("-1340.7"))
        report = self.broker.report("alpha", {})
        self.assertEqual(Decimal(report["pnl"]), Decimal("-1340.7"))

    def test_rejections_do_not_mutate_wallet(self):
        before = self.broker.wallet("alpha")
        stale = book()
        stale["timestamp"] -= 31000
        cases = [("buy", "6000", stale), ("buy", "999999", book()),
                 ("sell", "1", book()), ("buy", "100", book()),
                 ("buy", "NaN", book()), ("buy", "-1", book())]
        for side, amount, snapshot in cases:
            with self.subTest(side=side, amount=amount):
                with self.assertRaises(ValueError):
                    self.broker.execute("alpha", "KRW-BTC", side, amount, snapshot)
                self.assertEqual(self.broker.wallet("alpha"), before)
                self.assertEqual(self.broker.history("alpha"), [])

    def test_cash_rejection_rolls_back(self):
        self.broker.create_wallet("small", "5000")
        with self.assertRaises(ValueError):
            self.broker.execute("small", "KRW-BTC", "buy", "6000", book())
        self.assertEqual(self.broker.wallet("small")["cash"], "5000")
        self.assertEqual(self.broker.history("small"), [])

    def test_unrealized_pnl(self):
        self.broker.execute("alpha", "KRW-BTC", "buy", "10010", book())
        self.assertEqual(Decimal(self.broker.report("alpha", {"KRW-BTC": "12000"})["pnl"]), 1990)

    def test_duplicate_wallet_does_not_reset(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.broker.create_wallet("alpha", "200000")
        self.assertEqual(self.broker.wallet("alpha")["cash"], "100000")

    def test_order_retry_returns_original_fill_after_restart(self):
        snapshot = book()
        original = self.broker.execute("alpha", "KRW-BTC", "buy", "10010", snapshot, order_key="cycle-1")
        self.broker.execute("alpha", "KRW-BTC", "buy", "10010", book())
        before = self.broker.wallet("alpha")
        self.broker.close()
        self.broker = PaperBroker(self.path, fee_rate="0.001")
        snapshot["timestamp"] = 1
        retried = self.broker.execute("alpha", "KRW-BTC", "buy", "10010.00", snapshot, order_key="cycle-1")
        self.assertEqual(retried, original)
        self.assertEqual(self.broker.wallet("alpha"), before)
        self.assertEqual(len(self.broker.history("alpha")), 2)
        for agent, side, amount in [("beta", "buy", "10010"), ("alpha", "sell", "10010"), ("alpha", "buy", "11000")]:
            with self.assertRaisesRegex(ValueError, "different request"):
                self.broker.execute(agent, "KRW-BTC", side, amount, book(), order_key="cycle-1")

    def test_outer_transaction_rolls_back_all_broker_state(self):
        before = self.broker.wallet("alpha")
        self.broker.db.execute("BEGIN IMMEDIATE")
        self.broker.create_wallet("child", "50000")
        self.broker.set_wallet_status("child", "active")
        self.broker.execute("alpha", "KRW-BTC", "buy", "10010", book(), order_key="outer")
        self.broker.db.execute("ROLLBACK")
        self.assertEqual(self.broker.wallet("alpha"), before)
        self.assertEqual(self.broker.history("alpha"), [])
        self.assertEqual(self.broker.db.execute("SELECT count(*) FROM order_keys").fetchone()[0], 0)
        self.assertEqual(self.broker.db.execute("SELECT count(*) FROM wallet_controls").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            self.broker.wallet("child")

    def test_failed_order_preserves_outer_transaction(self):
        self.broker.db.execute("BEGIN IMMEDIATE")
        self.broker.create_wallet("child", "50000")
        with self.assertRaises(ValueError):
            self.broker.execute("alpha", "KRW-BTC", "sell", "1", book())
        self.assertTrue(self.broker.db.in_transaction)
        self.broker.db.execute("COMMIT")
        self.assertEqual(self.broker.wallet("child")["cash"], "50000")

    def test_dead_managed_wallet_rejects_direct_orders(self):
        self.broker.set_wallet_status("alpha", "active")
        self.broker.execute("alpha", "KRW-BTC", "buy", "10010", book())
        self.broker.set_wallet_status("alpha", "dead")
        before = self.broker.wallet("alpha")
        for side in ["buy", "sell"]:
            with self.assertRaisesRegex(ValueError, "Dead agent"):
                self.broker.execute("alpha", "KRW-BTC", side, "1", book())
        self.assertEqual(self.broker.wallet("alpha"), before)
        self.broker.execute("beta", "KRW-BTC", "buy", "10010", book())
        with self.assertRaises(ValueError):
            self.broker.set_wallet_status("alpha", "unknown")

    def test_injected_clock_freshness_and_trade_timestamp(self):
        snapshot = book()
        snapshot["timestamp"] = 100000
        self.broker.execute("alpha", "KRW-BTC", "buy", "10010", snapshot, now_ms=101000)
        self.assertEqual(self.broker.history("alpha")[0]["created_ms"], 101000)
        for timestamp in [131001, 94000]:
            with self.assertRaisesRegex(ValueError, "stale or future"):
                self.broker.execute("alpha", "KRW-BTC", "buy", "10010", snapshot, now_ms=timestamp)
        with self.assertRaisesRegex(ValueError, "integer timestamp"):
            self.broker.execute("alpha", "KRW-BTC", "buy", "10010", snapshot, now_ms=101000.5)


if __name__ == "__main__":
    unittest.main()
