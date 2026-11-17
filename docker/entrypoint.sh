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

# Keep printer errors visible to the web status endpoint.
status_file="${WEBPRINTER_PRINTER_STATUS_FILE:-/run/webprinter/printer-error}"
mkdir -p "$(dirname "$status_file")"
rm -f "$status_file"

configure_printer() {
  if output=$(lpadmin -E -p "$PRINTER_NAME" -v "$PRINTER_ADDRESS" -m everywhere 2>&1); then
    rm -f "$status_file"
    echo "CUPS printer '$PRINTER_NAME' configured."
    cupsenable "$PRINTER_NAME" || true
    cupsaccept "$PRINTER_NAME" || true
    lpoptions -d "$PRINTER_NAME" || true
    return 0
  else
    status=$?
  fi
  message="Printer setup failed (exit code $status). Check power, network, and IPP address. $output"
  printf '%s\n' "$message" > "$status_file.tmp"
  mv -f "$status_file.tmp" "$status_file"
  echo "WARNING: $message" >&2
  return 1
}

if ! configure_printer; then
  # Retry in the background while the web application remains available.
  retry_interval="${WEBPRINTER_RETRY_INTERVAL:-30}"
  if [ "$retry_interval" -gt 0 ] 2>/dev/null; then
    (
      while sleep "$retry_interval"; do
        if configure_printer; then
          exit 0
        fi
      done
    ) >> /var/log/webprinter-printer-setup.log 2>&1 &
  fi
fi

exec uvicorn app:app --host "$APP_HOST" --port "$APP_PORT"
