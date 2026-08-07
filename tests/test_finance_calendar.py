import json
import tempfile
import unittest
from pathlib import Path

import proxy


class FinanceCalendarTests(unittest.TestCase):
    def test_loads_company_finance_reports_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "finance_calendar.yaml"
            path.write_text(
                """company_finance_reports:
  - symbol: AAPL
    name: Apple 财报
    time_utc: 2026-08-01T20:00:00Z
  - TSLA
  - symbol: MSFT
    time_utc:
""",
                encoding="utf-8",
            )

            self.assertEqual(
                proxy.load_finance_calendar_events(path),
                [{"n": "Apple 财报", "t": 1785614400000}],
            )

    def test_telegram_finance_news_builds_receiver_warning_packet(self):
        record = proxy.telegram_finance_news_record(
            {
                "important": True,
                "summary": "美联储暗示降息",
                "btc_price": "increase",
                "us_tech_stocks": "increase",
                "korean_tech_stocks": "increase",
            },
            chat_name="tradfi",
            msg_id=123,
            text="Fed cuts rates",
            timestamp="2026-07-07 14:00:00",
        )

        packet = proxy.build_telegram_finance_warning_packet(record)

        self.assertEqual(packet["type"], "warning_message")
        self.assertEqual(packet["warning_type"], "market_news")
        self.assertEqual(packet["warning_id"], "market_news:telegram:tradfi:123")
        self.assertEqual(packet["trader_name"], "tradfi")
        self.assertEqual(packet["price_direction"], "increase")
        self.assertIn("BTC:increase | 美股:increase | 韩股:increase", packet["message"])

    def test_udp_telegram_finance_news_is_stored_and_processed(self):
        sent = []
        original_ai = proxy._post_finance_ai
        original_send = proxy._send_receiver_packet
        original_items = list(proxy.TELEGRAM_FINANCE_ITEMS)
        try:
            proxy.TELEGRAM_FINANCE_ITEMS.clear()
            proxy._post_finance_ai = lambda _text: {
                "important": True,
                "summary": "美联储暗示降息",
                "btc_price": "increase",
                "us_tech_stocks": "increase",
                "korean_tech_stocks": "increase",
            }
            proxy._send_receiver_packet = sent.append

            result = proxy.ingest_telegram_finance_packet(
                {
                    "type": "telegram_finance_news",
                    "source": "telegram",
                    "chat_name": "tradfi",
                    "msg_id": 123,
                    "text": "Fed cuts rates",
                    "timestamp": "2026-07-07 14:00:00",
                }
            )

            feed = proxy.telegram_finance_feed_items()
        finally:
            proxy._post_finance_ai = original_ai
            proxy._send_receiver_packet = original_send
            proxy.TELEGRAM_FINANCE_ITEMS[:] = original_items

        self.assertTrue(result["sent"])
        self.assertEqual(sent[0]["warning_type"], "market_news")
        self.assertEqual(feed[0]["src"], "tradfi")
        self.assertEqual(feed[0]["timestamp"], "2026-07-07 14:00:00")
        self.assertEqual(feed[0]["title"], "Fed cuts rates")
        self.assertEqual(feed[0]["body"], "Fed cuts rates")

    def test_telegram_finance_news_archives_one_jsonl_file_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_dir = proxy.TELEGRAM_FINANCE_ARCHIVE_DIR
            original_ai = proxy._post_finance_ai
            original_send = proxy._send_receiver_packet
            original_items = list(proxy.TELEGRAM_FINANCE_ITEMS)
            try:
                proxy.TELEGRAM_FINANCE_ARCHIVE_DIR = Path(tmp)
                proxy.TELEGRAM_FINANCE_ITEMS.clear()
                proxy._post_finance_ai = lambda _text: {
                    "important": True,
                    "summary": "美联储暗示降息",
                    "direction": "利好",
                    "st": "正面",
                    "lt": "正面",
                    "level": 4,
                    "conf": 90,
                    "coins": ["BTC"],
                    "cat": "宏观",
                    "news_label": ["macro_rate_policy"],
                    "news_point": "Fed cuts rates",
                    "news_implication": "Risk assets may rise",
                    "btc_price": "increase",
                    "us_tech_stocks": "increase",
                    "korean_tech_stocks": "increase",
                    "action": "long",
                    "bias": "bullish",
                    "reason_tags": ["macro"],
                    "event_timing": "live",
                    "priced_in": False,
                    "binary_event_risk": False,
                    "gap": "none",
                    "why": "Lower rates support risk assets",
                    "reverse": "Inflation rebounds",
                }
                proxy._send_receiver_packet = lambda _packet: None

                proxy.ingest_telegram_finance_packet(
                    {
                        "type": "telegram_finance_news",
                        "source": "telegram",
                        "chat_name": "tradfi",
                        "msg_id": 123,
                        "text": "Fed cuts rates",
                        "timestamp": "2026-07-07 14:00:00",
                    }
                )
            finally:
                proxy.TELEGRAM_FINANCE_ARCHIVE_DIR = original_dir
                proxy._post_finance_ai = original_ai
                proxy._send_receiver_packet = original_send
                proxy.TELEGRAM_FINANCE_ITEMS[:] = original_items

            path = Path(tmp) / "2026-07-07.jsonl"
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chat_name"], "tradfi")
        self.assertEqual(rows[0]["msg_id"], 123)
        self.assertEqual(rows[0]["text"], "Fed cuts rates")
        self.assertEqual(rows[0]["ai"]["news_label"], ["macro_rate_policy"])
        self.assertEqual(rows[0]["ai"]["btc_price"], "increase")

    def test_page_forces_telegram_as_only_source(self):
        source = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")

        self.assertIn("srcOn:{telegram:true}", source)
        self.assertIn("S.srcOn={telegram:true};S.srcUrl={};", source)
        self.assertIn("const ACTIVE_SOURCE_KEYS=['telegram'];", source)
        self.assertIn("const keys=ACTIVE_SOURCE_KEYS;", source)
        self.assertNotIn('class="sSrc"', source)
        self.assertNotIn('class="sUrl"', source)
        self.assertIn('"priced_in":true或false,"why"', source)
        self.assertNotIn('"news_label":["macro_rate_policy"]', source)
        self.assertNotIn("a.news_label=normList(a.news_label,4);", source)
        self.assertNotIn("事件 · ${esc(a.news_point)}", source)
        self.assertNotIn("影响 · ${esc(a.news_implication)}", source)

    def test_telegram_finance_prompt_loads_from_file(self):
        path = Path(__file__).resolve().parents[1] / "prompts" / "telegram_finance_prompt.txt"

        self.assertEqual(proxy.TELEGRAM_FINANCE_PROMPT, path.read_text(encoding="utf-8").strip())

    def test_telegram_finance_prompt_uses_unified_non_duplicated_info_schema(self):
        root = Path(__file__).resolve().parents[1]
        prompt = (root / "prompts" / "telegram_finance_prompt.txt").read_text(encoding="utf-8")

        for field in (
            '"direction"',
            '"st"',
            '"lt"',
            '"level"',
            '"conf"',
            '"coins"',
            '"cat"',
            '"news_label"',
            '"news_point"',
            '"news_implication"',
            '"action"',
            '"bias"',
            '"reason_tags"',
            '"event_timing"',
            '"priced_in"',
            '"binary_event_risk"',
            '"gap"',
            '"why"',
            '"reverse"',
        ):
            self.assertIn(field, prompt)

        for duplicate in ('"symbols"', '"markets"', '"cross_asset_effect"', '"trading_action"'):
            self.assertNotIn(duplicate, prompt)


if __name__ == "__main__":
    unittest.main()
