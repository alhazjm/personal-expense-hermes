---
name: expense-tracker
description: Categorizes bank transactions and logs them to Google Sheets budget tracker
version: 1.1.0
author: Hadi
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [finance, expense, tracking, automation]
---

# Expense Tracker

## When to Use
- A bank transaction webhook arrives from Gmail Apps Script
- The user manually reports a purchase via WhatsApp
- The user asks to log or record an expense

## Transaction Sources

The webhook payload includes a `type` field:
- `paylah` — DBS PayLah! debit wallet payment
- `card` — DBS/POSB or UOB credit card transaction

And a `payment_method` field with details like "DBS/POSB card ending 5305" or "PayLah! Wallet (Mobile ending 5167)".

## Categorization

**Use Hermes memory to cache the category list.** This avoids a Sheets API call on every transaction.

### First-time setup (or when user says "categories changed"):
1. Call `get_remaining_budget` once to fetch all category names
2. Save the list to Hermes memory: "Budget categories: Food & Drinks, Transport, Groceries, ..."
3. Use this cached list for all future categorization

### Per-transaction (no Sheets read needed):
1. Read the merchant name from the payload
2. Match to the cached category list using your judgment
3. If confident (>90%), assign directly
4. If ambiguous, ask the user via WhatsApp:
   ```
   $4.50 at 7-Eleven — which category? Food & Drinks, Groceries, or Misc?
   ```
5. Save the user's choice to memory for future use (e.g., "7-Eleven → Groceries")

### When to re-fetch categories:
- User says "I added/changed a category"
- First transaction of a new month (budget amounts may differ)
- Memory has no cached category list

## Handling Incoming Transactions

When a webhook payload arrives:

1. Extract: `merchant`, `amount`, `date`, `payment_method`, `type` from the payload
2. Determine category from memory (no API call)
3. Call `log_expense` with all fields — **this is the only Sheets API call per transaction**
4. Send a WhatsApp confirmation:
   ```
   Logged: $<amount> at <merchant> → <category>
   (<payment_method>)
   ```

Only call `get_remaining_budget` if the user explicitly asks about their budget status.

## Handling Manual Reports

When the user says something like "I spent $30 at IKEA":

1. Parse the amount and merchant from natural language
2. Determine category from memory
3. Call `log_expense` — one API call
4. Confirm the log

## API Cost Optimization

- **Per transaction**: 1 LLM call (MiniMax) + 1 Sheets write. No Sheets reads.
- **Budget queries**: Only when user asks ("How much left for food?") — 1 LLM call + 1 Sheets read.
- **Category refresh**: Once per month or on-demand — 1 Sheets read, saved to memory.
- **Merchant memory**: Builds over time, reducing ambiguity questions to near zero.

## Important Notes

- The budget categories can change — re-fetch when the user says so or at the start of each month
- Some categories may have $0 budget for certain months (one-off expenses) — still log the transaction
- The user's budget has per-month values that can differ (e.g., Food & Drinks might be $640 one month and $450 another)
- Keep the Transactions tab and Budget tab clean — all helper rows, cash flow analysis, pivots, etc. should live on separate tabs
