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

    def test_loads_recent_telegram_finance_archive_into_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_dir = proxy.TELEGRAM_FINANCE_ARCHIVE_DIR
            original_items = list(proxy.TELEGRAM_FINANCE_ITEMS)
            try:
                proxy.TELEGRAM_FINANCE_ARCHIVE_DIR = Path(tmp)
                proxy.TELEGRAM_FINANCE_ITEMS.clear()
                older = Path(tmp) / "2026-07-06.jsonl"
                newer = Path(tmp) / "2026-07-07.jsonl"
                older.write_text(
                    "\n".join(
                        json.dumps(
                            {
                                "timestamp": f"2026-07-06 10:0{i}:00",
                                "chat_name": "tradfi",
                                "msg_id": f"old-{i}",
                                "text": f"old msg {i}",
                            },
                            ensure_ascii=False,
                        )
                        for i in range(3)
                    )
                    + "\n",
                    encoding="utf-8",
                )
                newer.write_text(
                    "\n".join(
                        json.dumps(
                            {
                                "timestamp": f"2026-07-07 10:0{i}:00",
                                "chat_name": "tradfi",
                                "msg_id": f"new-{i}",
                                "text": f"new msg {i}",
                            },
                            ensure_ascii=False,
                        )
                        for i in range(4)
                    )
                    + "\n",
                    encoding="utf-8",
                )

                loaded = proxy.load_recent_telegram_finance_archive(limit=5)
                feed = proxy.telegram_finance_feed_items()
            finally:
                proxy.TELEGRAM_FINANCE_ARCHIVE_DIR = original_dir
                proxy.TELEGRAM_FINANCE_ITEMS[:] = original_items

        self.assertEqual(loaded, 5)
        self.assertEqual([row["body"] for row in feed], ["new msg 3", "new msg 2", "new msg 1", "new msg 0", "old msg 2"])

    def test_finance_ai_retries_invalid_json_response(self):
        original_key = proxy.FINANCE_AI_KEY
        original_urlopen = proxy.urllib.request.urlopen
        original_sleep = proxy.time.sleep
        calls = []

        class Response:
            def __init__(self, content):
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps({"choices": [{"message": {"content": self.content}}]}).encode("utf-8")

        def fake_urlopen(_req, timeout):
            calls.append(timeout)
            return Response("not json" if len(calls) == 1 else '{"important":true,"summary":"ok"}')

        try:
            proxy.FINANCE_AI_KEY = "test-key"
            proxy.urllib.request.urlopen = fake_urlopen
            proxy.time.sleep = lambda _seconds: None

            result = proxy._post_finance_ai("hello")
        finally:
            proxy.FINANCE_AI_KEY = original_key
            proxy.urllib.request.urlopen = original_urlopen
            proxy.time.sleep = original_sleep

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(len(calls), 2)

    def test_finance_ai_stops_after_json_retry_limit(self):
        original_key = proxy.FINANCE_AI_KEY
        original_urlopen = proxy.urllib.request.urlopen
        original_sleep = proxy.time.sleep
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps({"choices": [{"message": {"content": "not json"}}]}).encode("utf-8")

        def fake_urlopen(_req, timeout):
            calls.append(1)
            return Response()

        try:
            proxy.FINANCE_AI_KEY = "test-key"
            proxy.urllib.request.urlopen = fake_urlopen
            proxy.time.sleep = lambda _seconds: None

            with self.assertRaisesRegex(RuntimeError, "自动重试 3 次后仍未返回有效 JSON"):
                proxy._post_finance_ai("hello")
        finally:
            proxy.FINANCE_AI_KEY = original_key
            proxy.urllib.request.urlopen = original_urlopen
            proxy.time.sleep = original_sleep

        self.assertEqual(len(calls), 4)

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
