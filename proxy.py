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

try:
    PORT = int(os.environ.get("CID_PORT", "8899"))
    if not 1024 <= PORT <= 65535:
        raise ValueError
except ValueError:
    print("[错误] CID_PORT 必须是 1024 到 65535 之间的整数。")
    sys.exit(2)
BASE = os.path.dirname(os.path.abspath(__file__))
FINANCE_CALENDAR_FILE = Path(os.environ.get("FINANCE_CALENDAR_FILE", os.path.join(BASE, "finance_calendar.yaml"))).expanduser()
TELEGRAM_FINANCE_ARCHIVE_DIR = Path(os.environ.get(
    "TELEGRAM_FINANCE_ARCHIVE_DIR",
    os.path.join(BASE, "..", "state", "telegram_finance_news"),
)).expanduser()
MAX_REQUEST_BODY = 2 * 1024 * 1024
MAX_RESPONSE_BODY = 12 * 1024 * 1024
TELEGRAM_FINANCE_TTL_MS = 5 * 60 * 1000
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


class RetryableAIJsonError(ValueError):
    pass

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


def _yaml_value(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _company_finance_rows(text):
    rows = []
    current = None
    in_section = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("company_finance_reports:"):
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
        rows = _company_finance_rows(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except OSError as exc:
        print(f"[proxy] failed to load finance calendar: {exc}")
        return []
    events = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        name = str(row.get("name") or (f"{symbol} 财报" if symbol else "")).strip()
        ts_ms = _parse_utc_ms(row.get("time_utc"))
        if name and ts_ms > 0:
            events.append({"n": name, "t": ts_ms})
    return events


def _json_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "重要", "important"}


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


def _ai_json(content):
    text = str(content or "").replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise RetryableAIJsonError("AI 未返回 JSON")
    try:
        result = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RetryableAIJsonError("AI 返回的 JSON 格式无效") from exc
    return result if isinstance(result, dict) else {}


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
            {"role": "user", "content": TELEGRAM_FINANCE_PROMPT.replace("{message}", str(text or ""))},
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
            return _ai_json(data["choices"][0]["message"]["content"])
        except RetryableAIJsonError as exc:
            last_error = exc
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
    ai_result = _post_finance_ai(text)
    print(f"[proxy] telegram finance AI result: {json.dumps(ai_result, ensure_ascii=False, separators=(',', ':'))}")
    record = telegram_finance_news_record(
        ai_result,
        chat_name=payload.get("chat_name") or "telegram",
        msg_id=payload.get("msg_id"),
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
    _remember_telegram_finance_item(payload)
    try:
        result = process_telegram_finance_news(payload)
    except Exception as exc:
        _archive_telegram_finance_news(payload, error=exc)
        raise
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
            print(
                f"[proxy] telegram finance received chat={payload.get('chat_name') or '-'} "
                f"msg_id={payload.get('msg_id') or '-'} text={payload.get('text') or ''}"
            )
            result = ingest_telegram_finance_packet(payload)
            print(
                f"[proxy] telegram finance msg chat={payload.get('chat_name') or '-'} "
                f"msg_id={payload.get('msg_id') or '-'} sent={result.get('sent')}"
            )
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
        load_recent_telegram_finance_archive(limit=5)
        threading.Thread(target=_telegram_finance_udp_loop, daemon=True, name="telegram-finance-udp").start()
        ThreadingServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n监控台已停止。")
    except OSError:
        print("[错误] 端口 %d 已被占用,可能监控台已在运行。" % PORT)
        sys.exit(1)
