"""Read-only live observations and deterministic, network-free paper fixtures."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import sin
from random import Random

from .analysis import analyze
from .bithumb import BithumbPublicClient
from .research import collect


def context(candles, market, unit, now, external):
    return {"schema_version": 1, "market": market, "created_at": now.isoformat(),
            "chart": analyze(candles, market, unit, now), "external": deepcopy(external),
            "purpose": "Strategy input for paper trading; does not execute real orders"}


class LiveFeed:
    def __init__(self, config, client=None, clock=None, collector=None):
        self.settings = config["system"]
        self.client = client or BithumbPublicClient()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.collector = collector or collect
        self.ticks = 0
        self.external = {"items": [], "providers": [], "status": "not_requested"}

    def next_frame(self):
        unit = self.settings.get("unit", 5)
        every = self.settings.get("research_every_ticks", 12)
        # Slow external providers must not age already-fetched orderbooks.
        if self.settings.get("external", False) and self.ticks % every == 0:
            self.external = self.collector(self.settings.get("research_config", "research.toml"), self.clock())
        candles, books = {}, {}
        for market in self.settings["markets"]:
            candles[market] = self.client.candles(market, unit, 200)
        for market in self.settings["markets"]:
            books[market] = self.client.orderbook(market)
        now = self.clock()
        contexts = {market: context(rows, market, unit, now, self.external)
                    for market, rows in candles.items()}
        self.ticks += 1
        return {"timestamp_ms": int(now.timestamp() * 1000), "contexts": contexts,
                "books": books, "source": "live"}


class DemoFeed:
    """Synthetic regimes for plumbing verification, not profitability evidence."""
    def __init__(self, config, seed=7):
        self.settings = config["system"]
        self.random = Random(seed)
        self.unit = self.settings.get("unit", 5)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.ticks = 0
        self.history = {}
        self.prices = {}
        for index, market in enumerate(self.settings["markets"]):
            price = 100_000_000.0 if market == "KRW-BTC" else 3_000_000.0 * (index + 1)
            rows = []
            for bar in range(200):
                opening = price
                price *= 1 + 0.002 * sin(bar / 7) + self.random.uniform(-0.001, 0.001)
                start = self.now - timedelta(minutes=self.unit * (200 - bar))
                rows.append(self._bar(market, start, opening, price))
            self.history[market], self.prices[market] = rows, price

    def _bar(self, market, start, opening, close):
        return {"market": market, "candle_date_time_utc": start.isoformat(),
                "opening_price": opening, "high_price": max(opening, close) * 1.003,
                "low_price": min(opening, close) * 0.997, "trade_price": close,
                "candle_acc_trade_volume": self.random.uniform(20, 50)}

    def next_frame(self):
        start = self.now
        self.now += timedelta(minutes=self.unit)
        timestamp = int(self.now.timestamp() * 1000)
        contexts, books = {}, {}
        regime = (self.ticks // 16) % 3
        for index, market in enumerate(self.settings["markets"]):
            opening = self.prices[market]
            drift = (0.014, -0.022, 0.006 * sin(self.ticks * 1.5 + index))[regime]
            price = opening * (1 + drift + self.random.uniform(-0.002, 0.002))
            self.prices[market] = price
            self.history[market].append(self._bar(market, start, opening, price))
            self.history[market] = self.history[market][-200:]
            contexts[market] = context(self.history[market], market, self.unit, self.now,
                                       {"items": [], "providers": [], "status": "demo_no_external"})
            books[market] = {"market": market, "timestamp": timestamp,
                             "total_ask_size": 10_000, "total_bid_size": 10_000,
                             "orderbook_units": [{"ask_price": price * 1.0005,
                                                  "bid_price": price * 0.9995,
                                                  "ask_size": 10_000, "bid_size": 10_000}]}
        self.ticks += 1
        return {"timestamp_ms": timestamp, "contexts": contexts, "books": books, "source": "demo"}
