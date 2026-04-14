#!/usr/bin/env bash
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/root/.hermes}"
mkdir -p "$HERMES_HOME"

# Write service account JSON to a file instead of inlining in .env
SA_PATH="/data/service-account.json"
if [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ]; then
    echo "${GOOGLE_SERVICE_ACCOUNT_JSON}" > "$SA_PATH"
    echo "Service account JSON written to $SA_PATH"
fi

# Export all vars so hermes sees them immediately
export MINIMAX_API_KEY="${MINIMAX_API_KEY:-}"
export GOOGLE_SERVICE_ACCOUNT_JSON="$SA_PATH"
export GSPREAD_SPREADSHEET_ID="${GSPREAD_SPREADSHEET_ID:-}"
export WEBHOOK_HMAC_SECRET="${WEBHOOK_HMAC_SECRET:-}"
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
export TELEGRAM_ALLOWED_USERS="${TELEGRAM_ALLOWED_USERS:-}"
export TELEGRAM_HOME_CHANNEL="${TELEGRAM_ALLOWED_USERS:-}"
export TELEGRAM_HOME_CHANNEL_NAME="Home"

# Also write to .env for hermes internals
cat > "$HERMES_HOME/.env" <<ENVEOF
MINIMAX_API_KEY=${MINIMAX_API_KEY}
GOOGLE_SERVICE_ACCOUNT_JSON=${SA_PATH}
GSPREAD_SPREADSHEET_ID=${GSPREAD_SPREADSHEET_ID:-}
WEBHOOK_HMAC_SECRET=${WEBHOOK_HMAC_SECRET:-}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_ALLOWED_USERS=${TELEGRAM_ALLOWED_USERS:-}
TELEGRAM_HOME_CHANNEL=${TELEGRAM_ALLOWED_USERS:-}
TELEGRAM_HOME_CHANNEL_NAME=Home
ENVEOF
echo "Environment written to $HERMES_HOME/.env"

# Link persistent disk paths
for dir in sessions memories cron; do
    if [ -d "/data/$dir" ]; then
        ln -sfn "/data/$dir" "$HERMES_HOME/$dir"
        echo "$dir linked to persistent disk"
    else
        mkdir -p "/data/$dir"
        ln -sfn "/data/$dir" "$HERMES_HOME/$dir"
        echo "$dir created on persistent disk"
    fi
done

echo "Starting Hermes gateway (foreground)..."
echo "TELEGRAM_BOT_TOKEN is set: $([ -n "$TELEGRAM_BOT_TOKEN" ] && echo 'yes' || echo 'NO')"
echo "TELEGRAM_ALLOWED_USERS: ${TELEGRAM_ALLOWED_USERS:-not set}"

# Use 'gateway run' (foreground) — 'gateway start' requires systemd which Docker doesn't have
exec hermes gateway run < /dev/null 2>&1
