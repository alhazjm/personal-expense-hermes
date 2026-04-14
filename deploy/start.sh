#!/usr/bin/env bash
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/root/.hermes}"
mkdir -p "$HERMES_HOME"

if [ -n "${MINIMAX_API_KEY:-}" ]; then
    cat > "$HERMES_HOME/.env" <<ENVEOF
MINIMAX_API_KEY=${MINIMAX_API_KEY}
GOOGLE_SERVICE_ACCOUNT_JSON=${GOOGLE_SERVICE_ACCOUNT_JSON:-}
GSPREAD_SPREADSHEET_ID=${GSPREAD_SPREADSHEET_ID:-}
WEBHOOK_HMAC_SECRET=${WEBHOOK_HMAC_SECRET:-}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_ALLOWED_USER_IDS=${TELEGRAM_ALLOWED_USER_IDS:-}
ENVEOF
    echo "Environment written to $HERMES_HOME/.env"
fi

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

echo "Starting Hermes gateway..."
exec hermes gateway start
