"""Read-only Bithumb public REST API adapter (no credentials required)."""
import json
import re
import time
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class APIError(RuntimeError):
    pass


def validate_market(market: str) -> str:
    if not re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+", market):
        raise ValueError("Market must look like KRW-BTC")
    return market


class BithumbPublicClient:
    BASE_URL = "https://api.bithumb.com"

    def __init__(self, timeout=10, retries=2, interval=0.2):
        self.timeout = timeout
        self.retries = retries
        self.interval = interval
        self._last_request = 0.0

    def _get(self, path, params):
        url = self.BASE_URL + path + "?" + urlencode(params)
        for attempt in range(self.retries + 1):
            time.sleep(max(0, self.interval - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            try:
                request = Request(url, headers={"Accept": "application/json", "User-Agent": "coin-agents/0.1"})
                with urlopen(request, timeout=self.timeout) as response:
                    result = json.load(response, parse_float=Decimal)
                if not isinstance(result, list):
                    raise APIError("Unexpected API response; expected a list")
                return result
            except HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    raise APIError(f"Bithumb HTTP {exc.code}: {exc.read(2048).decode(errors='replace')}") from exc
                error = f"Bithumb HTTP {exc.code}"
                retry_after = exc.headers.get("Retry-After", "")
                delay = min(float(retry_after), 30) if retry_after.isdigit() else 2 ** attempt
            except (URLError, TimeoutError, OSError) as exc:
                error, delay = f"Bithumb connection failed: {exc}", 2 ** attempt
            except (ValueError, TypeError) as exc:
                raise APIError("Malformed Bithumb JSON response") from exc
            if attempt < self.retries:
                time.sleep(delay)
        raise APIError(error)

    def markets(self):
        return self._get("/v1/market/all", {"isDetails": "true"})

    def ticker(self, markets):
        return self._get("/v1/ticker", {"markets": ",".join(validate_market(m) for m in markets)})

    def orderbook(self, market):
        rows = self._get("/v1/orderbook", {"markets": validate_market(market)})
        if len(rows) != 1 or rows[0].get("market") != market:
            raise APIError("Missing or mismatched orderbook")
        return rows[0]

    def candles(self, market, unit=1, count=100, to=None):
        if unit not in (1, 3, 5, 10, 15, 30, 60, 240) or not 1 <= count <= 200:
            raise ValueError("Invalid candle unit or count (1..200)")
        params = {"market": validate_market(market), "count": count}
        if to:
            params["to"] = to  # API expects KST; excludes the specified candle.
        return self._get(f"/v1/candles/minutes/{unit}", params)
