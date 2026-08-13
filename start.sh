#!/usr/bin/env sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CID_PORT=${CID_PORT:-5200}
export CID_PORT
FINANCE_NEWS_PROXY_UDP_HOST=${FINANCE_NEWS_PROXY_UDP_HOST:-127.0.0.1}
FINANCE_NEWS_PROXY_UDP_PORT=${FINANCE_NEWS_PROXY_UDP_PORT:-5201}
export FINANCE_NEWS_PROXY_UDP_HOST FINANCE_NEWS_PROXY_UDP_PORT
FINANCE_CALENDAR_FILE=${FINANCE_CALENDAR_FILE:-"$APP_DIR/../state/finance_calendar.yaml"}
export FINANCE_CALENDAR_FILE
TELEGRAM_FINANCE_ARCHIVE_DIR=${TELEGRAM_FINANCE_ARCHIVE_DIR:-"$APP_DIR/../state/telegram_finance_news"}
export TELEGRAM_FINANCE_ARCHIVE_DIR
INFO_PARSER_BACKEND=${INFO_PARSER_BACKEND:-${AI_BACKEND:-local}}
case "$INFO_PARSER_BACKEND" in
  deepseek)
    INFO_PARSER_PROVIDER=${INFO_PARSER_PROVIDER:-deepseek}
    INFO_PARSER_URL=${INFO_PARSER_URL:-${DEEPSEEK_URL:-https://api.deepseek.com/chat/completions}}
    INFO_PARSER_API_KEY=${INFO_PARSER_API_KEY:-${DEEPSEEK_API_KEY:-}}
    INFO_PARSER_MODEL=${INFO_PARSER_MODEL:-${DEEPSEEK_MODEL:-deepseek-v4-pro}}
    ;;
  local)
    CODEX_PROXY_URL=${CODEX_PROXY_URL:-http://127.0.0.1:8787/v1/chat/completions}
    INFO_PARSER_PROVIDER=${INFO_PARSER_PROVIDER:-openai}
    INFO_PARSER_URL=${INFO_PARSER_URL:-"$CODEX_PROXY_URL"}
    INFO_PARSER_API_KEY=${INFO_PARSER_API_KEY:-${CODEX_PROXY_API_KEY:-${OPENAI_API_KEY:-codex-proxy}}}
    INFO_PARSER_MODEL=${INFO_PARSER_MODEL:-${OPENAI_MODEL:-gpt-5.4}}
    ;;
  *)
    printf '%s\n' "INFO_PARSER_BACKEND must be 'deepseek' or 'local'." >&2
    exit 1
    ;;
esac
export INFO_PARSER_BACKEND CODEX_PROXY_URL INFO_PARSER_PROVIDER INFO_PARSER_URL INFO_PARSER_API_KEY INFO_PARSER_MODEL
APP_URL="http://127.0.0.1:$CID_PORT"

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  printf '%s\n' "Python 3.8+ was not found. Install Python from https://www.python.org/downloads/ and run this file again." >&2
  exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
  printf '%s\n' "Python 3.8 or newer is required." >&2
  exit 1
fi

cd "$APP_DIR"
mkdir -p "$(dirname "$FINANCE_CALENDAR_FILE")"
mkdir -p "$TELEGRAM_FINANCE_ARCHIVE_DIR"
if [ ! -f "$FINANCE_CALENDAR_FILE" ]; then
  cp "$APP_DIR/finance_calendar.yaml" "$FINANCE_CALENDAR_FILE"
fi
printf '%s\n' "Crypto Intelligence Desk is running at $APP_URL"
printf '%s\n' "Finance calendar: $FINANCE_CALENDAR_FILE"
printf '%s\n' "Telegram finance archive: $TELEGRAM_FINANCE_ARCHIVE_DIR"
printf '%s\n' "Telegram finance UDP: $FINANCE_NEWS_PROXY_UDP_HOST:$FINANCE_NEWS_PROXY_UDP_PORT"
printf '%s\n' "Finance AI: $INFO_PARSER_BACKEND/$INFO_PARSER_MODEL via $INFO_PARSER_URL"
printf '%s\n' "Press Ctrl+C to stop."
exec "$PYTHON" proxy.py
