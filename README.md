# Hermes Finance Agent — Personal Expense Tracker

An automated personal finance assistant built on the [Hermes Agent](https://github.com/alhazjm/hermes-agent) framework. It ingests bank transaction emails (DBS/UOB), categorizes expenses, updates a Google Sheets budget, and interacts via WhatsApp.

## Architecture

```
Gmail (DBS/UOB alerts)
    │
    ▼
Google Apps Script ──webhook──▶ Hermes Agent (MiniMax LLM)
                                    │
                        ┌───────────┼───────────┐
                        ▼           ▼           ▼
                  Google Sheets  WhatsApp    Cron Jobs
                  (budget DB)   (user UI)   (reminders)
```

## Prerequisites

- **Windows 11** with WSL2 enabled
- **Python 3.11+** (installed inside WSL2)
- **Node.js 18+** (installed inside WSL2, required for WhatsApp bridge)
- **Google Cloud project** with Sheets API & Gmail API enabled
- **MiniMax API key**
- **WhatsApp** personal account

## Setup

### 1. Install WSL2

Open **PowerShell as Admin** on Windows:

```powershell
wsl --install
```

Reboot, then open the Ubuntu terminal and set your Unix username/password.

### 2. Install dependencies inside WSL2

```bash
cd /mnt/c/Users/Hadi/Projects/personal-expense-tracker
bash scripts/setup-wsl.sh
```

This installs Python 3.11+, Node.js 18+, clones hermes-agent, and installs all Python deps.

### 3. Configure Google Cloud

```bash
bash scripts/setup-google-oauth.sh
```

Follow the prompts to:
1. Create a Google Cloud project
2. Enable Google Sheets API
3. Create a service account and download the JSON key
4. Share your Google Sheet with the service account email

### 4. Set up the Google Sheet

See [sheets-template/README.md](sheets-template/README.md) for the required sheet structure and default budget categories.

### 5. Configure Hermes

```bash
bash hermes-config/install.sh
```

Then edit `~/.hermes/.env` with your actual API keys:

```
MINIMAX_API_KEY=your-key-here
GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/service-account.json
GSPREAD_SPREADSHEET_ID=your-spreadsheet-id
WEBHOOK_HMAC_SECRET=a-random-secret-string
```

### 6. Link custom tools into Hermes

```bash
bash scripts/link-tools.sh
```

### 7. Set up WhatsApp bridge

```bash
hermes gateway setup
# Select: whatsapp
# Bridge type: baileys
# Scan the QR code with your phone
```

### 8. Deploy Gmail Apps Script

See [apps-script/README.md](apps-script/README.md) for clasp deployment instructions.

### 9. Set up cron jobs

```bash
bash cron/setup-cron-jobs.sh
```

### 10. Start the agent

```bash
hermes gateway start
```

## Usage (via WhatsApp)

| Message | What happens |
|---|---|
| "How much have I spent on food?" | Queries Sheets, returns total |
| "What's my remaining budget?" | Shows all categories with remaining amounts |
| "Remind me daily to stop grabbing Grab" | Creates a cron job with daily WhatsApp reminder |
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
├── scripts/             # Setup & installation scripts
└── tests/               # Unit tests & fixtures
```

## Future Features

- **Guilt-Free Calculator** — "Can I afford X?" with budget impact assessment
- **Subscription Creep Detection** — Alerts for new recurring charges
- **Dynamic Budget Reallocation** — Natural language budget adjustments
- **Weekly Roasts/Cheers** — Fun Friday spending summaries
