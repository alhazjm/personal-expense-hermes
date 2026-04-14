#!/usr/bin/env bash
set -euo pipefail

echo "=== Setting up Hermes cron jobs for expense tracker ==="

# Daily 9 PM — spending summary for the day
hermes cron create \
    --schedule "0 21 * * *" \
    --command "Check today's spending across all categories and send me a brief summary. Warn me if any category is over 80%." \
    --skills expense-tracker,budget-manager \
    --deliver telegram
echo "Created: Daily 9 PM spending check"

# Friday 6 PM — weekly summary
hermes cron create \
    --schedule "0 18 * * 5" \
    --command "Generate my weekly expense summary with top categories, top merchants, and budget pace. Keep it fun." \
    --skills weekly-summary,budget-manager \
    --deliver telegram
echo "Created: Friday 6 PM weekly summary"

# 1st of month 9 AM — monthly report
hermes cron create \
    --schedule "0 9 1 * *" \
    --command "Generate last month's final expense report. Show total spent vs budget, category breakdown, top merchants, and one suggestion for this month." \
    --skills weekly-summary,budget-manager \
    --deliver telegram
echo "Created: 1st of month monthly report"

# Daily noon — budget warning check
hermes cron create \
    --schedule "0 12 * * *" \
    --command "Silently check if any budget category is over 80% used. Only message me if there's a warning." \
    --skills budget-manager \
    --deliver telegram
echo "Created: Daily noon budget warning"

echo ""
echo "=== All cron jobs created ==="
echo "Verify with: hermes cron list"
