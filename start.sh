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

(
  sleep 1
  if command -v open >/dev/null 2>&1; then
    open "$APP_URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$APP_URL"
  fi
) >/dev/null 2>&1 &

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
printf '%s\n' "Press Ctrl+C to stop."
exec "$PYTHON" proxy.py
