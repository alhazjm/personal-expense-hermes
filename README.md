# Hermes Finance Agent — Personal Expense Tracker

An automated personal finance assistant built on the [Hermes Agent](https://github.com/alhazjm/hermes-agent) framework. It ingests bank transaction emails (DBS/UOB), categorizes expenses via LLM, updates a Google Sheets budget, and interacts via Telegram.

## Architecture

```
Gmail (DBS/UOB alerts)
    │
    ▼
Google Apps Script ──webhook──▶ Hermes Agent (MiniMax LLM)
                                    │
                        ┌───────────┼───────────┐
                        ▼           ▼           ▼
                  Google Sheets  Telegram    Cron Jobs
                  (budget DB)   (user UI)   (reminders)
```

## Deployment

This project is designed to run on **Render** ($7/mo Starter tier, Singapore region) for always-on operation.

See [deploy/README.md](deploy/README.md) for the full Render deployment guide.

## Local Development

### Prerequisites

- **Windows 11** with WSL2 enabled
- **Python 3.11+** (installed inside WSL2)
- **Google Cloud project** with Sheets API enabled
- **MiniMax API key**
- **Telegram bot** (from @BotFather)

### Setup

1. **Install WSL2** (PowerShell as Admin):
   ```powershell
   wsl --install
   ```

2. **Install dependencies** (inside WSL2):
   ```bash
   cd /mnt/c/Users/Hadi/Projects/personal-expense-tracker
   bash scripts/setup-wsl.sh
   ```

3. **Configure Google Cloud**:
   ```bash
   bash scripts/setup-google-oauth.sh
   ```

4. **Set up Google Sheet** — see [sheets-template/README.md](sheets-template/README.md)

5. **Configure Hermes**:
   ```bash
   bash hermes-config/install.sh
   ```
   Then edit `~/.hermes/.env`:
   ```
   MINIMAX_API_KEY=your-key
   GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/service-account.json
   GSPREAD_SPREADSHEET_ID=your-spreadsheet-id
   WEBHOOK_HMAC_SECRET=random-secret
   TELEGRAM_BOT_TOKEN=from-botfather
   TELEGRAM_ALLOWED_USER_IDS=your-telegram-user-id
   ```

6. **Link tools & start**:
   ```bash
   bash scripts/link-tools.sh
   hermes gateway start
   ```

## Usage (via Telegram)

| Message | What happens |
|---|---|
| "How much have I spent on food?" | Queries Sheets, returns total |
| "What's my remaining budget?" | Shows all categories with remaining amounts |
| "Remind me daily to stop ordering Grab" | Creates a cron job with daily reminder |
| "Can I afford a $150 jacket?" | Checks discretionary budget and gives assessment |
| "Reallocate $50 from Dining to Transport" | Updates budget limits in Sheets |

## Project Structure

```
personal-expense-tracker/
├── hermes-config/       # Hermes config templates
├── tools/               # Custom Hermes tools (Sheets integration)
├── skills/              # Agent skills (categorization, budgeting, summaries)
├── apps-script/         # Google Apps Script (Gmail → webhook)
├── cron/                # Scheduled job definitions
├── sheets-template/     # Google Sheet structure & default categories
├── deploy/              # Render deployment (Dockerfile, render.yaml, startup)
├── scripts/             # Setup & installation scripts
└── tests/               # Unit tests & fixtures
```

## Cost Breakdown

| Item | Cost |
|---|---|
| Render Starter (Singapore) | $7/month |
| MiniMax API | ~$1-2/month |
| Google Sheets API | Free |
| Telegram Bot API | Free |
| Gmail Apps Script | Free |

## Future Features

- **Guilt-Free Calculator** — "Can I afford X?" with budget impact assessment
- **Subscription Creep Detection** — Alerts for new recurring charges
- **Dynamic Budget Reallocation** — Natural language budget adjustments
- **Weekly Roasts/Cheers** — Fun Friday spending summaries
