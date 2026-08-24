import importlib.util
from pathlib import Path


def load_proxy():
    path = Path(__file__).resolve().parents[1] / "proxy.py"
    spec = importlib.util.spec_from_file_location("news_tracker_proxy", path)
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    with proxy.TELEGRAM_FINANCE_PROCESSED_LOCK:
        proxy.TELEGRAM_FINANCE_RECENT_DEDUPE.clear()
    return proxy


def test_chinese_adjacent_amount_is_number_token():
    proxy = load_proxy()

    assert "650亿美元" in proxy._telegram_finance_number_tokens("ANTHROPIC年化营收在IPO前突破650亿美元")


def test_digest_reuses_cached_single_story_with_same_clause():
    proxy = load_proxy()
    result = {"ok": True, "record": {"summary": "anthropic revenue"}}
    single = {
        "text": "市场消息：ANTHROPIC年化营收在IPO前突破650亿美元。 ANTHROPIC IPO",
        "timestamp": "2026-08-18 03:41:19",
    }
    digest = {
        "text": "1. OPENAI签署俄亥俄州大型数据中心租约。 2. ANTHROPIC年化营收在IPO前突破650亿美元。 3. 软银投资2亿美元。 英伟达 OPENAI ANTHROPIC",
        "timestamp": "2026-08-18 12:05:21",
    }

    proxy._remember_similar_telegram_finance(single, result)

    assert proxy._cached_similar_telegram_finance(digest) is result
