"""Atomic, agent-isolated spot paper wallets; Decimal amounts stored as text."""
import json
import sqlite3
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

from .bithumb import validate_market

QUANTUM = Decimal("0.00000001")


def number(value, *, positive=True):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid numeric amount") from exc
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise ValueError("Amount must be finite and positive (or nonnegative for rates)")
    return result


class PaperBroker:
    def __init__(self, db="data/paper.sqlite3", fee_rate="0.0004", min_notional="5000", max_age_seconds=30):
        self.fee_rate = number(fee_rate, positive=False)
        if self.fee_rate >= 1:
            raise ValueError("Fee rate must be below 1")
        self.min_notional = number(min_notional)
        self.max_age_seconds = number(max_age_seconds)
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS wallets (
                agent TEXT PRIMARY KEY, initial_cash TEXT NOT NULL, cash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions (
                agent TEXT NOT NULL REFERENCES wallets(agent), market TEXT NOT NULL,
                quantity TEXT NOT NULL, cost TEXT NOT NULL, PRIMARY KEY(agent, market));
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY, agent TEXT NOT NULL REFERENCES wallets(agent),
                market TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL,
                notional TEXT NOT NULL, fee TEXT NOT NULL, realized_pnl TEXT NOT NULL,
                created_ms INTEGER NOT NULL, book_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS order_keys (
                order_key TEXT PRIMARY KEY, trade_id TEXT NOT NULL REFERENCES trades(id),
                request_json TEXT NOT NULL, result_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wallet_controls (
                agent TEXT PRIMARY KEY REFERENCES wallets(agent),
                status TEXT NOT NULL CHECK(status IN ('active', 'dead')));
        ''')

    def close(self):
        self.db.close()

    def create_wallet(self, agent, cash="1000000"):
        if not agent.strip():
            raise ValueError("Agent name is required")
        cash = str(number(cash))
        self.db.execute("INSERT INTO wallets VALUES (?, ?, ?)", (agent, cash, cash))
        return self.wallet(agent)

    def wallet(self, agent):
        row = self.db.execute("SELECT * FROM wallets WHERE agent=?", (agent,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown agent: {agent}")
        return {**dict(row), "positions": [dict(p) for p in self.db.execute(
            "SELECT market, quantity, cost FROM positions WHERE agent=?", (agent,))]}

    def history(self, agent):
        self.wallet(agent)
        return [dict(t) for t in self.db.execute(
            "SELECT id, market, side, quantity, notional, fee, realized_pnl, created_ms FROM trades WHERE agent=? ORDER BY created_ms, rowid", (agent,))]

    def set_wallet_status(self, agent, status):
        if status not in ('active', 'dead'):
            raise ValueError("Wallet status must be active or dead")
        self.wallet(agent)
        self.db.execute("INSERT INTO wallet_controls VALUES (?, ?) ON CONFLICT(agent) DO UPDATE SET status=excluded.status",
                        (agent, status))

    def execute(self, agent, market, side, amount, book, *, now_ms=None, order_key=None):
        """Execute atomically, including inside an enclosing engine transaction.

        An order key identifies one immutable economic request. Retries return its
        original result, even if the supplied snapshot has since expired.
        """
        savepoint = 'paper_' + uuid.uuid4().hex
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            result = self._execute(agent, market, side, amount, book, now_ms=now_ms, order_key=order_key)
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return result
        except BaseException:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def _execute(self, agent, market, side, amount, book, *, now_ms, order_key):
        """Buy amount = KRW budget including fee. Sell amount = coin quantity.

        Walk displayed depth; reject entire order if liquidity is insufficient.
        A snapshot's liquidity is not shared across independent simulations.
        """
        validate_market(market)
        if not market.startswith("KRW-") or side not in ("buy", "sell"):
            raise ValueError("Only KRW spot buy/sell is supported")
        amount = number(amount)
        # Decimal.normalize() is context-sensitive; fixed-point formatting keeps
        # equivalent requests comparable without rounding arbitrary input.
        canonical_amount = format(amount, 'f')
        if '.' in canonical_amount:
            canonical_amount = canonical_amount.rstrip('0').rstrip('.')
        request = json.dumps([agent, market, side, canonical_amount])
        if order_key is not None:
            if not isinstance(order_key, str) or not order_key.strip():
                raise ValueError("Order key must be a nonempty string")
            recorded = self.db.execute("SELECT request_json, result_json FROM order_keys WHERE order_key=?", (order_key,)).fetchone()
            if recorded:
                if recorded['request_json'] != request:
                    raise ValueError("Order key was already used for a different request")
                return json.loads(recorded['result_json'])
        control = self.db.execute("SELECT status FROM wallet_controls WHERE agent=?", (agent,)).fetchone()
        if control and control['status'] == 'dead':
            raise ValueError("Dead agent wallet cannot trade")
        if now_ms is not None and (isinstance(now_ms, bool) or not isinstance(now_ms, int)):
            raise ValueError("now_ms must be an integer timestamp")
        now = int(time.time() * 1000) if now_ms is None else now_ms
        timestamp = number(book.get("timestamp", 0))
        age = Decimal(now) - timestamp
        if book.get("market") != market or age < -5000 or age > self.max_age_seconds * 1000:
            raise ValueError("Mismatched, stale or future orderbook")
        units = book.get("orderbook_units", [])
        price_key, size_key = ("ask_price", "ask_size") if side == "buy" else ("bid_price", "bid_size")
        levels = sorted([(number(u[price_key]), number(u[size_key], positive=False)) for u in units],
                        reverse=side == "sell")
        remaining = amount / (1 + self.fee_rate) if side == "buy" else amount
        quantity, notional = Decimal(0), Decimal(0)
        for price, size in levels:
            fill = min(size, remaining / price if side == "buy" else remaining)
            quantity += fill
            notional += fill * price
            remaining -= fill * price if side == "buy" else fill
            if remaining <= 0:
                break
        if remaining > Decimal("1e-18"):
            raise ValueError("Insufficient displayed liquidity; no fill")
        # Re-walk depth for the rounded executable coin quantity.
        quantity = quantity.quantize(QUANTUM, rounding=ROUND_DOWN)
        if side == "sell" and quantity != amount:
            raise ValueError("Sell quantity supports at most 8 decimal places")
        left, notional = quantity, Decimal(0)
        for price, size in levels:
            fill = min(left, size)
            notional += fill * price
            left -= fill
            if left == 0:
                break
        if quantity <= 0 or notional < self.min_notional:
            raise ValueError("Order is below configured minimum notional")
        fee = notional * self.fee_rate
        wallet = self.wallet(agent)
        cash = Decimal(wallet["cash"])
        pos = self.db.execute("SELECT quantity, cost FROM positions WHERE agent=? AND market=?", (agent, market)).fetchone()
        held, cost = (Decimal(pos[0]), Decimal(pos[1])) if pos else (Decimal(0), Decimal(0))
        realized = Decimal(0)
        if side == "buy":
            if cash < amount:
                raise ValueError("Insufficient wallet cash")
            cash -= notional + fee
            held += quantity
            cost += notional + fee
        else:
            if held < quantity:
                raise ValueError("Insufficient wallet coin balance")
            removed_cost = cost * quantity / held
            realized = notional - fee - removed_cost
            cost -= removed_cost
            held -= quantity
            cash += notional - fee
        self.db.execute("UPDATE wallets SET cash=? WHERE agent=?", (str(cash), agent))
        self.db.execute("INSERT INTO positions VALUES (?, ?, ?, ?) ON CONFLICT(agent, market) DO UPDATE SET quantity=excluded.quantity, cost=excluded.cost", (agent, market, str(held), str(cost)))
        trade_id = str(uuid.uuid4())
        self.db.execute("INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trade_id, agent, market, side, str(quantity), str(notional), str(fee), str(realized), now, json.dumps(book, default=str)))
        result = {"id": trade_id, "agent": agent, "market": market, "side": side,
                "quantity": str(quantity), "notional": str(notional), "fee": str(fee),
                "realized_pnl": str(realized), "cash": str(cash)}
        if order_key is not None:
            self.db.execute("INSERT INTO order_keys VALUES (?, ?, ?, ?)",
                            (order_key, trade_id, request, json.dumps(result)))
        return result

    def report(self, agent, prices):
        wallet = self.wallet(agent)
        equity = Decimal(wallet["cash"])
        for position in wallet["positions"]:
            qty = Decimal(position["quantity"])
            if qty:
                equity += qty * number(prices[position["market"]])
        initial = Decimal(wallet["initial_cash"])
        return {**wallet, "equity": str(equity), "pnl": str(equity - initial),
                "return_pct": str((equity / initial - 1) * 100),
                "realized_pnl": str(sum((Decimal(t["realized_pnl"]) for t in self.history(agent)), Decimal(0)))}
