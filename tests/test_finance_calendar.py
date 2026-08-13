import contextlib
import io
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
                """macro_events:
  - name: 美国CPI
    label: macro_rate_policy
    time_utc: 2026-08-12T12:30:00Z
    affects: [BTC, QQQ]
company_finance_reports:
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
                [
                    {"n": "美国CPI", "t": 1786537800000},
                    {"n": "Apple 财报", "t": 1785614400000},
                ],
            )

    def test_refresh_finance_calendar_writes_macro_events_and_keeps_earnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "finance_calendar.yaml"
            path.write_text(
                """company_finance_reports:
  - symbol: NVDA
    name: Nvidia 财报
    time_utc: 2026-08-26T20:00:00Z
""",
                encoding="utf-8",
            )
            old_file = proxy.FINANCE_CALENDAR_FILE
            old_auto = proxy.FINANCE_CALENDAR_AUTO_UPDATE
            old_bls = proxy._fetch_bls_macro_events
            old_fomc = proxy._fetch_fomc_macro_events
            old_nyfed = proxy._fetch_nyfed_macro_events
            try:
                proxy.FINANCE_CALENDAR_FILE = path
                proxy.FINANCE_CALENDAR_AUTO_UPDATE = True
                proxy._fetch_bls_macro_events = lambda: []
                proxy._fetch_nyfed_macro_events = lambda: [
                    {"name": "美国CPI", "label": "macro_rate_policy", "time_utc": "2026-08-12T12:30:00Z", "affects": ["BTC", "QQQ"]}
                ]
                proxy._fetch_fomc_macro_events = lambda: [
                    {"name": "FOMC 利率决议", "label": "macro_rate_policy", "time_utc": "2026-09-16T18:00:00Z", "affects": ["BTC", "QQQ"]}
                ]

                proxy.refresh_finance_calendar_on_startup()

                text = path.read_text(encoding="utf-8")
                self.assertIn("macro_events:", text)
                self.assertIn("美国CPI", text)
                self.assertIn("FOMC 利率决议", text)
                self.assertIn("Nvidia 财报", text)
            finally:
                proxy.FINANCE_CALENDAR_FILE = old_file
                proxy.FINANCE_CALENDAR_AUTO_UPDATE = old_auto
                proxy._fetch_bls_macro_events = old_bls
                proxy._fetch_fomc_macro_events = old_fomc
                proxy._fetch_nyfed_macro_events = old_nyfed

    def test_refresh_finance_calendar_uses_nyfed_before_bls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "finance_calendar.yaml"
            path.write_text("company_finance_reports:\n", encoding="utf-8")
            old_file = proxy.FINANCE_CALENDAR_FILE
            old_auto = proxy.FINANCE_CALENDAR_AUTO_UPDATE
            old_bls = proxy._fetch_bls_macro_events
            old_fomc = proxy._fetch_fomc_macro_events
            old_nyfed = proxy._fetch_nyfed_macro_events
            try:
                proxy.FINANCE_CALENDAR_FILE = path
                proxy.FINANCE_CALENDAR_AUTO_UPDATE = True
                proxy._fetch_fomc_macro_events = lambda: []
                proxy._fetch_nyfed_macro_events = lambda: [
                    {"name": "非农就业", "label": "macro_rate_policy", "time_utc": "2026-09-04T12:30:00Z", "affects": ["BTC", "QQQ"]}
                ]
                proxy._fetch_bls_macro_events = lambda: (_ for _ in ()).throw(AssertionError("BLS should not be called"))

                proxy.refresh_finance_calendar_on_startup()

                self.assertIn("非农就业", path.read_text(encoding="utf-8"))
            finally:
                proxy.FINANCE_CALENDAR_FILE = old_file
                proxy.FINANCE_CALENDAR_AUTO_UPDATE = old_auto
                proxy._fetch_bls_macro_events = old_bls
                proxy._fetch_fomc_macro_events = old_fomc
                proxy._fetch_nyfed_macro_events = old_nyfed

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

    def test_telegram_finance_news_record_drops_packet_fields_from_ai_result(self):
        record = proxy.telegram_finance_news_record(
            {
                "important": True,
                "summary": "美联储暗示降息",
                "type": "warning_message",
                "warning_id": "model-owned",
                "trader_name": "model",
            },
            chat_name="tradfi",
            msg_id=123,
            text="Fed cuts rates",
            timestamp="2026-07-07 14:00:00",
        )

        self.assertEqual(record["summary"], "美联储暗示降息")
        self.assertNotIn("type", record)
        self.assertNotIn("warning_id", record)
        self.assertNotIn("trader_name", record)

    def test_finance_ai_json_drops_packet_fields(self):
        result = proxy._ai_json('{"important":true,"summary":"ok","type":"warning_message","warning_id":"model-owned"}')

        self.assertEqual(result, {"important": True, "summary": "ok"})

    def test_finance_ai_text_fallback_builds_record_fields(self):
        result = proxy._ai_json("美联储释放降息信号，利好风险资产和 BTC。")

        self.assertEqual(result["summary"], "美联储释放降息信号，利好风险资产和 BTC。")
        self.assertTrue(result["important"])
        self.assertFalse(result["unrelated"])
        self.assertEqual(result["coins"], ["BTC"])
        self.assertEqual(result["btc_price"], "increase")

    def test_telegram_finance_news_record_marks_unrelated_messages(self):
        record = proxy.telegram_finance_news_record(
            {"important": False, "unrelated": True, "summary": "无关消息"},
            chat_name="tradfi",
            msg_id=124,
            text="local restaurant opens",
            timestamp="2026-07-07 14:01:00",
        )

        self.assertTrue(record["unrelated"])

    def test_telegram_finance_news_record_keeps_raw_text_for_dabing(self):
        record = proxy.telegram_finance_news_record(
            {"important": True, "unrelated": False, "summary": "降息", "reason": "macro"},
            chat_name="tradfi",
            msg_id=125,
            text="Fed cuts rates",
            timestamp="2026-07-07 14:02:00",
        )

        self.assertEqual(record["raw_text"], "Fed cuts rates")
        self.assertEqual(record["reason"], "macro")

    def test_udp_telegram_finance_news_is_stored_and_processed(self):
        sent = []
        original_ai = proxy._post_finance_ai
        original_send = proxy._send_receiver_packet
        original_items = list(proxy.TELEGRAM_FINANCE_ITEMS)
        original_processed = dict(proxy.TELEGRAM_FINANCE_PROCESSED)
        try:
            proxy.TELEGRAM_FINANCE_ITEMS.clear()
            proxy.TELEGRAM_FINANCE_PROCESSED.clear()
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
            proxy.TELEGRAM_FINANCE_PROCESSED.clear()
            proxy.TELEGRAM_FINANCE_PROCESSED.update(original_processed)

        self.assertTrue(result["sent"])
        self.assertEqual(sent[0]["warning_type"], "market_news")
        self.assertEqual(feed[0]["src"], "tradfi")
        self.assertEqual(feed[0]["timestamp"], "2026-07-07 14:00:00")
        self.assertEqual(feed[0]["title"], "Fed cuts rates")
        self.assertEqual(feed[0]["body"], "Fed cuts rates")

    def test_telegram_finance_news_logs_receive_and_ai_parse_success_without_details(self):
        original_ai = proxy._post_finance_ai
        original_send = proxy._send_receiver_packet
        original_items = list(proxy.TELEGRAM_FINANCE_ITEMS)
        original_processed = dict(proxy.TELEGRAM_FINANCE_PROCESSED)
        try:
            proxy.TELEGRAM_FINANCE_ITEMS.clear()
            proxy.TELEGRAM_FINANCE_PROCESSED.clear()
            proxy._post_finance_ai = lambda _text: {
                "important": True,
                "summary": "美联储暗示降息",
                "btc_price": "increase",
            }
            proxy._send_receiver_packet = lambda _packet: None
            out = io.StringIO()

            with contextlib.redirect_stdout(out):
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
            proxy._post_finance_ai = original_ai
            proxy._send_receiver_packet = original_send
            proxy.TELEGRAM_FINANCE_ITEMS[:] = original_items
            proxy.TELEGRAM_FINANCE_PROCESSED.clear()
            proxy.TELEGRAM_FINANCE_PROCESSED.update(original_processed)

        logs = out.getvalue()
        self.assertIn("[proxy] telegram finance msg received source=telegram chat=tradfi msg_id=123 text_len=14", logs)
        self.assertIn("[proxy] AI parsed telegram finance msg source=telegram chat=tradfi msg_id=123", logs)
        self.assertNotIn("Fed cuts rates", logs)
        self.assertNotIn("美联储暗示降息", logs)

    def test_duplicate_telegram_finance_packet_skips_ai(self):
        original_ai = proxy._post_finance_ai
        original_send = proxy._send_receiver_packet
        original_items = list(proxy.TELEGRAM_FINANCE_ITEMS)
        original_processed = dict(proxy.TELEGRAM_FINANCE_PROCESSED)
        calls = []
        try:
            proxy.TELEGRAM_FINANCE_ITEMS.clear()
            proxy.TELEGRAM_FINANCE_PROCESSED.clear()
            proxy._post_finance_ai = lambda text: calls.append(text) or {
                "important": True,
                "summary": "美联储暗示降息",
                "btc_price": "increase",
            }
            proxy._send_receiver_packet = lambda _packet: None
            payload = {
                "type": "telegram_finance_news",
                "source": "telegram",
                "chat_name": "tradfi",
                "msg_id": 123,
                "text": "Fed cuts rates",
                "timestamp": "2026-07-07 14:00:00",
            }

            first = proxy.ingest_telegram_finance_packet(dict(payload))
            second = proxy.ingest_telegram_finance_packet(dict(payload))
        finally:
            proxy._post_finance_ai = original_ai
            proxy._send_receiver_packet = original_send
            proxy.TELEGRAM_FINANCE_ITEMS[:] = original_items
            proxy.TELEGRAM_FINANCE_PROCESSED.clear()
            proxy.TELEGRAM_FINANCE_PROCESSED.update(original_processed)

        self.assertEqual(len(calls), 1)
        self.assertTrue(first["sent"])
        self.assertEqual(second, first)

    def test_udp_discord_finance_news_is_stored_and_processed(self):
        original_ai = proxy._post_finance_ai
        original_send = proxy._send_receiver_packet
        original_items = list(proxy.TELEGRAM_FINANCE_ITEMS)
        try:
            proxy.TELEGRAM_FINANCE_ITEMS.clear()
            proxy._post_finance_ai = lambda _text: {"important": False, "summary": "ok"}
            proxy._send_receiver_packet = lambda _packet: None

            result = proxy.ingest_telegram_finance_packet(
                {
                    "type": "telegram_finance_news",
                    "source": "discord",
                    "chat_name": "Guild#macro",
                    "msg_id": 456,
                    "text": "Discord macro update",
                    "timestamp": "2026-07-07 15:00:00",
                }
            )

            feed = proxy.telegram_finance_feed_items()
        finally:
            proxy._post_finance_ai = original_ai
            proxy._send_receiver_packet = original_send
            proxy.TELEGRAM_FINANCE_ITEMS[:] = original_items

        self.assertTrue(result["ok"])
        self.assertEqual(feed[0]["src"], "Guild#macro")
        self.assertEqual(feed[0]["body"], "Discord macro update")

    def test_important_person_name_forces_important_finance_news(self):
        sent = []
        original_ai = proxy._post_finance_ai
        original_send = proxy._send_receiver_packet
        original_items = list(proxy.TELEGRAM_FINANCE_ITEMS)
        try:
            proxy.TELEGRAM_FINANCE_ITEMS.clear()
            proxy._post_finance_ai = lambda _text: {
                "important": False,
                "summary": "Trump comments on Nvidia exports",
                "btc_price": "decrease",
                "us_tech_stocks": "decrease",
            }
            proxy._send_receiver_packet = sent.append

            result = proxy.ingest_telegram_finance_packet(
                {
                    "type": "telegram_finance_news",
                    "source": "telegram",
                    "chat_name": "tradfi",
                    "msg_id": 789,
                    "text": "Trump comments on Nvidia exports",
                    "timestamp": "2026-07-07 16:00:00",
                }
            )
        finally:
            proxy._post_finance_ai = original_ai
            proxy._send_receiver_packet = original_send
            proxy.TELEGRAM_FINANCE_ITEMS[:] = original_items

        self.assertTrue(result["important"])
        self.assertTrue(result["sent"])
        self.assertEqual(sent[0]["warning_type"], "market_news")

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
        self.assertEqual(rows[0]["ai"]["raw_text"], "Fed cuts rates")
        self.assertTrue(rows[0]["ai"]["important"])

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

    def test_telegram_finance_prompt_filters_only_and_saves_full_parse_prompt(self):
        root = Path(__file__).resolve().parents[1]
        prompt = (root / "prompts" / "telegram_finance_prompt.txt").read_text(encoding="utf-8")
        full_prompt = (root / "prompts" / "telegram_finance_prompt.full_parse.txt").read_text(encoding="utf-8")

        for field in ('"important"', '"unrelated"', '"summary"', '"reason"'):
            self.assertIn(field, prompt)

        for old_field in (
            '"unrelated"',
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
            self.assertIn(old_field, full_prompt)

        self.assertNotIn('"news_implication"', prompt)
        self.assertIn("Do not retry", prompt)
        self.assertIn("raw message", prompt)


if __name__ == "__main__":
    unittest.main()
