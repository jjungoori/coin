"""Opt-in live public API check with disposable local paper wallets.

Run: python -m coin.smoke
"""
import tempfile
from pathlib import Path

from .bithumb import BithumbPublicClient
from .paper import PaperBroker


def main():
    client = BithumbPublicClient()
    markets = client.markets()
    ticker = client.ticker(["KRW-BTC"])[0]
    candles = client.candles("KRW-BTC", 5, 2)
    with tempfile.TemporaryDirectory() as directory:
        broker = PaperBroker(str(Path(directory) / "smoke.sqlite3"))
        try:
            broker.create_wallet("smoke-alpha")
            broker.create_wallet("smoke-beta")
            buy = broker.execute("smoke-alpha", "KRW-BTC", "buy", "10000", client.orderbook("KRW-BTC"))
            broker.execute("smoke-alpha", "KRW-BTC", "sell", buy["quantity"], client.orderbook("KRW-BTC"))
            if len(broker.history("smoke-alpha")) != 2 or broker.wallet("smoke-beta")["positions"]:
                raise RuntimeError("Paper wallet verification failed")
            print({"markets": len(markets), "ticker_market": ticker["market"], "candles": len(candles),
                   "paper_round_trip": "passed", "wallet_isolation": "passed"})
        finally:
            broker.close()


if __name__ == "__main__":
    main()
