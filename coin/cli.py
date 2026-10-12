import argparse
import json
import sqlite3
import sys

from .bithumb import APIError, BithumbPublicClient
from .paper import PaperBroker
from .analysis import analyze
from .context import build_context, save_context
from .research import collect


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bithumb public data and local paper wallets")
    parser.add_argument("--db", default="data/paper.sqlite3")
    parser.add_argument("--fee-rate", default="0.0004", help="Simulation assumption, not the verified account fee")
    parser.add_argument("--min-notional", default="5000")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("markets")
    for command in ("analyze", "context"):
        p = sub.add_parser(command)
        p.add_argument("market")
        p.add_argument("--unit", type=int, default=5)
        p.add_argument("--count", type=int, default=200)
        if command == "context":
            p.add_argument("--external", action="store_true", help="Fetch configured external sources")
            p.add_argument("--config", default="research.toml")
            p.add_argument("--evidence", help="JSON file with sourced manual evidence")
            p.add_argument("--save", action="store_true", help="Archive input bundle in data/research")
    p = sub.add_parser("research")
    p.add_argument("--config", default="research.toml")
    for command in ("ticker", "orderbook", "candles"):
        p = sub.add_parser(command)
        p.add_argument("market")
        if command == "candles":
            p.add_argument("--unit", type=int, default=1)
            p.add_argument("--count", type=int, default=100)
            p.add_argument("--to", help="Exclusive KST time: YYYY-MM-DDTHH:MM:SS")
    p = sub.add_parser("create-wallet")
    p.add_argument("agent")
    p.add_argument("--cash", default="1000000")
    for command in ("wallet", "history", "report"):
        sub.add_parser(command).add_argument("agent")
    for command in ("buy", "sell"):
        p = sub.add_parser(command)
        p.add_argument("agent")
        p.add_argument("market")
        p.add_argument("amount", help="KRW budget incl. fees for buy; coin quantity for sell")
    args = parser.parse_args(argv)
    client = BithumbPublicClient()
    broker = None
    try:
        if args.command == "markets":
            result = client.markets()
        elif args.command == "ticker":
            result = client.ticker([args.market])
        elif args.command == "orderbook":
            result = client.orderbook(args.market)
        elif args.command == "candles":
            result = client.candles(args.market, args.unit, args.count, args.to)
        elif args.command == "analyze":
            result = analyze(client.candles(args.market, args.unit, args.count), args.market, args.unit)
        elif args.command == "research":
            result = collect(args.config)
        elif args.command == "context":
            result = build_context(args.market, args.unit, args.count, args.external,
                                   args.config, args.evidence, client)
            if args.save:
                result["saved_to"] = save_context(result)
        else:
            broker = PaperBroker(args.db, args.fee_rate, args.min_notional)
            if args.command == "create-wallet":
                result = broker.create_wallet(args.agent, args.cash)
            elif args.command == "wallet":
                result = broker.wallet(args.agent)
            elif args.command == "history":
                result = broker.history(args.agent)
            elif args.command == "report":
                positions = broker.wallet(args.agent)["positions"]
                markets = [p["market"] for p in positions if float(p["quantity"]) > 0]
                prices = {t["market"]: t["trade_price"] for t in client.ticker(markets)} if markets else {}
                result = broker.report(args.agent, prices)
            else:
                result = broker.execute(args.agent, args.market, args.command, args.amount, client.orderbook(args.market))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (APIError, ValueError, sqlite3.Error, KeyError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if broker:
            broker.close()
