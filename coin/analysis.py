"""Closed-candle technical features. Float math is for analysis, not accounting."""
from datetime import datetime, timedelta, timezone
from math import isfinite, sqrt


def utc_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def ema(values, period):
    result = [None] * len(values)
    if len(values) >= period:
        result[period - 1] = sum(values[:period]) / period
        for i in range(period, len(values)):
            result[i] = values[i] * (2 / (period + 1)) + result[i - 1] * (1 - 2 / (period + 1))
    return result


def wilder(values, period):
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = (result * (period - 1) + value) / period
    return result


def analyze(candles, market, unit, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Analysis time must include timezone")
    if unit not in (1, 3, 5, 10, 15, 30, 60, 240):
        raise ValueError("Invalid candle unit")
    rows, seen = [], set()
    for candle in candles:
        if candle.get("market") != market:
            raise ValueError("Mismatched candle market")
        start = utc_time(candle["candle_date_time_utc"])
        if start + timedelta(minutes=unit) > now:
            continue
        if start in seen:
            raise ValueError("Duplicate candle timestamp")
        seen.add(start)
        values = [float(candle[key]) for key in (
            "opening_price", "high_price", "low_price", "trade_price", "candle_acc_trade_volume")]
        op, high, low, close, volume = values
        if not all(isfinite(v) for v in values) or min(values[:4]) <= 0 or volume < 0 or not low <= min(op, close) <= max(op, close) <= high:
            raise ValueError("Invalid OHLCV data")
        rows.append({"time": start.isoformat(), "open": op, "high": high, "low": low,
                     "close": close, "volume": volume})
    rows.sort(key=lambda r: r["time"])
    if not rows:
        raise ValueError("No closed candles available")
    closes = [r["close"] for r in rows]
    volumes = [r["volume"] for r in rows]
    sma = lambda n: sum(closes[-n:]) / n if len(closes) >= n else None
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])]
    gain, loss = wilder(gains, 14), wilder(losses, 14)
    rsi = None if gain is None else (50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss))
    fast, slow = ema(closes, 12), ema(closes, 26)
    macd_series = [a - b for a, b in zip(fast, slow) if a is not None and b is not None]
    macd = macd_series[-1] if macd_series else None
    signal = ema(macd_series, 9)[-1] if macd_series else None
    mid = sma(20)
    std = sqrt(sum((c - mid) ** 2 for c in closes[-20:]) / 20) if mid is not None else None
    ranges = [max(row["high"] - row["low"], abs(row["high"] - prev["close"]),
                  abs(row["low"] - prev["close"])) for prev, row in zip(rows, rows[1:])]
    volume_base = sum(volumes[-21:-1]) / 20 if len(rows) >= 21 else None
    gaps = sum(utc_time(b["time"]) - utc_time(a["time"]) != timedelta(minutes=unit) for a, b in zip(rows, rows[1:]))
    age = (now - utc_time(rows[-1]["time"]) - timedelta(minutes=unit)).total_seconds()
    return {"market": market, "unit_minutes": unit, "as_of": now.isoformat(),
            "closed_candles": len(rows), "latest_close": closes[-1],
            "indicators": {"sma20": mid, "sma50": sma(50), "ema12": fast[-1], "ema26": slow[-1],
                "rsi14": rsi, "macd": macd, "macd_signal": signal,
                "macd_histogram": macd - signal if signal is not None else None,
                "bollinger20": None if mid is None else {"lower": mid - 2 * std, "middle": mid, "upper": mid + 2 * std},
                "atr14": wilder(ranges, 14),
                "volume_ratio20": volumes[-1] / volume_base if volume_base else None},
            "quality": {"gap_count": gaps, "latest_close_age_seconds": age,
                        "stale": age > unit * 60 * 2,
                        "warnings": (["Fewer than 50 closed candles; some indicators unavailable"] if len(rows) < 50 else [])
                                    + (["Candle gaps: indicators use observed bars without filling"] if gaps else [])},
            "candles": rows}
