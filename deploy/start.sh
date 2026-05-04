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

# Inject webhook HMAC secret into config (can't use env vars in YAML)
if [ -n "${WEBHOOK_HMAC_SECRET:-}" ]; then
    sed -i "s/__WEBHOOK_SECRET_PLACEHOLDER__/${WEBHOOK_HMAC_SECRET}/" "$HERMES_HOME/config.yaml"
    echo "Webhook secret injected into config"
fi

# Seed cron jobs once per persistent disk. The marker lives on /data so it
# survives container restarts but is re-seeded if the disk is recreated.
# Failures are non-fatal: we still start the gateway so the user can inspect
# logs and the next restart retries.
SEED_MARKER="/data/cron/.seeded"
if [ ! -f "$SEED_MARKER" ]; then
    echo "Seeding cron jobs (first run on this disk)..."
    if bash /app/cron/setup-cron-jobs.sh; then
        touch "$SEED_MARKER"
        echo "Cron jobs seeded successfully"
    else
        echo "WARNING: cron seeding failed — gateway will start anyway; next restart will retry"
    fi
else
    echo "Cron jobs already seeded (marker: $SEED_MARKER)"
fi

echo "Starting Hermes gateway (foreground)..."
echo "TELEGRAM_BOT_TOKEN is set: $([ -n "$TELEGRAM_BOT_TOKEN" ] && echo 'yes' || echo 'NO')"
echo "TELEGRAM_ALLOWED_USERS: ${TELEGRAM_ALLOWED_USERS:-not set}"

# Use 'gateway run' (foreground) — 'gateway start' requires systemd which Docker doesn't have
exec hermes gateway run < /dev/null 2>&1
