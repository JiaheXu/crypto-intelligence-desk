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

    def test_telegram_finance_news_record_keeps_affected_stocks(self):
        ai = proxy._ai_json('{"important":true,"affected_stocks":["ASML","NVDA","SK Hynix"],"positive_affected_stocks":["NVDA"],"negative_affected_stocks":["SKHYNIX"],"positive_affected_sectors":["AI"],"negative_affected_sectors":["memory"],"type":"warning_message"}')

        record = proxy.telegram_finance_news_record(
            ai,
            chat_name="tradfi",
            msg_id=125,
            text="China DUV machines challenge ASML",
            timestamp="2026-07-07 14:02:00",
        )

        self.assertEqual(record["affected_stocks"], ["ASML", "NVDA", "SK Hynix"])
        self.assertEqual(record["positive_affected_stocks"], ["NVDA"])
        self.assertEqual(record["negative_affected_stocks"], ["SKHYNIX"])
        self.assertEqual(record["positive_affected_sectors"], ["AI"])
        self.assertEqual(record["negative_affected_sectors"], ["memory"])

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

    def test_telegram_finance_news_logs_compact_receive_and_ai_parse_summary(self):
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
                "conf": 82,
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
                        "text": "Fed cuts rates and signals more cuts next quarter",
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
        self.assertIn("[proxy] 1 msg from \033[32mtelegram\033[0m chat=\033[34mtradfi\033[0m", logs)
        self.assertIn("[proxy] AI parsing: \033[32mimportant\033[0m / \033[32mincrease\033[0m / \033[32m82\033[0m", logs)
        self.assertIn("[proxy] raw text: Fed cuts rates and signals mor", logs)
        self.assertNotIn("next quarter", logs)
        self.assertNotIn("美联储暗示降息", logs)

    def test_ai_parsing_log_includes_affected_stocks(self):
        log = proxy._ai_parsing_log(
            {
                "important": True,
                "btc_price": "neutral",
                "conf": 70,
                "affected_stocks": ["NVDA", "MU"],
                "affected_sectors": ["AI infrastructure", "memory"],
                "positive_affected_stocks": ["NVDA"],
                "negative_affected_stocks": ["MU"],
                "positive_affected_sectors": ["AI infrastructure"],
                "negative_affected_sectors": ["memory"],
            }
        )

        self.assertIn("stocks=NVDA,MU", log)
        self.assertIn("sectors=AI infrastructure,memory", log)
        self.assertIn("positively affected sector: \033[32mAI infrastructure\033[0m, stocks: \033[32mNVDA\033[0m", log)
        self.assertIn("negatively affected sector: \033[31mmemory\033[0m, stocks: \033[31mMU\033[0m", log)

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

    def test_incoming_telegram_finance_log_keeps_duplicate_packets(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_ai = proxy._post_finance_ai
            original_send = proxy._send_receiver_packet
            original_items = list(proxy.TELEGRAM_FINANCE_ITEMS)
            original_processed = dict(proxy.TELEGRAM_FINANCE_PROCESSED)
            original_incoming_dir = proxy.TELEGRAM_FINANCE_INCOMING_LOG_DIR
            try:
                proxy.TELEGRAM_FINANCE_INCOMING_LOG_DIR = Path(tmp)
                proxy.TELEGRAM_FINANCE_ITEMS.clear()
                proxy.TELEGRAM_FINANCE_PROCESSED.clear()
                if hasattr(proxy, "TELEGRAM_FINANCE_RECENT_DEDUPE"):
                    proxy.TELEGRAM_FINANCE_RECENT_DEDUPE.clear()
                proxy._post_finance_ai = lambda _text: {"important": False, "summary": "ok"}
                proxy._send_receiver_packet = lambda _packet: None
                payload = {
                    "type": "telegram_finance_news",
                    "source": "telegram",
                    "chat_name": "tradfi",
                    "msg_id": 123,
                    "text": "Fed cuts rates",
                    "timestamp": "2026-07-07 14:00:00",
                }

                proxy.ingest_telegram_finance_packet(dict(payload))
                proxy.ingest_telegram_finance_packet(dict(payload))

                lines = (Path(tmp) / "2026-07-07.jsonl").read_text(encoding="utf-8").splitlines()
            finally:
                proxy._post_finance_ai = original_ai
                proxy._send_receiver_packet = original_send
                proxy.TELEGRAM_FINANCE_ITEMS[:] = original_items
                proxy.TELEGRAM_FINANCE_PROCESSED.clear()
                proxy.TELEGRAM_FINANCE_PROCESSED.update(original_processed)
                if hasattr(proxy, "TELEGRAM_FINANCE_RECENT_DEDUPE"):
                    proxy.TELEGRAM_FINANCE_RECENT_DEDUPE.clear()
                proxy.TELEGRAM_FINANCE_INCOMING_LOG_DIR = original_incoming_dir

        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(line)["text"] for line in lines], ["Fed cuts rates", "Fed cuts rates"])

    def test_duplicate_telegram_finance_text_from_other_channel_skips_ai(self):
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

            first = proxy.ingest_telegram_finance_packet(
                {
                    "type": "telegram_finance_news",
                    "source": "telegram",
                    "chat_name": "tradfi-a",
                    "msg_id": 123,
                    "text": "Fed cuts rates",
                    "timestamp": "2026-07-07 14:00:00",
                }
            )
            second = proxy.ingest_telegram_finance_packet(
                {
                    "type": "telegram_finance_news",
                    "source": "telegram",
                    "chat_name": "tradfi-b",
                    "msg_id": 456,
                    "text": "  Fed   cuts rates  ",
                    "timestamp": "2026-07-07 14:01:00",
                }
            )
        finally:
            proxy._post_finance_ai = original_ai
            proxy._send_receiver_packet = original_send
            proxy.TELEGRAM_FINANCE_ITEMS[:] = original_items
            proxy.TELEGRAM_FINANCE_PROCESSED.clear()
            proxy.TELEGRAM_FINANCE_PROCESSED.update(original_processed)

        self.assertEqual(len(calls), 1)
        self.assertEqual(second, first)

    def test_similar_telegram_finance_text_over_threshold_skips_ai(self):
        original_ai = proxy._post_finance_ai
        original_send = proxy._send_receiver_packet
        original_items = list(proxy.TELEGRAM_FINANCE_ITEMS)
        original_processed = dict(proxy.TELEGRAM_FINANCE_PROCESSED)
        original_threshold = getattr(proxy, "TELEGRAM_FINANCE_DEDUPE_SIMILARITY_THRESHOLD", None)
        calls = []
        try:
            proxy.TELEGRAM_FINANCE_ITEMS.clear()
            proxy.TELEGRAM_FINANCE_PROCESSED.clear()
            if hasattr(proxy, "TELEGRAM_FINANCE_RECENT_DEDUPE"):
                proxy.TELEGRAM_FINANCE_RECENT_DEDUPE.clear()
            proxy.TELEGRAM_FINANCE_DEDUPE_SIMILARITY_THRESHOLD = 0.85
            proxy._post_finance_ai = lambda text: calls.append(text) or {
                "important": True,
                "summary": "谷歌AI重组",
                "btc_price": "increase",
            }
            proxy._send_receiver_packet = lambda _packet: None

            first = proxy.ingest_telegram_finance_packet(
                {
                    "type": "telegram_finance_news",
                    "source": "telegram",
                    "chat_name": "tradfi_cn",
                    "msg_id": 123,
                    "text": "消息人士称 谷歌高管讨论人工智能部门重组事宜 $GOOGL",
                    "timestamp": "2026-08-13 01:33:39",
                }
            )
            second = proxy.ingest_telegram_finance_packet(
                {
                    "type": "telegram_finance_news",
                    "source": "telegram",
                    "chat_name": "章鱼哥新闻流 OctoSignal",
                    "msg_id": 402,
                    "text": "消息人士称 谷歌高管讨论人工智能部门重组事宜 $GOOGL ──────────── 章鱼哥新闻流 · OCTOSIGNAL",
                    "timestamp": "2026-08-13 01:33:58",
                }
            )
        finally:
            proxy._post_finance_ai = original_ai
            proxy._send_receiver_packet = original_send
            proxy.TELEGRAM_FINANCE_ITEMS[:] = original_items
            proxy.TELEGRAM_FINANCE_PROCESSED.clear()
            proxy.TELEGRAM_FINANCE_PROCESSED.update(original_processed)
            if hasattr(proxy, "TELEGRAM_FINANCE_RECENT_DEDUPE"):
                proxy.TELEGRAM_FINANCE_RECENT_DEDUPE.clear()
            if original_threshold is not None:
                proxy.TELEGRAM_FINANCE_DEDUPE_SIMILARITY_THRESHOLD = original_threshold

        self.assertEqual(len(calls), 1)
        self.assertEqual(second, first)

    def test_similar_telegram_finance_text_with_different_numbers_does_not_skip_ai(self):
        original_ai = proxy._post_finance_ai
        original_send = proxy._send_receiver_packet
        original_items = list(proxy.TELEGRAM_FINANCE_ITEMS)
        original_processed = dict(proxy.TELEGRAM_FINANCE_PROCESSED)
        original_threshold = getattr(proxy, "TELEGRAM_FINANCE_DEDUPE_SIMILARITY_THRESHOLD", None)
        calls = []
        try:
            proxy.TELEGRAM_FINANCE_ITEMS.clear()
            proxy.TELEGRAM_FINANCE_PROCESSED.clear()
            if hasattr(proxy, "TELEGRAM_FINANCE_RECENT_DEDUPE"):
                proxy.TELEGRAM_FINANCE_RECENT_DEDUPE.clear()
            proxy.TELEGRAM_FINANCE_DEDUPE_SIMILARITY_THRESHOLD = 0.85
            proxy._post_finance_ai = lambda text: calls.append(text) or {
                "important": False,
                "summary": "黄金ETF持仓变化",
            }
            proxy._send_receiver_packet = lambda _packet: None

            proxy.ingest_telegram_finance_packet(
                {
                    "type": "telegram_finance_news",
                    "source": "telegram",
                    "chat_name": "金十数据 闪电资讯",
                    "msg_id": 123,
                    "text": "全球最大黄金ETF持仓较上日增加3.139吨 当前持仓量为1025.811吨",
                    "timestamp": "2026-08-13 10:00:00",
                }
            )
            proxy.ingest_telegram_finance_packet(
                {
                    "type": "telegram_finance_news",
                    "source": "telegram",
                    "chat_name": "金十数据 闪电资讯",
                    "msg_id": 456,
                    "text": "全球最大黄金ETF持仓较上日减少2.568吨 当前持仓量为1023.243吨",
                    "timestamp": "2026-08-13 10:01:00",
                }
            )
        finally:
            proxy._post_finance_ai = original_ai
            proxy._send_receiver_packet = original_send
            proxy.TELEGRAM_FINANCE_ITEMS[:] = original_items
            proxy.TELEGRAM_FINANCE_PROCESSED.clear()
            proxy.TELEGRAM_FINANCE_PROCESSED.update(original_processed)
            if hasattr(proxy, "TELEGRAM_FINANCE_RECENT_DEDUPE"):
                proxy.TELEGRAM_FINANCE_RECENT_DEDUPE.clear()
            if original_threshold is not None:
                proxy.TELEGRAM_FINANCE_DEDUPE_SIMILARITY_THRESHOLD = original_threshold

        self.assertEqual(len(calls), 2)

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
        original_watchlist = proxy.SYMBOL_WATCHLIST
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
            proxy.SYMBOL_WATCHLIST = [{"symbol": "QQQ", "name": "Invesco QQQ Trust"}]
            proxy.urllib.request.urlopen = fake_urlopen
            proxy.time.sleep = lambda _seconds: None

            result = proxy._post_finance_ai("hello")
        finally:
            proxy.FINANCE_AI_KEY = original_key
            proxy.SYMBOL_WATCHLIST = original_watchlist
            proxy.urllib.request.urlopen = original_urlopen
            proxy.time.sleep = original_sleep

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(len(calls), 2)

    def test_finance_ai_prompt_includes_symbol_watchlist(self):
        original_key = proxy.FINANCE_AI_KEY
        original_urlopen = proxy.urllib.request.urlopen
        original_watchlist = proxy.SYMBOL_WATCHLIST
        bodies = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"choices":[{"message":{"content":"{\\"important\\":true,\\"summary\\":\\"ok\\"}"}}]}'

        def fake_urlopen(req, timeout=None):
            bodies.append(json.loads(req.data.decode("utf-8")))
            return Response()

        try:
            proxy.FINANCE_AI_KEY = "test-key"
            proxy.SYMBOL_WATCHLIST = [
                {"symbol": "BTC", "name": "Bitcoin"},
                {"symbol": "QQQ", "name": "Invesco QQQ Trust"},
            ]
            proxy.urllib.request.urlopen = fake_urlopen

            proxy._post_finance_ai("news")
        finally:
            proxy.FINANCE_AI_KEY = original_key
            proxy.SYMBOL_WATCHLIST = original_watchlist
            proxy.urllib.request.urlopen = original_urlopen

        prompt = bodies[0]["messages"][1]["content"]
        self.assertIn("- BTC: Bitcoin", prompt)
        self.assertIn("- QQQ: Invesco QQQ Trust", prompt)
        self.assertNotIn("{symbol_watchlist}", prompt)

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
        self.assertIn('"affected_stocks":["受影响股票/ETF/商品符号,最多8个"]', source)
        self.assertIn('"affected_sectors":["受影响板块/产业链,最多5个"]', source)
        self.assertIn('"positive_affected_stocks":["正面影响股票/ETF/商品符号,最多8个"]', source)
        self.assertIn('"negative_affected_stocks":["负面影响股票/ETF/商品符号,最多8个"]', source)
        self.assertIn("a.affected_stocks=", source)
        self.assertIn("a.affected_sectors=", source)
        self.assertIn("positively affected sector:", source)
        self.assertIn("negatively affected sector:", source)
        self.assertIn("class=\"pos\"", source)
        self.assertIn("class=\"neg\"", source)
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
