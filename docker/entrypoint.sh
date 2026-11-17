#!/bin/sh
set -eu

PRINTER_NAME="${PRINTER_NAME:-}"
PRINTER_ADDRESS="${PRINTER_ADDRESS:-}"
APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-8000}"

if [ -z "$PRINTER_NAME" ]; then
  echo "Missing PRINTER_NAME."
  exit 1
fi

if [ -z "$PRINTER_ADDRESS" ]; then
  echo "Missing PRINTER_ADDRESS."
  exit 1
fi

mkdir -p /run/cups /var/run/cups /var/spool/cups /var/log/cups

# Start CUPS daemon in the container.
cupsd

for i in $(seq 1 30); do
  if lpstat -r >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! lpstat -r >/dev/null 2>&1; then
  echo "CUPS did not become ready in time."
  exit 1
fi

# Configure CUPS without stopping the web application on printer errors.
if lpadmin -E -p "$PRINTER_NAME" -v "$PRINTER_ADDRESS" -m everywhere; then
  echo "CUPS printer '$PRINTER_NAME' configured."
else
  status=$?
  echo "WARNING: lpadmin failed with exit code $status." >&2
  echo "Check PRINTER_ADDRESS and IPP Everywhere support." >&2
  if [ -s /var/log/cups/error_log ]; then
    echo "Recent CUPS errors:" >&2
    tail -n 30 /var/log/cups/error_log >&2
  fi
fi

if lpstat -p "$PRINTER_NAME" >/dev/null 2>&1; then
  cupsenable "$PRINTER_NAME" || true
  cupsaccept "$PRINTER_NAME" || true
  lpoptions -d "$PRINTER_NAME" || true
else
  echo "WARNING: Printer queue '$PRINTER_NAME' is unavailable. WebPrinter will still start." >&2
fi

exec uvicorn app:app --host "$APP_HOST" --port "$APP_PORT"
