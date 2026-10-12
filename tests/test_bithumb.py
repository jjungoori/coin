import io
import unittest
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

from coin.bithumb import APIError, BithumbPublicClient


class ClientTests(unittest.TestCase):
    @patch("coin.bithumb.urlopen")
    def test_get_query_and_decimal(self, opened):
        opened.return_value.__enter__.return_value = io.StringIO('[{"trade_price": 1.25}]')
        client = BithumbPublicClient(interval=0)
        self.assertEqual(client.ticker(["KRW-BTC"])[0]["trade_price"], Decimal("1.25"))
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://api.bithumb.com/v1/ticker?markets=KRW-BTC")

    @patch("coin.bithumb.time.sleep")
    @patch("coin.bithumb.urlopen")
    def test_retry_429_and_stop_at_budget(self, opened, sleep):
        opened.side_effect = HTTPError("url", 429, "rate limit", {}, io.BytesIO())
        with self.assertRaises(APIError):
            BithumbPublicClient(interval=0).markets()
        self.assertEqual(opened.call_count, 3)

    @patch("coin.bithumb.urlopen")
    def test_invalid_input_never_calls_api(self, opened):
        client = BithumbPublicClient()
        with self.assertRaises(ValueError):
            client.candles("KRW-BTC", count=201)
        with self.assertRaises(ValueError):
            client.orderbook("../orders")
        opened.assert_not_called()
