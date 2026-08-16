#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""
币圈新闻监控台 · 本地代理服务器
- 仅监听 127.0.0.1(只有你自己的电脑能访问)
- 职责1: 托管 index.html 页面  ->  http://127.0.0.1:8899
- 职责2: 转发新闻源 / AI 接口请求,绕开浏览器跨域限制
- 纯 Python 标准库实现,无需安装任何第三方包
"""
import http.server
import socketserver
import urllib.request
import urllib.error
import urllib.parse
import ssl
import os
import re
import difflib
import sys
import time
import gzip
import io
import ipaddress
import socket
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    PORT = int(os.environ.get("CID_PORT", "8899"))
    if not 1024 <= PORT <= 65535:
        raise ValueError
except ValueError:
    print("[错误] CID_PORT 必须是 1024 到 65535 之间的整数。")
    sys.exit(2)
BASE = os.path.dirname(os.path.abspath(__file__))
FINANCE_CALENDAR_FILE = Path(os.environ.get(
    "FINANCE_CALENDAR_FILE",
    os.path.join(BASE, "..", "state", "finance_calendar.yaml"),
)).expanduser()
FINANCE_CALENDAR_FALLBACK_FILE = Path(os.path.join(BASE, "finance_calendar.yaml")).expanduser()
SYMBOL_WATCHLIST_FILE = Path(os.environ.get(
    "SYMBOL_WATCHLIST_FILE",
    os.path.join(BASE, "symbol_watchlist.yaml"),
)).expanduser()
FINANCE_CALENDAR_AUTO_UPDATE = os.environ.get("FINANCE_CALENDAR_AUTO_UPDATE", "1").strip().lower() in {"1", "true", "yes", "on"}
BLS_ICS_URL = os.environ.get("BLS_CALENDAR_ICS_URL", "https://www.bls.gov/schedule/news_release/bls.ics")
FOMC_CALENDAR_URL = os.environ.get("FOMC_CALENDAR_URL", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
NYFED_CALENDAR_URL_TEMPLATE = os.environ.get("NYFED_CALENDAR_URL_TEMPLATE", "https://www.newyorkfed.org/research/calendars/i-{month}{year}.html")
TELEGRAM_FINANCE_ARCHIVE_DIR = Path(os.environ.get(
    "TELEGRAM_FINANCE_ARCHIVE_DIR",
    os.path.join(BASE, "..", "state", "telegram_finance_news"),
)).expanduser()
TELEGRAM_FINANCE_INCOMING_LOG_DIR = Path(os.environ.get(
    "TELEGRAM_FINANCE_INCOMING_LOG_DIR",
    os.path.join(BASE, "..", "state", "telegram_finance_incoming"),
)).expanduser()
MAX_REQUEST_BODY = 2 * 1024 * 1024
MAX_RESPONSE_BODY = 12 * 1024 * 1024
TELEGRAM_FINANCE_TTL_MS = 5 * 60 * 1000
TELEGRAM_FINANCE_DEDUPE_SIMILARITY_THRESHOLD = float(os.environ.get("TELEGRAM_FINANCE_DEDUPE_SIMILARITY_THRESHOLD", "0.90") or 0.90)
TELEGRAM_FINANCE_DEDUPE_WINDOW_SECONDS = int(os.environ.get("TELEGRAM_FINANCE_DEDUPE_WINDOW_SECONDS", "14400") or 14400)
TELEGRAM_FINANCE_DEDUPE_RECENT_LIMIT = int(os.environ.get("TELEGRAM_FINANCE_DEDUPE_RECENT_LIMIT", "200") or 200)
TELEGRAM_FINANCE_UDP_HOST = os.environ.get("CID_TELEGRAM_FINANCE_UDP_HOST", "127.0.0.1").strip() or "127.0.0.1"
TELEGRAM_FINANCE_UDP_PORT = int(os.environ.get("CID_TELEGRAM_FINANCE_UDP_PORT", os.environ.get("FINANCE_NEWS_PROXY_UDP_PORT", "5201")) or 5201)
RECEIVER_UDP_HOST = os.environ.get("CID_RECEIVER_UDP_HOST", "127.0.0.1").strip() or "127.0.0.1"
RECEIVER_UDP_PORTS = [
    int(part)
    for part in re.split(r"[,\s]+", os.environ.get("CID_RECEIVER_UDP_PORTS", os.environ.get("UDP_PORTS", "11000")).strip())
    if part.isdigit() and int(part) > 0
]
RECEIVER_UDP_SOURCE_PORT = int(os.environ.get("CID_RECEIVER_UDP_SOURCE_PORT", "0") or 0)
FINANCE_AI_PROVIDER = os.environ.get("INFO_PARSER_PROVIDER", os.environ.get("AI_PROVIDER", "deepseek")).strip().lower()
FINANCE_AI_KEY = (
    os.environ.get("INFO_PARSER_API_KEY")
    or (os.environ.get("OPENAI_API_KEY") if FINANCE_AI_PROVIDER == "openai" else os.environ.get("DEEPSEEK_API_KEY"))
    or ""
)
FINANCE_AI_URL = os.environ.get(
    "INFO_PARSER_URL",
    "https://api.openai.com/v1/chat/completions"
    if FINANCE_AI_PROVIDER == "openai"
    else "https://api.deepseek.com/chat/completions",
)
FINANCE_AI_MODEL = os.environ.get(
    "INFO_PARSER_MODEL",
    os.environ.get("OPENAI_MODEL", "gpt-4.1") if FINANCE_AI_PROVIDER == "openai" else os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
)
FINANCE_AI_TIMEOUT = float(os.environ.get("INFO_PARSER_TIMEOUT", "30") or 30)
FINANCE_AI_JSON_RETRIES = int(os.environ.get("INFO_PARSER_JSON_RETRIES", "3") or 3)
ALLOWED_APP_ORIGINS = {
    "http://127.0.0.1:%d" % PORT,
    "http://localhost:%d" % PORT,
}

# 允许从浏览器透传到目标服务器的请求头(鉴权用)
FORWARD_HEADERS = {"authorization", "x-api-key", "anthropic-version", "x-goog-api-key", "content-type"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TELEGRAM_FINANCE_PROMPT_FILE = Path(os.environ.get(
    "TELEGRAM_FINANCE_PROMPT_FILE",
    os.path.join(BASE, "prompts", "telegram_finance_prompt.txt"),
)).expanduser()
TELEGRAM_FINANCE_PROMPT = TELEGRAM_FINANCE_PROMPT_FILE.read_text(encoding="utf-8").strip()
TELEGRAM_FINANCE_AI_INFO_KEYS = {
    "important",
    "unrelated",
    "summary",
    "reason",
    "affected_stocks",
    "affected_sectors",
    "positive_affected_stocks",
    "negative_affected_stocks",
    "positive_affected_sectors",
    "negative_affected_sectors",
    "direction",
    "st",
    "lt",
    "level",
    "conf",
    "coins",
    "cat",
    "news_label",
    "news_point",
    "news_implication",
    "btc_price",
    "us_tech_stocks",
    "korean_tech_stocks",
    "action",
    "bias",
    "reason_tags",
    "event_timing",
    "priced_in",
    "binary_event_risk",
    "gap",
    "why",
    "reverse",
}


class RetryableAIJsonError(ValueError):
    pass


ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_BLUE = "\033[34m"
ANSI_GRAY = "\033[90m"


def _ansi(text, color):
    return f"{color}{text}{ANSI_RESET}"


def _ai_level_color(value, *, effect=False, confidence=False):
    if confidence:
        try:
            return ANSI_GREEN if float(value) >= 70 else ANSI_YELLOW if float(value) >= 50 else ANSI_RED
        except (TypeError, ValueError):
            return ANSI_GRAY
    text = str(value or "").strip().lower()
    if effect:
        if text in {"increase", "positive", "bullish", "正面", "上涨"}:
            return ANSI_GREEN
        if text in {"decrease", "negative", "bearish", "负面", "下跌"}:
            return ANSI_RED
        return ANSI_GRAY if text in {"", "neutral", "unclear", "中性"} else ANSI_YELLOW
    return ANSI_GREEN if bool(value) else ANSI_YELLOW


def _ai_parsing_log(ai_result):
    important = "important" if ai_result.get("important") else "not important"
    effect = ai_result.get("effect") or ai_result.get("btc_price") or ai_result.get("direction") or "-"
    confidence = ai_result.get("confidence") if ai_result.get("confidence") is not None else ai_result.get("conf", "-")
    stocks = _string_list(ai_result.get("affected_stocks"))[:8]
    sectors = _string_list(ai_result.get("affected_sectors"))[:5]
    pos_stocks = _string_list(ai_result.get("positive_affected_stocks"))[:8]
    neg_stocks = _string_list(ai_result.get("negative_affected_stocks"))[:8]
    pos_sectors = _string_list(ai_result.get("positive_affected_sectors"))[:5]
    neg_sectors = _string_list(ai_result.get("negative_affected_sectors"))[:5]
    suffix = ""
    if stocks:
        suffix += f" / stocks={','.join(stocks)}"
    if sectors:
        suffix += f" / sectors={','.join(sectors)}"
    if pos_stocks or pos_sectors:
        suffix += (
            " / positively affected sector: "
            f"{_ansi(','.join(pos_sectors) or '-', ANSI_GREEN)}, stocks: "
            f"{_ansi(','.join(pos_stocks) or '-', ANSI_GREEN)}"
        )
    if neg_stocks or neg_sectors:
        suffix += (
            " / negatively affected sector: "
            f"{_ansi(','.join(neg_sectors) or '-', ANSI_RED)}, stocks: "
            f"{_ansi(','.join(neg_stocks) or '-', ANSI_RED)}"
        )
    return (
        "[proxy] AI parsing: "
        f"{_ansi(important, _ai_level_color(ai_result.get('important')))} / "
        f"{_ansi(effect, _ai_level_color(effect, effect=True))} / "
        f"{_ansi(confidence, _ai_level_color(confidence, confidence=True))}"
        f"{suffix}"
    )

# 部分新闻接口要求带来源页,否则返回 403
REFERERS = {
    "api.jinse.cn": "https://www.jinse.cn/",
    "api.jinse.com": "https://www.jinse.com/",
    "api.theblockbeats.news": "https://www.theblockbeats.info/",
    "www.panewslab.com": "https://www.panewslab.com/",
    "rss.panewslab.com": "https://www.panewslab.com/",
    "www.odaily.news": "https://www.odaily.news/",
    "rss.odaily.news": "https://www.odaily.news/",
    "www.techflowpost.com": "https://www.techflowpost.com/zh-CN/newsletter",
    "www.chaincatcher.com": "https://www.chaincatcher.com/",
}

# 阿里云 WAF JS 挑战的 cookie 缓存(如深潮 TechFlow)
ACW_COOKIES = {}
TELEGRAM_FINANCE_ITEMS = []
TELEGRAM_FINANCE_LOCK = threading.Lock()
TELEGRAM_FINANCE_ARCHIVE_LOCK = threading.Lock()
TELEGRAM_FINANCE_PROCESSED = {}
TELEGRAM_FINANCE_PROCESSED_LOCK = threading.Lock()
TELEGRAM_FINANCE_RECENT_DEDUPE = []
IMPORTANT_PERSON_KEYWORDS = ("川普", "特朗普", "trump", "马斯克", "elon musk", "musk", "黄仁勋", "jensen huang")


def _yaml_value(value):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return [part.strip().strip("'\"") for part in value[1:-1].split(",") if part.strip()]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _calendar_section_rows(text, section):
    rows = []
    current = None
    in_section = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith(f"{section}:"):
            in_section = True
            continue
        if not in_section:
            continue
        if raw[:1] not in {" ", "\t"} and not line.lstrip().startswith("-"):
            break
        item = line.strip()
        if item.startswith("- "):
            if current is not None:
                rows.append(current)
            current = {}
            rest = item[2:].strip()
            if ":" in rest:
                key, value = rest.split(":", 1)
                current[key.strip()] = _yaml_value(value)
            elif rest:
                current["symbol"] = _yaml_value(rest)
        elif current is not None and ":" in item:
            key, value = item.split(":", 1)
            current[key.strip()] = _yaml_value(value)
    if current is not None:
        rows.append(current)
    return rows


def _company_finance_rows(text):
    return _calendar_section_rows(text, "company_finance_reports")


def _macro_event_rows(text):
    return _calendar_section_rows(text, "macro_events")


def load_symbol_watchlist(path=SYMBOL_WATCHLIST_FILE):
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return []
    watchlist = []
    seen = set()
    for row in _calendar_section_rows(text, "symbol_watchlist"):
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        watchlist.append({"symbol": symbol, "name": str(row.get("name") or symbol).strip()})
    return watchlist


SYMBOL_WATCHLIST = load_symbol_watchlist()


def _symbol_watchlist_prompt():
    if not SYMBOL_WATCHLIST:
        return "- none"
    return "\n".join(f"- {row['symbol']}: {row.get('name') or row['symbol']}" for row in SYMBOL_WATCHLIST)


def _parse_utc_ms(value):
    if value is None:
        return 0
    if isinstance(value, (int, float)) and value > 0:
        return int(value if value > 10_000_000_000 else value * 1000)
    text = str(value).strip()
    if not text:
        return 0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def load_finance_calendar_events(path=FINANCE_CALENDAR_FILE):
    try:
        calendar_path = Path(path)
        text = calendar_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if Path(path) == FINANCE_CALENDAR_FILE and FINANCE_CALENDAR_FALLBACK_FILE.exists():
            text = FINANCE_CALENDAR_FALLBACK_FILE.read_text(encoding="utf-8")
        else:
            return []
    except OSError as exc:
        print(f"[proxy] failed to load finance calendar: {exc}")
        return []
    rows = _macro_event_rows(text) + _company_finance_rows(text)
    events = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        name = str(row.get("name") or (f"{symbol} 财报" if symbol else "")).strip()
        ts_ms = _parse_utc_ms(row.get("time_utc"))
        if name and ts_ms > 0:
            events.append({"n": name, "t": ts_ms})
    return events


def _calendar_config_text() -> str:
    try:
        return FINANCE_CALENDAR_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return FINANCE_CALENDAR_FALLBACK_FILE.read_text(encoding="utf-8") if FINANCE_CALENDAR_FALLBACK_FILE.exists() else ""


def _format_yaml_value(value):
    text = "" if value is None else str(value)
    if not text:
        return ""
    if re.search(r"[:#\n\r]", text):
        return json.dumps(text, ensure_ascii=False)
    return text


def _write_finance_calendar(macro_rows, company_rows):
    FINANCE_CALENDAR_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ["macro_events:"]
    for row in sorted(macro_rows, key=lambda item: str(item.get("time_utc") or "")):
        lines.append(f"  - name: {_format_yaml_value(row.get('name'))}")
        lines.append(f"    label: {_format_yaml_value(row.get('label'))}")
        lines.append(f"    time_utc: {_format_yaml_value(row.get('time_utc'))}")
        affects = row.get("affects")
        if isinstance(affects, list) and affects:
            lines.append("    affects: [" + ", ".join(str(item) for item in affects) + "]")
    lines.append("")
    lines.append("company_finance_reports:")
    for row in company_rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        lines.append(f"  - symbol: {symbol}")
        lines.append(f"    name: {_format_yaml_value(row.get('name') or symbol + ' 财报')}")
        lines.append(f"    time_utc: {_format_yaml_value(row.get('time_utc'))}")
    FINANCE_CALENDAR_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _fetch_text(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(2_000_000)
    return raw.decode("utf-8", errors="replace")


def _unfold_ics(text):
    rows = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and rows:
            rows[-1] += line[1:]
        else:
            rows.append(line.rstrip("\r"))
    return rows


def _parse_ics_dt(value):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        if value.endswith("Z"):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        elif "T" in value:
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo("America/New_York"))
        else:
            dt = datetime.strptime(value, "%Y%m%d").replace(hour=8, minute=30, tzinfo=ZoneInfo("America/New_York"))
    except ValueError:
        return ""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fetch_bls_macro_events():
    text = _fetch_text(BLS_ICS_URL)
    events = []
    current = {}
    for line in _unfold_ics(text):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            summary = str(current.get("SUMMARY") or "")
            start = _parse_ics_dt(current.get("DTSTART"))
            if start and "Consumer Price Index" in summary:
                events.append({"name": "美国CPI", "label": "macro_rate_policy", "time_utc": start, "affects": ["BTC", "QQQ", "NASDAQ"]})
            elif start and "Employment Situation" in summary:
                events.append({"name": "非农就业", "label": "macro_rate_policy", "time_utc": start, "affects": ["BTC", "QQQ", "NASDAQ"]})
            current = {}
        elif ":" in line and current is not None:
            key, value = line.split(":", 1)
            current[key.split(";", 1)[0]] = value
    return events


def _month_window(start, months):
    year = start.year
    month = start.month
    for _ in range(months):
        yield year, month
        month += 1
        if month > 12:
            year += 1
            month = 1


def _nyfed_event_utc(year, month, day, hhmm):
    try:
        hour, minute = [int(part) for part in str(hhmm).split(":", 1)]
        dt = datetime(int(year), int(month), int(day), hour, minute, tzinfo=ZoneInfo("America/New_York"))
    except Exception:
        return ""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fetch_nyfed_macro_events():
    month_names = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    events = []
    for year, month in _month_window(datetime.now(timezone.utc), 18):
        url = NYFED_CALENDAR_URL_TEMPLATE.format(month=month_names[month - 1], year=str(year)[-2:])
        try:
            text = _fetch_text(url)
        except Exception:
            continue
        for cell in re.findall(r"<div>\s*(\d{1,2})\s*<br/>(.*?)</div>", text, flags=re.S):
            day, body = cell
            body_text = re.sub(r"<[^>]+>", " ", body)
            times = re.findall(r"\((\d{2}:\d{2})\)", body_text)
            if "Consumer Price Index" in body_text:
                events.append({"name": "美国CPI", "label": "macro_rate_policy", "time_utc": _nyfed_event_utc(year, month, day, times[0] if times else "08:30"), "affects": ["BTC", "QQQ", "NASDAQ"]})
            if "Employment Situation" in body_text:
                events.append({"name": "非农就业", "label": "macro_rate_policy", "time_utc": _nyfed_event_utc(year, month, day, times[0] if times else "08:30"), "affects": ["BTC", "QQQ", "NASDAQ"]})
    return [event for event in events if event.get("time_utc")]


def _fomc_decision_utc(year, month_name, date_text):
    months = {
        "January": 1, "February": 2, "March": 3, "April": 4,
        "May": 5, "June": 6, "July": 7, "August": 8,
        "September": 9, "October": 10, "November": 11, "December": 12,
    }
    month = months.get(month_name)
    days = [int(item) for item in re.findall(r"\d{1,2}", date_text)]
    if not month or not days:
        return ""
    dt = datetime(int(year), month, days[-1], 14, 0, tzinfo=ZoneInfo("America/New_York"))
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fetch_fomc_macro_events():
    text = re.sub(r"<[^>]+>", "\n", _fetch_text(FOMC_CALENDAR_URL))
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    events = []
    month_re = re.compile(r"^(January|February|March|April|May|June|July|August|September|October|November|December)$")
    for idx, line in enumerate(lines):
        m = re.match(r"^(\d{4}) FOMC Meetings$", line)
        if not m:
            continue
        year = m.group(1)
        j = idx + 1
        while j < len(lines) and not re.match(r"^\d{4} FOMC Meetings$", lines[j]):
            if month_re.match(lines[j]) and j + 1 < len(lines):
                time_utc = _fomc_decision_utc(year, lines[j], lines[j + 1])
                if time_utc:
                    events.append({"name": "FOMC 利率决议", "label": "macro_rate_policy", "time_utc": time_utc, "affects": ["BTC", "QQQ", "NASDAQ"]})
                    j += 2
                    continue
            j += 1
    return events


def _dedupe_calendar_rows(rows):
    result = {}
    for row in rows:
        key = (str(row.get("name") or "").strip(), str(row.get("time_utc") or "").strip())
        if key[0] and key[1]:
            result[key] = row
    return list(result.values())


def _active_macro_rows(rows):
    now_ms = int(time.time() * 1000)
    low = now_ms - 14 * 86400 * 1000
    high = now_ms + 540 * 86400 * 1000
    return [row for row in rows if low <= _parse_utc_ms(row.get("time_utc")) <= high]


def refresh_finance_calendar_on_startup():
    if not FINANCE_CALENDAR_AUTO_UPDATE:
        return
    text = _calendar_config_text()
    company_rows = _company_finance_rows(text)
    existing_macro = _macro_event_rows(text)
    fetched = []
    errors = []
    try:
        fetched.extend(_fetch_fomc_macro_events())
    except Exception as exc:
        errors.append(str(exc))
    if not any(row.get("name") in {"美国CPI", "非农就业"} for row in fetched):
        try:
            fetched.extend(_fetch_nyfed_macro_events())
        except Exception as exc:
            errors.append(str(exc))
    if not any(row.get("name") in {"美国CPI", "非农就业"} for row in fetched):
        try:
            fetched.extend(_fetch_bls_macro_events())
        except Exception as exc:
            errors.append(str(exc))
    macro_rows = _active_macro_rows(_dedupe_calendar_rows(fetched or existing_macro))
    if macro_rows or company_rows:
        _write_finance_calendar(macro_rows, company_rows)
        print(f"[proxy] finance calendar ready: {FINANCE_CALENDAR_FILE} ({len(macro_rows)} macro, {len(company_rows)} company)")
    if errors:
        print("[proxy] finance calendar refresh warnings: " + " | ".join(errors))


def _json_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "重要", "important"}


def _has_important_person_name(text):
    text = str(text or "").lower()
    return any(name in text for name in IMPORTANT_PERSON_KEYWORDS)


def _direction(value):
    value = str(value or "").strip().lower()
    return value if value in {"increase", "decrease", "neutral", "unclear"} else "unclear"


def _int_range(value, default, low, high):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _string_list(value):
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _ai_text_record(text):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        raise RetryableAIJsonError("AI 未返回内容")
    summary = re.split(r"(?<=[。.!！？?])\s+", text, maxsplit=1)[0].strip()
    summary = summary[:160]
    lower = text.lower()
    coins = []
    for coin in ("BTC", "ETH", "SOL", "BNB", "DOGE", "XRP"):
        if re.search(rf"\b{coin}\b", text, re.IGNORECASE):
            coins.append(coin)
    risk_words = ("btc", "crypto", "coin", "fed", "fomc", "rate", "inflation", "cpi", "ppi", "tariff", "trump", "musk", "jensen", "nvda", "nvidia", "hbm", "tesla", "oil", "gold", "美元", "美联储", "降息", "加息", "通胀", "关税", "川普", "特朗普", "马斯克", "黄仁勋", "英伟达", "半导体", "原油", "黄金", "比特币", "以太")
    positive_words = ("利好", "上涨", "看涨", "降息", "放松", "流动性", "bullish", "positive", "cut")
    negative_words = ("利空", "下跌", "看跌", "加息", "收紧", "制裁", "战争", "bearish", "negative", "hike")
    important = any(word in lower or word in text for word in risk_words)
    unrelated = any(word in lower or word in text for word in ("unrelated", "not market", "无关", "无市场影响"))
    if not important and not unrelated:
        raise RetryableAIJsonError("AI 返回的文本不可解析")
    btc_price = "increase" if any(word in lower or word in text for word in positive_words) else "decrease" if any(word in lower or word in text for word in negative_words) else "unclear"
    return {
        "important": important,
        "unrelated": not important,
        "summary": summary,
        "reason": "AI returned prose; converted locally",
        "cat": "其他",
        "coins": coins,
        "btc_price": btc_price,
        "us_tech_stocks": "unclear",
        "korean_tech_stocks": "unclear",
    }


def _ai_json(content):
    text = str(content or "").replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return _ai_text_record(text)
    try:
        result = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RetryableAIJsonError("AI 返回的 JSON 格式无效") from exc
    if not isinstance(result, dict):
        return {}
    return {key: value for key, value in result.items() if key in TELEGRAM_FINANCE_AI_INFO_KEYS}


def _parse_time_ms(value):
    if isinstance(value, (int, float)) and value > 0:
        return int(value if value > 10_000_000_000 else value * 1000)
    text = str(value or "").strip()
    if not text:
        return int(time.time() * 1000)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return int(time.time() * 1000)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _archive_day(value):
    text = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    return datetime.now().strftime("%Y-%m-%d")


def _archive_telegram_finance_news(payload, *, result=None, error=None):
    text = str(payload.get("text") or "").strip()
    if not text:
        return
    record = {
        "received_at": datetime.now().isoformat(timespec="seconds"),
        "timestamp": str(payload.get("timestamp") or "").strip(),
        "source": str(payload.get("source") or "telegram").strip() or "telegram",
        "chat_name": str(payload.get("chat_name") or "Telegram").strip() or "Telegram",
        "msg_id": payload.get("msg_id"),
        "text": text,
    }
    if isinstance(result, dict):
        record["important"] = bool(result.get("important"))
        record["sent"] = bool(result.get("sent"))
        record["ai"] = result.get("record") if isinstance(result.get("record"), dict) else {}
    if error:
        record["error"] = str(error)
    path = TELEGRAM_FINANCE_ARCHIVE_DIR / f"{_archive_day(record['timestamp'])}.jsonl"
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    with TELEGRAM_FINANCE_ARCHIVE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def _log_incoming_telegram_finance_news(payload):
    text = str(payload.get("text") or "").strip()
    if not text:
        return
    record = {
        "received_at": datetime.now().isoformat(timespec="seconds"),
        "timestamp": str(payload.get("timestamp") or "").strip(),
        "source": str(payload.get("source") or "telegram").strip() or "telegram",
        "chat_name": str(payload.get("chat_name") or "Telegram").strip() or "Telegram",
        "msg_id": payload.get("msg_id"),
        "text": text,
    }
    path = TELEGRAM_FINANCE_INCOMING_LOG_DIR / f"{_archive_day(record['timestamp'])}.jsonl"
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    with TELEGRAM_FINANCE_ARCHIVE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def _remember_telegram_finance_item(payload):
    text = str(payload.get("text") or "").strip()
    chat_name = str(payload.get("chat_name") or "Telegram").strip() or "Telegram"
    msg_id = str(payload.get("msg_id") or int(time.time() * 1000)).strip()
    timestamp = str(payload.get("timestamp") or "").strip()
    row = {
        "id": f"tg:{chat_name}:{msg_id}",
        "time": _parse_time_ms(payload.get("timestamp")),
        "timestamp": timestamp,
        "title": text[:90],
        "body": text,
        "url": "#",
        "src": chat_name,
    }
    with TELEGRAM_FINANCE_LOCK:
        existing = next((idx for idx, item in enumerate(TELEGRAM_FINANCE_ITEMS) if item.get("id") == row["id"]), -1)
        if existing >= 0:
            TELEGRAM_FINANCE_ITEMS[existing] = row
        else:
            TELEGRAM_FINANCE_ITEMS.insert(0, row)
            del TELEGRAM_FINANCE_ITEMS[200:]
    return row


def _archive_row_payload(row):
    text = str(row.get("text") or "").strip()
    if not text:
        return None
    timestamp = str(row.get("timestamp") or row.get("received_at") or "").strip()
    chat_name = str(row.get("chat_name") or row.get("source") or "Telegram").strip() or "Telegram"
    msg_id = row.get("msg_id") or row.get("received_at") or f"archive:{timestamp}:{abs(hash(text))}"
    return {
        "type": "telegram_finance_news",
        "source": str(row.get("source") or "telegram").strip() or "telegram",
        "chat_name": chat_name,
        "msg_id": msg_id,
        "text": text,
        "timestamp": timestamp,
    }


def load_recent_telegram_finance_archive(limit=5):
    rows = []
    try:
        paths = sorted(TELEGRAM_FINANCE_ARCHIVE_DIR.glob("*.jsonl"), reverse=True)
    except OSError as exc:
        print(f"[proxy] failed to list telegram finance archive: {exc}")
        return 0
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            print(f"[proxy] failed to read telegram finance archive {path}: {exc}")
            continue
        for line in reversed(lines):
            if len(rows) >= limit:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                payload = _archive_row_payload(row)
                if payload:
                    rows.append(payload)
        if len(rows) >= limit:
            break
    for payload in reversed(rows):
        _remember_telegram_finance_item(payload)
    if rows:
        print(f"[proxy] loaded {len(rows)} telegram finance archive messages from {TELEGRAM_FINANCE_ARCHIVE_DIR}")
    return len(rows)


def telegram_finance_feed_items():
    with TELEGRAM_FINANCE_LOCK:
        return [dict(item) for item in TELEGRAM_FINANCE_ITEMS]


def _normalize_telegram_finance_text(text):
    text = str(text or "").lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"@[\w_]+", " ", text)
    text = re.sub(r"\brt\b", " ", text)
    text = re.sub(r"免费助力|注册币安|claude/gpt/grok/glm|全球顶级ai中转站|章鱼哥新闻流|octosignal", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff.%+-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _telegram_finance_number_tokens(text):
    return tuple(
        re.findall(
            r"(?<!\w)[+-]?\d+(?:\.\d+)?\s*(?:%|bp|bps|亿美元|万亿|亿|万|美元|usdt|btc|eth)?",
            str(text or "").lower(),
        )
    )


def _telegram_finance_dedupe_key(payload):
    text = str(payload.get("text") or "").strip()
    return ("finance_text", _normalize_telegram_finance_text(text))


def _remember_processed_telegram_finance(key, result):
    with TELEGRAM_FINANCE_PROCESSED_LOCK:
        TELEGRAM_FINANCE_PROCESSED[key] = result
        while len(TELEGRAM_FINANCE_PROCESSED) > 500:
            TELEGRAM_FINANCE_PROCESSED.pop(next(iter(TELEGRAM_FINANCE_PROCESSED)))


def _cached_processed_telegram_finance(key):
    with TELEGRAM_FINANCE_PROCESSED_LOCK:
        return TELEGRAM_FINANCE_PROCESSED.get(key)


def _cached_similar_telegram_finance(payload):
    threshold = TELEGRAM_FINANCE_DEDUPE_SIMILARITY_THRESHOLD
    if threshold <= 0 or threshold > 1:
        return None
    text = str(payload.get("text") or "").strip()
    normalized = _normalize_telegram_finance_text(text)
    if len(normalized) < 20:
        return None
    now_ms = _parse_time_ms(payload.get("timestamp"))
    window_ms = max(0, TELEGRAM_FINANCE_DEDUPE_WINDOW_SECONDS) * 1000
    numbers = _telegram_finance_number_tokens(text)
    with TELEGRAM_FINANCE_PROCESSED_LOCK:
        recent = []
        for item in TELEGRAM_FINANCE_RECENT_DEDUPE:
            if window_ms and now_ms - item["time"] > window_ms:
                continue
            recent.append(item)
        if len(recent) != len(TELEGRAM_FINANCE_RECENT_DEDUPE):
            TELEGRAM_FINANCE_RECENT_DEDUPE[:] = recent
        for item in recent:
            if numbers != item["numbers"]:
                continue
            other = item["normalized"]
            if abs(len(normalized) - len(other)) / max(len(normalized), len(other)) > 0.55:
                continue
            if difflib.SequenceMatcher(None, normalized, other).ratio() >= threshold:
                return item["result"]
    return None


def _remember_similar_telegram_finance(payload, result):
    text = str(payload.get("text") or "").strip()
    normalized = _normalize_telegram_finance_text(text)
    if len(normalized) < 20:
        return
    row = {
        "time": _parse_time_ms(payload.get("timestamp")),
        "normalized": normalized,
        "numbers": _telegram_finance_number_tokens(text),
        "result": result,
    }
    with TELEGRAM_FINANCE_PROCESSED_LOCK:
        TELEGRAM_FINANCE_RECENT_DEDUPE.insert(0, row)
        del TELEGRAM_FINANCE_RECENT_DEDUPE[max(1, TELEGRAM_FINANCE_DEDUPE_RECENT_LIMIT):]


def telegram_finance_news_record(result, *, chat_name, msg_id, text, timestamp):
    return {
        "source": "telegram",
        "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
        "timestamp": timestamp,
        "chat_name": str(chat_name or "").strip(),
        "msg_id": msg_id,
        "important": _json_bool(result.get("important")),
        "unrelated": _json_bool(result.get("unrelated")),
        "summary": str(result.get("summary") or "").strip(),
        "reason": str(result.get("reason") or "").strip(),
        "affected_stocks": _string_list(result.get("affected_stocks")),
        "affected_sectors": _string_list(result.get("affected_sectors")),
        "positive_affected_stocks": _string_list(result.get("positive_affected_stocks")),
        "negative_affected_stocks": _string_list(result.get("negative_affected_stocks")),
        "positive_affected_sectors": _string_list(result.get("positive_affected_sectors")),
        "negative_affected_sectors": _string_list(result.get("negative_affected_sectors")),
        "direction": str(result.get("direction") or "中性").strip(),
        "st": str(result.get("st") or "中性").strip(),
        "lt": str(result.get("lt") or "中性").strip(),
        "level": _int_range(result.get("level"), 1, 1, 5),
        "conf": _int_range(result.get("conf"), 0, 0, 100),
        "coins": _string_list(result.get("coins")),
        "cat": str(result.get("cat") or "其他").strip(),
        "news_label": _string_list(result.get("news_label")),
        "news_point": str(result.get("news_point") or "").strip(),
        "news_implication": str(result.get("news_implication") or "").strip(),
        "btc_price": _direction(result.get("btc_price")),
        "us_tech_stocks": _direction(result.get("us_tech_stocks")),
        "korean_tech_stocks": _direction(result.get("korean_tech_stocks")),
        "action": str(result.get("action") or "none").strip().lower(),
        "bias": str(result.get("bias") or "n/a").strip().lower(),
        "reason_tags": _string_list(result.get("reason_tags")),
        "event_timing": str(result.get("event_timing") or "unknown").strip().lower(),
        "priced_in": _json_bool(result.get("priced_in")),
        "binary_event_risk": _json_bool(result.get("binary_event_risk")),
        "gap": str(result.get("gap") or "none").strip(),
        "why": str(result.get("why") or "").strip(),
        "reverse": str(result.get("reverse") or "").strip(),
        "raw_text": text,
    }


def build_telegram_finance_warning_packet(record):
    msg_id = str(record.get("msg_id") or "").strip()
    chat_name = str(record.get("chat_name") or "telegram").strip() or "telegram"
    warning_id = f"market_news:telegram:{chat_name}:{msg_id or record.get('saved_at') or time.time()}"
    message = (
        f"{record.get('summary') or record.get('raw_text') or ''} | "
        f"BTC:{record.get('btc_price') or 'unclear'} | "
        f"美股:{record.get('us_tech_stocks') or 'unclear'} | "
        f"韩股:{record.get('korean_tech_stocks') or 'unclear'}"
    )
    return {
        "type": "warning_message",
        "warning_id": warning_id,
        "warning_type": "market_news",
        "title": "Market News",
        "trader_name": chat_name,
        "message": message,
        "price_direction": record.get("btc_price") or "unclear",
        "ttl_ms": TELEGRAM_FINANCE_TTL_MS,
        "timestamp": record.get("timestamp"),
        "raw_text": record.get("raw_text"),
    }


def _post_finance_ai(text):
    if not FINANCE_AI_KEY:
        raise RuntimeError(f"{FINANCE_AI_MODEL} API key not set")
    payload = {
        "model": FINANCE_AI_MODEL,
        "temperature": 0.2,
        "max_tokens": 600,
        "messages": [
            {"role": "system", "content": "You classify Telegram finance news impact for BTC and US/Korean tech stocks."},
            {"role": "user", "content": TELEGRAM_FINANCE_PROMPT.replace("{symbol_watchlist}", _symbol_watchlist_prompt()).replace("{message}", str(text or ""))},
        ],
    }
    req = urllib.request.Request(
        FINANCE_AI_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {FINANCE_AI_KEY}"},
    )
    last_error = None
    for attempt in range(FINANCE_AI_JSON_RETRIES + 1):
        if attempt:
            print(f"[proxy] AI JSON retry {attempt}/{FINANCE_AI_JSON_RETRIES}")
            time.sleep(0.5 * attempt)
        try:
            with urllib.request.urlopen(req, timeout=FINANCE_AI_TIMEOUT) as resp:
                data = json.loads(resp.read(MAX_RESPONSE_BODY).decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return _ai_json(content)
        except RetryableAIJsonError as exc:
            last_error = exc
            snippet = re.sub(r"\s+", " ", str(content if "content" in locals() else ""))[:240]
            if snippet:
                print(f"[proxy] AI JSON invalid: {exc}; response={snippet!r}")
            if attempt >= FINANCE_AI_JSON_RETRIES:
                break
    raise RuntimeError(f"自动重试 {FINANCE_AI_JSON_RETRIES} 次后仍未返回有效 JSON") from last_error


def _send_receiver_packet(packet):
    data = json.dumps(packet, ensure_ascii=False).encode("utf-8")
    for port in RECEIVER_UDP_PORTS or [11000]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if RECEIVER_UDP_SOURCE_PORT > 0:
                sock.bind(("127.0.0.1", RECEIVER_UDP_SOURCE_PORT))
            sock.sendto(data, (RECEIVER_UDP_HOST, port))
        finally:
            sock.close()


def process_telegram_finance_news(payload):
    source = str(payload.get("source") or "telegram").strip().lower()
    if source not in {"telegram", "discord"}:
        raise ValueError("source must be telegram or discord")
    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")
    chat_name = str(payload.get("chat_name") or "telegram").strip() or "telegram"
    msg_id = payload.get("msg_id")
    print(f"[proxy] 1 msg from {_ansi(source, ANSI_GREEN)} chat={_ansi(chat_name, ANSI_BLUE)}")
    raw_snippet = re.sub(r"\s+", " ", text)[:30]
    print(f"[proxy] raw text: {raw_snippet}")
    ai_result = _post_finance_ai(text)
    print(_ai_parsing_log(ai_result))
    if _has_important_person_name(text):
        ai_result["important"] = True
    record = telegram_finance_news_record(
        ai_result,
        chat_name=chat_name,
        msg_id=msg_id,
        text=text,
        timestamp=payload.get("timestamp") or datetime.utcnow().isoformat(timespec="seconds"),
    )
    sent = False
    if record.get("important") and _direction(record.get("btc_price")) in {"increase", "decrease"}:
        _send_receiver_packet(build_telegram_finance_warning_packet(record))
        sent = True
    return {"ok": True, "important": bool(record.get("important")), "unrelated": bool(record.get("unrelated")), "sent": sent, "record": record}


def ingest_telegram_finance_packet(payload):
    if str(payload.get("type") or "").strip().lower() != "telegram_finance_news":
        raise ValueError("packet type must be telegram_finance_news")
    _log_incoming_telegram_finance_news(payload)
    dedupe_key = _telegram_finance_dedupe_key(payload)
    cached = _cached_processed_telegram_finance(dedupe_key)
    if cached is None:
        cached = _cached_similar_telegram_finance(payload)
    if cached is not None:
        source = str(payload.get("source") or "telegram").strip().lower()
        chat_name = str(payload.get("chat_name") or "Telegram").strip() or "Telegram"
        msg_id = str(payload.get("msg_id") or "").strip()
        print(f"[proxy] duplicate telegram finance msg ignored source={source} chat={chat_name} msg_id={msg_id or '-'}")
        return cached
    _remember_telegram_finance_item(payload)
    try:
        result = process_telegram_finance_news(payload)
    except Exception as exc:
        _archive_telegram_finance_news(payload, error=exc)
        raise
    _remember_processed_telegram_finance(dedupe_key, result)
    _remember_similar_telegram_finance(payload, result)
    _archive_telegram_finance_news(payload, result=result)
    return result


def _telegram_finance_udp_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((TELEGRAM_FINANCE_UDP_HOST, TELEGRAM_FINANCE_UDP_PORT))
    print(f"[proxy] telegram finance UDP listening on {TELEGRAM_FINANCE_UDP_HOST}:{TELEGRAM_FINANCE_UDP_PORT}")
    while True:
        try:
            data, addr = sock.recvfrom(MAX_REQUEST_BODY)
            payload = json.loads(data.decode("utf-8"))
            if not isinstance(payload, dict):
                continue
            ingest_telegram_finance_packet(payload)
        except Exception as exc:
            print(f"[proxy] telegram finance UDP failed: {exc}")


def is_public_https_url(url):
    """只允许公网 HTTPS，避免本地代理被用于访问内网或本机服务。"""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return False
        port = parsed.port or 443
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        if not addresses:
            return False
        return all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except (OSError, ValueError):
        return False


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向也必须继续指向公网 HTTPS 地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_public_https_url(newurl):
            raise urllib.error.HTTPError(newurl, 403, "blocked redirect target", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def acw_sc_v2(arg1):
    """阿里云 WAF acw_sc__v2 cookie 算法(移植自 RSSHub 开源实现)"""
    pwd = "3000176000856006061501533003690027800375"
    code = [15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23, 25, 13,
            6, 11, 39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4, 17, 5, 3, 28, 34, 37, 12, 36]
    res = [""] * len(code)
    for i, cur in enumerate(arg1):
        for j, c in enumerate(code):
            if c == i + 1:
                res[j] = cur
    box = "".join(res)
    out = ""
    n = min(len(box), len(pwd))
    for i in range(0, n - 1, 2):
        out += format(int(box[i:i + 2], 16) ^ int(pwd[i:i + 2], 16), "02x")
    return out


def make_ctx():
    """带 ALPN 的 TLS 上下文:部分站点的 CDN 会掐断没有 ALPN 的连接"""
    ctx = ssl.create_default_context()
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except Exception:
        pass
    return ctx


def decompress_gzip_limited(data):
    """限制 gzip 解压后的体积，避免异常上游响应占用过多内存。"""
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
        result = compressed.read(MAX_RESPONSE_BODY + 1)
    if len(result) > MAX_RESPONSE_BODY:
        raise ValueError("upstream response too large")
    return result


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CryptoIntelLocal/1.0"
    sys_version = ""

    # ---------- 工具 ----------
    def _is_app_request(self):
        origin = (self.headers.get("Origin") or "").rstrip("/")
        referer = self.headers.get("Referer") or ""
        host = (self.headers.get("Host") or "").lower()
        same_origin_fetch = (
            (self.headers.get("Sec-Fetch-Site") or "").lower() == "same-origin"
            and host in {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT}
        )
        return same_origin_fetch or origin in ALLOWED_APP_ORIGINS or any(
            referer == allowed + "/" or referer.startswith(allowed + "/")
            for allowed in ALLOWED_APP_ORIGINS
        )

    def _is_local_request(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _cors(self):
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin in ALLOWED_APP_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "content-type, authorization, x-api-key, anthropic-version, x-goog-api-key",
        )

    def _reply(self, code, data, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if ctype.startswith("text/html"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            )
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        if not self._is_app_request():
            return self._reply(403, b'{"error":"origin not allowed"}')
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/favicon.ico":
            return self._reply(204, b"", "image/x-icon")
        if path == "/p":
            if not self._is_app_request():
                return self._reply(403, b'{"error":"origin not allowed"}')
            return self._forward("GET")
        if path == "/calendar-events":
            if not self._is_app_request():
                return self._reply(403, b'{"error":"origin not allowed"}')
            data = json.dumps(load_finance_calendar_events(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return self._reply(200, data)
        if path == "/telegram-finance-news":
            if not self._is_app_request():
                return self._reply(403, b'{"error":"origin not allowed"}')
            data = json.dumps(telegram_finance_feed_items(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return self._reply(200, data)
        if path == "/ping":
            return self._reply(200, b'{"ok":true,"app":"crypto-intelligence-desk","version":"1.0.1"}')
        self._reply(404, b'{"error":"not found"}')

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/telegram-finance-news":
            if not self._is_local_request():
                return self._reply(403, b'{"error":"local requests only"}')
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > MAX_REQUEST_BODY:
                    return self._reply(413, b'{"error":"request body too large"}')
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                result = process_telegram_finance_news(payload if isinstance(payload, dict) else {})
                data = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                return self._reply(200, data)
            except ValueError as exc:
                return self._reply(400, json.dumps({"ok": False, "error": str(exc)}).encode("utf-8"))
            except Exception as exc:
                print(f"[proxy] telegram finance news failed: {exc}")
                return self._reply(500, json.dumps({"ok": False, "error": str(exc)}).encode("utf-8"))
        if path == "/p":
            if not self._is_app_request():
                return self._reply(403, b'{"error":"origin not allowed"}')
            return self._forward("POST")
        self._reply(404, b'{"error":"not found"}')

    # ---------- 静态文件 ----------
    def _serve_file(self, name, ctype):
        try:
            with open(os.path.join(BASE, name), "rb") as f:
                self._reply(200, f.read(), ctype)
        except FileNotFoundError:
            self._reply(404, "index.html 不存在,请确认它和 proxy.py 在同一文件夹".encode("utf-8"),
                        "text/plain; charset=utf-8")

    # ---------- 请求转发 ----------
    def _forward(self, method):
        qs = urllib.parse.urlparse(self.path).query
        url = urllib.parse.parse_qs(qs).get("u", [None])[0]
        if not url or not is_public_https_url(url):
            return self._reply(400, b'{"error":"target must be a public https url"}')

        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_REQUEST_BODY:
                return self._reply(413, b'{"error":"request body too large"}')
            body = self.rfile.read(length) if length else None

        host = urllib.parse.urlparse(url).netloc

        def do_request(cookie=None):
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "*/*")
            req.add_header("Accept-Encoding", "gzip")
            if host in REFERERS:
                req.add_header("Referer", REFERERS[host])
                req.add_header("Origin", "https://" + host)
            if host == "api.theblockbeats.news":
                req.add_header("language", "cn")  # 官方RSS文档要求的语言头
            if host == "www.techflowpost.com":
                req.add_header("Accept-Language", "zh-CN")
            if cookie:
                req.add_header("Cookie", cookie)
            for k, v in self.headers.items():
                if k.lower() in FORWARD_HEADERS:
                    req.add_header(k, v)
            try:
                opener = urllib.request.build_opener(
                    SafeRedirectHandler(), urllib.request.HTTPSHandler(context=make_ctx())
                )
                with opener.open(req, timeout=30) as resp:
                    data = resp.read(MAX_RESPONSE_BODY + 1)
                    if len(data) > MAX_RESPONSE_BODY:
                        raise ValueError("upstream response too large")
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = decompress_gzip_limited(data)
                    return resp.status, resp.headers.get("Content-Type", "application/octet-stream"), data
            except urllib.error.HTTPError as e:
                data = e.read(MAX_RESPONSE_BODY + 1)
                if len(data) > MAX_RESPONSE_BODY:
                    raise ValueError("upstream response too large")
                try:
                    if e.headers.get("Content-Encoding") == "gzip":
                        data = decompress_gzip_limited(data)
                except Exception:
                    pass
                return e.code, e.headers.get("Content-Type", "application/json"), data

        try:
            status, ctype, data = do_request(ACW_COOKIES.get(host))
            # 命中阿里云 WAF JS 挑战:现场算出 cookie 后重试一次
            m = re.search(rb"var arg1='([0-9A-Fa-f]+)'", data[:4000])
            if m:
                ACW_COOKIES[host] = "acw_sc__v2=" + acw_sc_v2(m.group(1).decode())
                status, ctype, data = do_request(ACW_COOKIES[host])
            self._reply(status, data, ctype)
        except Exception:
            self._reply(502, b'{"error":"upstream request failed"}')

    # 精简日志:只打印转发目标的主机名
    def log_message(self, fmt, *args):
        try:
            msg = fmt % args
            if "/p?u=" in msg:
                host = urllib.parse.urlparse(
                    urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)["u"][0]).netloc
                if host == "api.deepseek.com":
                    return
                msg = "-> " + host
            elif msg.startswith('"GET /telegram-finance-news '):
                return
            sys.stdout.write("[proxy] %s\n" % msg)
        except Exception:
            pass


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print("=" * 52)
    print("   币圈新闻监控台 · 本地代理已启动")
    print("   请在浏览器打开:  http://127.0.0.1:%d" % PORT)
    print("   Telegram 财经消息 UDP: %s:%d" % (TELEGRAM_FINANCE_UDP_HOST, TELEGRAM_FINANCE_UDP_PORT))
    print("   关闭本窗口即停止服务")
    print("=" * 52)
    try:
        refresh_finance_calendar_on_startup()
        load_recent_telegram_finance_archive(limit=5)
        threading.Thread(target=_telegram_finance_udp_loop, daemon=True, name="telegram-finance-udp").start()
        ThreadingServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n监控台已停止。")
    except OSError:
        print("[错误] 端口 %d 已被占用,可能监控台已在运行。" % PORT)
        sys.exit(1)
