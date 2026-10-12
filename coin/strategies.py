"""Deterministic chart policies and bounded, reproducible strategy evolution.

Genomes contain policy parameters only. Wallets, ancestry and performance belong
to the agent engine. External headlines are never treated as executable signals.
"""
from datetime import timedelta
from math import isfinite

from .analysis import utc_time


BOUNDS = {
    "allocation": (0.05, 0.8), "stop_loss": (0.01, 0.3),
    "take_profit": (0.02, 0.8), "rsi_buy": (10, 65),
    "rsi_sell": (40, 95), "trend_buffer": (0, 0.05),
    "breakout_lookback": (5, 60), "sentiment_min": (0, 100),
}
BASE = dict(allocation=0.25, stop_loss=0.06, take_profit=0.18,
            rsi_buy=35, rsi_sell=75, trend_buffer=0.002,
            breakout_lookback=20, sentiment_min=0)


def default_genomes():
    return [dict(BASE, kind="trend", rsi_buy=45),
            dict(BASE, kind="reversion", rsi_buy=30, rsi_sell=65,
                 take_profit=0.10),
            dict(BASE, kind="breakout", rsi_buy=40, rsi_sell=85,
                 stop_loss=0.04)]


def validate_genome(genome):
    if not isinstance(genome, dict) or genome.get("kind") not in ("trend", "reversion", "breakout"):
        raise ValueError("Unknown strategy kind")
    if set(genome) - (set(BASE) | {"kind"}):
        raise ValueError("Unknown genome fields")
    result = dict(BASE, **genome)
    for key, (low, high) in BOUNDS.items():
        value = result[key]
        if isinstance(value, bool):
            raise ValueError(f"Invalid genome parameter: {key}")
        try:
            value = float(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid genome parameter: {key}") from exc
        if not isfinite(value) or not low <= value <= high:
            raise ValueError(f"Genome parameter out of range: {key}")
        if key == "breakout_lookback":
            if value != int(value):
                raise ValueError("Breakout lookback must be integral")
            value = int(value)
        result[key] = value
    if result["rsi_buy"] >= result["rsi_sell"]:
        raise ValueError("RSI buy threshold must be below sell threshold")
    return result


def _finite(value):
    value = float(value)
    if not isfinite(value):
        raise ValueError("Nonfinite chart value")
    return value


def sentiment_allows(genome, context):
    if genome["sentiment_min"] <= 0:
        return True
    valid = []
    as_of = utc_time(context.get("created_at", context["chart"]["as_of"]))
    for item in context.get("external", {}).get("items", []):
        if item.get("kind") != "sentiment":
            continue
        try:
            value = _finite(item["value"])
            elapsed = as_of - utc_time(item["published_at"])
            if 0 <= value <= 100 and timedelta(0) <= elapsed <= timedelta(hours=72):
                valid.append((utc_time(item["published_at"]), value))
        except (ValueError, TypeError, KeyError):
            continue
    return bool(valid) and max(valid)[1] >= genome["sentiment_min"]


def decide(genome, context, wallet):
    genome = validate_genome(genome)
    def decision(action, reason):
        return {"action": action, "reason": reason,
                "target_fraction": genome["allocation"] if action == "buy" else 0.0}
    try:
        chart = context["chart"]
        market = context["market"]
        quality = chart["quality"]
        price = _finite(chart["latest_close"])
        age = _finite(quality["latest_close_age_seconds"])
        unit = _finite(chart["unit_minutes"])
        if (chart["market"] != market or price <= 0 or unit <= 0
                or quality["stale"] or quality["gap_count"] != 0
                or age < 0 or age > unit * 120):
            return decision("hold", "Chart is stale, gapped or invalid")
        # Detect a reused bundle when the caller supplies a newer evaluation time.
        if "created_at" in context and "as_of" in chart:
            delta = (utc_time(context["created_at"]) - utc_time(chart["as_of"])).total_seconds()
            if delta < 0 or delta + age > unit * 120:
                return decision("hold", "Chart is stale at context time")
        position = next((p for p in wallet["positions"] if p["market"] == market), None)
        quantity = _finite(position["quantity"]) if position else 0
        cost = _finite(position["cost"]) if position else 0
        cash = _finite(wallet["cash"])
        if quantity < 0 or cost < 0 or cash < 0 or (quantity > 0 and cost <= 0):
            raise ValueError("Invalid wallet")
        if quantity > 0:
            change = price * quantity / cost - 1
            if change <= -genome["stop_loss"]:
                return decision("sell", "Stop loss reached")
            if change >= genome["take_profit"]:
                return decision("sell", "Take profit reached")
        indicators = chart["indicators"]
        rsi = _finite(indicators["rsi14"])
        if not 0 <= rsi <= 100:
            raise ValueError("Invalid RSI")
        kind = genome["kind"]
        if kind == "trend":
            fast, slow = _finite(indicators["sma20"]), _finite(indicators["sma50"])
            if min(fast, slow) <= 0:
                raise ValueError("Invalid moving average")
            buy = (fast > slow * (1 + genome["trend_buffer"])
                   and price > fast and genome["rsi_buy"] <= rsi < genome["rsi_sell"])
            sell = fast < slow or rsi >= genome["rsi_sell"]
            reason = "Trend averages and RSI"
        elif kind == "reversion":
            bands = indicators["bollinger20"]
            lower, middle = _finite(bands["lower"]), _finite(bands["middle"])
            if middle <= 0 or lower > middle:
                raise ValueError("Invalid Bollinger bands")
            buy = price <= lower and rsi <= genome["rsi_buy"]
            sell = price >= middle or rsi >= genome["rsi_sell"]
            reason = "Bollinger mean reversion and RSI"
        else:
            rows = chart["candles"]
            lookback = genome["breakout_lookback"]
            if len(rows) < lookback + 1:
                return decision("hold", "Insufficient closed bars for breakout")
            previous = rows[-lookback-1:-1]
            highs = [_finite(row["high"]) for row in previous]
            lows = [_finite(row["low"]) for row in previous]
            if min(lows) <= 0 or any(lo > hi for lo, hi in zip(lows, highs)):
                raise ValueError("Invalid breakout history")
            # Exclude the signal bar from the channel to avoid lookahead.
            volume = _finite(indicators["volume_ratio20"])
            buy = price > max(highs) * (1 + genome["trend_buffer"]) and volume >= 1 and rsi < genome["rsi_sell"]
            sell = price < min(lows) or rsi >= genome["rsi_sell"]
            reason = "Prior closed-bar channel breakout and volume"
        if quantity > 0:
            return decision("sell" if sell else "hold", reason if sell else "Existing position retained; no accumulating buys")
        if not buy or cash == 0:
            return decision("hold", "No entry signal" if cash else "No available cash")
        if not sentiment_allows(genome, context):
            return decision("hold", "Sentiment gate requires fresh evidence above threshold")
        return decision("buy", reason)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return decision("hold", "Required chart or wallet data is invalid or unavailable")


def breed(parent_a, parent_b, rng, mutation_rate=0.25):
    """Uniform crossover followed by mutation; one numeric gene always changes."""
    parent_a, parent_b = validate_genome(parent_a), validate_genome(parent_b)
    if not isfinite(mutation_rate) or not 0 <= mutation_rate <= 1:
        raise ValueError("Mutation rate must be between zero and one")
    child = {key: rng.choice((parent_a[key], parent_b[key])) for key in parent_a}
    # Crossing independently chosen RSI thresholds can violate their ordering.
    if child["rsi_buy"] >= child["rsi_sell"]:
        child["rsi_buy"], child["rsi_sell"] = parent_a["rsi_buy"], parent_a["rsi_sell"]
    before = dict(child)
    selected = [key for key in BOUNDS if rng.random() < mutation_rate]
    if not selected:
        selected = [rng.choice(list(BOUNDS))]
    for key in selected:
        low, high = BOUNDS[key]
        if key == "rsi_buy":
            high = min(high, child["rsi_sell"] - 0.001)
        if key == "rsi_sell":
            low = max(low, child["rsi_buy"] + 0.001)
        if key == "breakout_lookback":
            child[key] = rng.choice([value for value in range(low, high + 1) if value != child[key]])
        else:
            value = rng.uniform(low, high)
            child[key] = value if value != child[key] else (low if child[key] != low else high)
    assert child != before
    return validate_genome(child)
