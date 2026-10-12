import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from coin.feeds import DemoFeed, LiveFeed


def config(external=False):
    return {"system": {"markets": ["KRW-BTC", "KRW-ETH"], "unit": 5,
                       "external": external, "research_every_ticks": 2}}


class FeedTests(unittest.TestCase):
    def test_demo_repeatable_with_closed_history_and_simulated_book_clock(self):
        first, second = DemoFeed(config(), 7), DemoFeed(config(), 7)
        for _ in range(55):
            frame = first.next_frame()
            self.assertEqual(frame, second.next_frame())
            now = datetime.fromtimestamp(frame["timestamp_ms"] / 1000, timezone.utc)
            for market, context in frame["contexts"].items():
                chart = context["chart"]
                self.assertEqual(chart["closed_candles"], 200)
                self.assertFalse(chart["quality"]["stale"])
                self.assertEqual(frame["books"][market]["timestamp"], frame["timestamp_ms"])
                for candle in chart["candles"]:
                    self.assertLessEqual(datetime.fromisoformat(candle["time"]) + timedelta(minutes=5), now)

    def test_live_external_shared_cached_and_forming_candle_excluded(self):
        demo = DemoFeed(config())
        demo.next_frame()
        now = demo.now
        client = Mock()
        client.candles.side_effect = lambda market, unit, count: demo.history[market] + [
            demo._bar(market, now, 100, 100)]
        client.orderbook.side_effect = lambda market: {"market": market, "timestamp": int(now.timestamp() * 1000)}
        collector = Mock(return_value={"items": [], "providers": [], "complete": True})
        feed = LiveFeed(config(True), client, lambda: now, collector)
        frames = [feed.next_frame() for _ in range(3)]
        self.assertEqual(collector.call_count, 2)
        self.assertEqual(client.candles.call_count, 6)
        self.assertEqual(client.orderbook.call_count, 6)
        for frame in frames:
            self.assertEqual(frame["source"], "live")
            self.assertEqual(frame["contexts"]["KRW-BTC"]["chart"]["closed_candles"], 200)

    def test_live_no_external_without_opt_in_and_failed_frame_not_counted(self):
        client, collector = Mock(), Mock()
        client.candles.side_effect = RuntimeError("unavailable")
        feed = LiveFeed(config(), client, collector=collector)
        with self.assertRaises(RuntimeError):
            feed.next_frame()
        collector.assert_not_called()
        self.assertEqual(feed.ticks, 0)

    def test_market_failure_never_returns_partial_frame(self):
        demo = DemoFeed(config())
        client = Mock()
        client.candles.side_effect = lambda market, unit, count: demo.history[market]
        client.orderbook.side_effect = [{"market": "KRW-BTC"}, RuntimeError("ETH book unavailable")]
        feed = LiveFeed(config(), client, lambda: demo.now)
        with self.assertRaisesRegex(RuntimeError, "ETH book unavailable"):
            feed.next_frame()
        self.assertEqual(feed.ticks, 0)

    def test_slow_research_precedes_fresh_books_and_final_analysis_clock(self):
        demo = DemoFeed(config())
        calls = []
        simulated = [demo.now]
        def research(path, now):
            calls.append("external")
            simulated[0] += timedelta(seconds=45)
            return {"items": [], "providers": []}
        def candles(market, unit, count):
            calls.append("candles")
            return demo.history[market]
        def book(market):
            calls.append("book")
            return {"market": market, "timestamp": int(simulated[0].timestamp() * 1000)}
        client = Mock()
        client.candles.side_effect = candles
        client.orderbook.side_effect = book
        frame = LiveFeed(config(True), client, lambda: simulated[0], research).next_frame()
        self.assertEqual(calls, ["external", "candles", "candles", "book", "book"])
        self.assertEqual(frame["books"]["KRW-BTC"]["timestamp"], frame["timestamp_ms"])
        self.assertEqual(frame["contexts"]["KRW-BTC"]["chart"]["as_of"], simulated[0].isoformat())


if __name__ == "__main__":
    unittest.main()
