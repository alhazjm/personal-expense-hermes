# Google Sheet Setup

Create a new Google Sheet with the following structure. The agent reads and writes to this sheet.

## Sheet 1: Transactions

| Column | Type | Description |
|---|---|---|
| Date | Date (YYYY-MM-DD) | Transaction date |
| Merchant | Text | Store or service name |
| Amount | Number | Transaction amount |
| Currency | Text | Currency code (SGD) |
| Category | Text | Budget category |
| Source | Text | "email" or "manual" |
| Notes | Text | Optional notes |

Add the header row manually. The agent appends new rows below.

## Sheet 2: Budget

| Column | Type | Description |
|---|---|---|
| Category | Text | Budget category name |
| Monthly Limit | Number | Spending limit in SGD |

Populate with default categories from `budget_categories.json`:

| Category | Monthly Limit |
|---|---|
| Food & Dining | 800 |
| Transport | 300 |
| Groceries | 500 |
| Shopping | 400 |
| Entertainment | 200 |
| Subscriptions | 150 |
| Utilities | 200 |
| Health | 150 |
| Miscellaneous | 200 |

## Sharing with Service Account

After creating the sheet:

1. Copy the spreadsheet ID from the URL: `https://docs.google.com/spreadsheets/d/<THIS_IS_THE_ID>/edit`
2. Click **Share** and add the service account email (from your JSON key file, looks like `name@project.iam.gserviceaccount.com`)
3. Give it **Editor** access
4. Put the spreadsheet ID in your `.env` as `GSPREAD_SPREADSHEET_ID`
