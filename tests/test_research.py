import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from coin.context import build_context, save_context
from coin.research import collect, import_evidence, parse_feed


class ResearchTests(unittest.TestCase):
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)

    def test_rss_filter_dedupe_and_provider_failure(self):
        xml = b'''<rss><channel>
          <item><title>Bitcoin rise</title><link>https://example.com/a</link><pubDate>Thu, 01 Jan 2026 12:00:00 GMT</pubDate></item>
          <item><title>Bitcoin duplicate</title><link>https://example.com/a</link><pubDate>Thu, 01 Jan 2026 12:00:00 GMT</pubDate></item>
          <item><title>Bitcoin old</title><link>https://example.com/b</link><pubDate>Thu, 01 Jan 2020 12:00:00 GMT</pubDate></item>
          <item><title>Bitcoin undated</title><link>https://example.com/c</link></item>
          <item><title>Other news</title><link>https://example.com/d</link><pubDate>Thu, 01 Jan 2026 12:00:00 GMT</pubDate></item>
        </channel></rss>'''
        config = '''[[feeds]]
name = "test"
url = "https://example.com/feed"
terms = ["bitcoin"]
[[feeds]]
name = "broken"
url = "https://example.com/broken"
'''
        def get(url):
            if "broken" in url:
                raise TimeoutError("timeout")
            return xml
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(config)
            result = collect(path, self.now, get)
        self.assertEqual(len(result["items"]), 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["providers"][1]["status"], "error")
        self.assertEqual(result["items"][0]["collected_at"], self.now.isoformat())

    def test_atom_and_dtd_rejection(self):
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Announcement</title>
          <link href="https://example.com/a"/><published>2026-01-01T00:00:00Z</published></entry></feed>'''
        rows = parse_feed(xml, "source", "https://example.com/feed", self.now.isoformat())
        self.assertEqual(rows[0]["published_at"], "2026-01-01T00:00:00+00:00")
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE rss><rss/>', "source", "url", "time")

    def test_sentiment_validation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text('[sentiment]\nenabled=true')
            data = {"data": [{"value": "50", "value_classification": "Neutral", "timestamp": "1767225600"}]}
            result = collect(path, self.now, lambda _: json.dumps(data).encode())
            self.assertEqual(result["items"][0]["value"], 50)
            data["data"][0]["value"] = "101"
            self.assertFalse(collect(path, self.now, lambda _: json.dumps(data).encode())["complete"])

    def test_manual_evidence_provenance(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "evidence.json"
            row = {"title": "Official update", "url": "https://example.com/update", "source": "Official",
                   "published_at": "2026-01-01T00:00:00Z", "collected_at": "2026-01-01T12:00:00Z"}
            path.write_text(json.dumps([row]))
            self.assertEqual(import_evidence(path, self.now)[0]["kind"], "manual_evidence")
            row["collected_at"] = "2027-01-01T00:00:00Z"
            path.write_text(json.dumps([row]))
            with self.assertRaises(ValueError):
                import_evidence(path, self.now)

    @patch("coin.context.analyze", return_value={"indicators": {}})
    @patch("coin.context.collect")
    def test_external_opt_in_and_archive(self, collect_mock, analyze_mock):
        client = Mock()
        client.candles.return_value = []
        result = build_context("KRW-BTC", client=client)
        collect_mock.assert_not_called()
        self.assertEqual(result["external"]["status"], "not_requested")
        with tempfile.TemporaryDirectory() as d:
            saved = save_context(result, d)
            self.assertEqual(json.loads(Path(saved).read_text())["market"], "KRW-BTC")
