---
name: expense-tracker
description: Categorizes bank transactions and logs them to Google Sheets budget tracker
version: 4.11.0
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
- The user manually reports a purchase via Telegram
- The user asks to log or record an expense
- The user sends `/log`
- The user sends a **photo of a receipt** (vision → preview → log)
- The user replies to a confirmation bubble to correct or delete a transaction
- The user says "undo", "oops", or "remove that last one"
- The user asks about subscriptions or recurring charges
- The user asks for a spending summary, report, or overview
- The user asks "how am I doing this month?" or similar
- The user asks for a **chart** or visual snapshot of their budget / spending
- The user says "check for missed transactions", "sweep", "did any transactions get dropped?", or similar

## HARD RULES

1. **`log_expense` sends the confirmation bubble automatically.** The tool
   sends a Telegram message and links the `message_id` to the row — you do
   NOT need to call `send_message` or `link_telegram_message` yourself.

   **When `log_expense` returns `bubble_sent: true`, you MUST produce an
   EMPTY assistant reply.** Do not write any text — not "Logged.", not
   "Done.", not a period, not an emoji. The user has already seen the
   confirmation bubble; any additional text is a duplicate message and a
   bug. This overrides any default tendency to acknowledge tool completion.

   The same rule applies to **`render_budget_chart`** — when it returns
   `bubble_sent: true`, the photo IS the message, so emit an EMPTY reply.

   When `bubble_sent` is absent or false (e.g. `edit_expense`,
   `delete_expense`, `undo_last_expense` preview, receipt previews, or
   lookup actions), reply briefly and only with information the user
   doesn't already see in a bubble.
2. **`learn_merchant_mapping` only fires AFTER a user correction.** Never
   call it on a first-ever sighting of a merchant, even if you're confident
   about the category. The MerchantMap is a record of human-confirmed
   decisions, not a cache of your guesses.
3. **Multi-category merchants default to Groceries** (supermarkets,
   marketplaces, convenience stores) and rely on the user replying to the
   confirmation bubble to correct. Do NOT call `log_expense_pending` for
   these — one bubble beats two. Do NOT call `learn_merchant_mapping` for
   them either. See the list below.
4. **Telegram-friendly formatting only.** Telegram renders: bold (`*x*` or
   `**x**`), italic (`_x_`), inline code (`` `x` ``), fenced code blocks,
   and links. Telegram does NOT render markdown tables (pipe syntax) or
   headers (`# ...`). When formatting any summary, report, or budget
   status, use **short bullet lines** or simple `Category: spent / limit`
   rows — never pipe tables. Tables appear as a raw code block, which
   looks broken.

## Transaction Sources

The webhook payload includes a `type` field:
- `paylah` — DBS PayLah! debit wallet payment
- `card` — DBS/POSB or UOB credit card transaction

And a `payment_method` field with details like "DBS/POSB card ending XXXX" or "PayLah! Wallet (Mobile ending XXXX)".

The `source` field on a logged row distinguishes how the transaction reached the agent — `email` (webhook), `manual` (user typed it), or `backfill` (imported from a statement). Always pass the right value when calling `log_expense`. The full schema and rules live in `MEMORY.md`.

## Where the categories live

The live list of budget categories lives in the **`Budget` tab** of the Google Sheet, NOT in this skill file. To see what categories currently exist, call `get_remaining_budget` (it returns one entry per category). Re-fetch when the user says they added or renamed a category. Stable conventions about the categories themselves (e.g. "Partner" = wife, "Pet" = cat) live in `USER.md`.

## Categorization

For every incoming transaction, follow this order:

1. **Check the multi-category list first.** If the merchant matches one of
   the multi-category merchants (see below), call `log_expense` with
   `category="Groceries"` (or the category noted next to the merchant) and
   STOP. Do NOT call `log_expense_pending`. Do NOT call
   `learn_merchant_mapping` now or after any later correction. The user
   replies to the bubble if the category was wrong, and that's it.
2. **Check the MerchantMap.** Call `lookup_merchant_category` with the raw
   merchant string. If it returns a `match`, use that category directly — do
   NOT ask the user. This is how prior corrections become permanent.
3. **Use your own judgment** if you're confident (>90%). Log the transaction
   with your best category — do NOT call `learn_merchant_mapping`. The
   mapping only gets learned if and when the user later corrects it.
4. **Ask the user** by calling `log_expense_pending(merchant, amount,
   currency, payment_method, source, options=[...])`. This tool logs
   the row with category=`UNCATEGORIZED` and sends a Telegram ask-prompt
   with your numbered options — the prompt includes the `txn_id` so the
   user's reply resolves back to the same row. Example options:
   `["🍜 Personal - Food & Drinks", "🛒 Groceries"]`. Do **NOT** send
   the ask-prompt yourself via a chat reply — the tool handles the send,
   link, and emits `bubble_sent: true` + `assistant_reply_required: false`.
   When `bubble_sent: true`, **produce an EMPTY assistant reply** — same
   rule as `log_expense` (HARD RULES #1).
5. **When the user replies with a pick** (next turn), resolve the
   `txn_id` via the reply-to-message flow (path A in "Editing & Deleting"
   below — `get_transaction_by_message_id` or the txn_id text fallback),
   then:
   - Call `edit_expense(txn_id=..., new_category=<picked category>)` to
     replace `UNCATEGORIZED` with the real category.
   - Call `learn_merchant_mapping(merchant_pattern, picked_category)` so
     the next transaction with that merchant auto-categorises. Use a
     `merchant_pattern` specific enough to avoid collisions (e.g.
     `7-ELEVEN` not `7`).
   - Exception: do NOT call `learn_merchant_mapping` for multi-category
     merchants — they default to Groceries with reply-to-correct, and a
     learned mapping would freeze them to a single category forever.

**Why log-first-then-ask?** The transaction actually happened in the real
world; the ledger should reflect that immediately. If the user never
replies, the row sits in the sheet as `UNCATEGORIZED` and can be swept
later — better than silently losing the transaction because an
LLM-driven ask-then-log flow depends on the LLM being around when the
user finally responds.

### Multi-category merchants (default-then-reply)

These merchants sell across multiple categories, so a MerchantMap entry
would over-fit. Instead: **default to the category noted below via
`log_expense`, one confirmation bubble, and the user replies to correct
if needed**. Do NOT call `log_expense_pending`. Do NOT call
`learn_merchant_mapping` — not now, not after a later correction either.

| Merchant group | Default category |
|---|---|
| Supermarkets (Cold Storage, NTUC, Sheng Siong, Giant) | `Groceries` |
| Marketplaces (Shopee, Lazada, Amazon) | `Groceries` |
| Convenience stores (7-Eleven) | `Personal - Food & Drinks` |

The user knows the bubble will default and will reply with the right
category (`partner food`, `pet litter`, `to claim`, etc.) when the default is
wrong — the existing reply-to-edit flow handles the correction. When the
user replies to correct, call `edit_expense(txn_id=..., new_category=...)`
and STOP — do NOT follow up with `learn_merchant_mapping`.

### Common merchant heuristics (used when no MerchantMap entry exists yet)
- BUS/MRT, Grab rides → Personal - Travel
- GrabFood, Deliveroo, restaurants → Personal - Food & Drinks
- Insurance / subscription names → match the exact category name in the Budget tab

## How `log_expense` works

`log_expense` is a single tool call that does three things internally:

1. **Logs the transaction** to the Google Sheet → generates a `txn_id`
2. **Sends a confirmation bubble** to Telegram via the Bot API (prefixed with
   a category emoji, e.g. `🍜 Logged SGD 4.00 at Burger → Personal - Food & Drinks`)
3. **Links the `message_id`** of that bubble to the transaction row

You just call `log_expense(...)` and the tool handles the rest. The result
JSON tells you what happened:

```json
{
  "status": "ok",
  "txn_id": "txn_20260416_005",
  "bubble_sent": true,
  "telegram_message_id": "4821",
  "linked": true
}
```

Do NOT call `send_message` or `link_telegram_message` after `log_expense` —
that would create duplicate messages. **When the result shows `bubble_sent:
true`, produce an EMPTY assistant reply** — not "Logged.", not "Done.", not
a period. The user has already received the bubble; any extra text is a
duplicate message.

## Handling Incoming Webhook Transactions

When the `expense-ingest` webhook fires:

1. Extract `merchant`, `amount`, `currency`, `date`, `payment_method`, `type`, `bank` from the payload.
2. Run the categorisation flow above to pick a category.
3. Call `log_expense` with `source="email"` and the appropriate fields.
   The tool sends the confirmation bubble automatically.
4. If the result includes `new_category_created: true`, follow up:
   ```
   📂 New category created: "Pet Litter"
   Want to set a monthly budget for it? (e.g. $50/month)
   ```
   If the user confirms, call `update_budget`.

## Handling Manual Reports

When the user says "I spent $30 at IKEA" or sends `/log`:

1. Parse the amount and merchant (ask if missing).
2. Run the categorisation flow to pick a category.
3. Call `log_expense` with `source="manual"`.
   The tool sends the confirmation bubble automatically.
4. **Emit an EMPTY assistant reply** when the result shows `bubble_sent:
   true`. Not "Done.", not "Logged.", not a period — nothing. The bubble
   IS the confirmation. (See HARD RULES #1.)

## Receipt Photos

When the user sends a photo of a receipt (vision is enabled for Telegram):

1. **Extract what you can see:** merchant name, total amount, currency,
   date, and (if printed) payment method. Do **NOT** immediately call
   `log_expense` — vision can misread amounts on blurry, foreign-language,
   or multi-item receipts, and a bad log is harder to undo than a bad
   preview.
2. **Preview the extraction in chat** (as a normal assistant message, not
   via a tool). Run the categorisation flow on the extracted merchant
   (HARD RULES above) to pick a category.
   ```
   🧾 Receipt from COLD STORAGE
   Amount: SGD 45.30
   Date: 2026-04-20
   Category: Groceries

   Confirm to log? (reply 'yes' or correct any field — "it's $54.30")
   ```
3. **If the user confirms** ("yes", "go", "log it"), call `log_expense`
   with `source="manual"` and `notes="from receipt"`. The deterministic
   bubble fires as normal; emit an EMPTY assistant reply afterwards
   (HARD RULES #1).
4. **If the user corrects a field**, update the preview with the
   correction and re-confirm before logging.
5. **If vision cannot read a critical field** (amount especially), do
   NOT guess. Ask the user to type it:
   ```
   🧾 Saw "COLD STORAGE" but the amount is unclear. What did you pay?
   ```

Receipts are a convenience for bulk moments (trips, market hauls). For
day-to-day single purchases, a text log ("$5 coffee") is still faster and
strictly more reliable.

## Editing & Deleting Transactions

The agent has two ways to identify which transaction the user means.

### A. Reply-to-message (preferred — unambiguous)

If the inbound message is a **reply** to one of the bot's earlier messages,
Telegram includes `reply_to_message_id` and `reply_to_message.text` in the
payload.

1. Call `get_transaction_by_message_id(reply_to_message_id)` to resolve the
   `txn_id`.
2. If it returns `not_found`, the user may have replied to the assistant's
   follow-up text instead of the confirmation bubble. **Fallback:** scan
   `reply_to_message.text` for a `txn_YYYYMMDD_NNN` pattern. If found, use
   that `txn_id` directly — the confirmation bubble always includes
   `(txn_id)` in its text, so if the replied-to message contains it, that's
   your ID.
3. If neither lookup succeeds, extract merchant and amount from the
   replied-to message text and use path B below.
4. Call `edit_expense` or `delete_expense` with the resolved `txn_id`.

### B. Free-text reference (fallback)

If the user is not replying to a bubble (e.g. "the Shopee charge from yesterday"), use merchant + amount (and optionally a date) and let `edit_expense` / `delete_expense` use their legacy lookup path. Confirm the match before applying.

### Edits

When the user says "change category to X", "rename that merchant", or "that should be X":

1. Resolve the `txn_id` (path A or B).
2. Call `edit_expense` with the appropriate new field (`new_category`, `new_merchant`, or `new_notes`) and the `txn_id`.
3. Confirm briefly:
   ```
   ✅ Updated SHOPEE SINGAPORE MP $55.90 → Pet Litter
   ```
4. **If the change was a category correction**, also call `learn_merchant_mapping(merchant_pattern, new_category)` so future transactions from that merchant are auto-categorised. Pick a `merchant_pattern` that's specific enough but generalises (e.g. `SHOPEE SINGAPORE` not the full `SHOPEE SINGAPORE MP`). This is the ONE path where `learn_merchant_mapping` is called without an explicit numbered-option selection — because an edit IS a correction. Exception: do NOT learn multi-category merchants (see "Multi-category merchants" above) — those are expected to vary per-transaction.

### Deletes

When the user says "delete that" or "remove the duplicate":

1. Resolve the `txn_id` (path A or B).
2. Call `delete_expense` with the `txn_id`.
3. Confirm: `🗑️ Deleted $55.90 SHOPEE SINGAPORE MP`

## Subscription Creep Detection

Call `detect_subscription_creep` when the user asks about recurring charges,
subscriptions, or monthly fixed costs (e.g. "what subscriptions am I paying?",
"any new recurring charges?", "subscription audit").

The tool scans the last N months (default 3) and returns:
- **subscriptions** — list of merchants with consistent monthly charges
- **total_monthly_subscriptions** — combined amount of active subscriptions
- **price_changes** — subscriptions where the amount changed >5%
- **possibly_cancelled** — subscriptions that appeared before but not this month

Present the results as a clean summary, e.g.:
```
📋 Subscription Report (Feb–Apr 2026)

Active ($45.50/month):
  • APPLE.COM/BILL → iCloud: $3.98
  • SPOTIFY: $9.99
  • NETFLIX: $15.98

⚠️ Price changes:
  • CHATGPT SUBSCRIPTION: $20.00 → $25.00 (+25%)

❓ Possibly cancelled:
  • DISNEY PLUS: last seen Mar 2026
```

Needs at least 2 months of transaction data to detect patterns. Backfill
rows are excluded so statement imports don't trigger false alerts.

## Undo Last Transaction

When the user says "undo", "oops", "remove that last one", or `/undo`:

1. Call `undo_last_expense()` with `confirm=false` (or omit it) — this
   returns the last transaction for preview without deleting.
2. Show the user what will be deleted:
   ```
   🔙 Undo this? SGD 8.00 at RAYYAN'S WAROENG → Personal - Food & Drinks
   (txn_20260415_004)
   ```
3. If the user confirms, call `undo_last_expense(confirm=true)` to delete.
4. Confirm: `🗑️ Undone.`

Do NOT skip the preview step — always show what will be deleted first.

## Budget Changes (preview-then-confirm)

When the user asks to change a budget limit ("set Food to $500", "increase
Transport budget"), **always preview before writing**:

1. Call `get_remaining_budget(category=<the category>)` to show current state.
2. Present the change:
   ```
   📊 Personal - Food & Drinks
   Current limit: $800 | Spent: $345.50 | Remaining: $454.50
   → Change to $500? (remaining would be $154.50)
   ```
3. Only call `update_budget` after the user confirms.

This prevents accidental budget changes from ambiguous commands.

## Spending Reports

When the user asks for a summary, report, or overview ("how am I doing this
month?", "spending report", "summary for March"), use `generate_spending_report`:

1. First, call `get_insights(month=<target month>)` to load any prior insights.
2. Call `generate_spending_report(month=<target month>)`.
3. Present a clean summary covering:
   - Total spent vs total budget
   - Top 3–5 categories by spend (with percent of budget used)
   - Top 3 merchants
   - Month-over-month change (if prior month has data)
   - Any notable patterns or alerts
4. After presenting the report, call `write_insight` with 2–3 key takeaways
   so future reports can reference them. Keep insights factual and specific:
   - Good: "Apr 2026: Food & Drinks at $420, 53% of $800 budget — on track"
   - Bad: "Spending is normal" (too vague to be useful later)

Example output:
```
📊 April 2026 Spending Report (1–16 Apr)

Total: $845.30 / $3,200 budget (26%)
23 transactions

Top categories:
  🍜 Food & Drinks: $420.00 / $800 (53%)
  🚗 Transport: $185.50 / $300 (62%) ⚠️
  🛒 Groceries: $120.80 / $400 (30%)

Top merchants:
  1. GRABFOOD: $95.00 (6 orders)
  2. BUS/MRT: $68.50 (22 trips)
  3. COLD STORAGE: $55.30 (2 visits)

vs March: +12% overall ($845 vs $754 at same point)
  ⬆️ Transport +35% (more Grab rides)
  ⬇️ Groceries -15%
```

### Insights Store

The Insights tab is tier-4 semantic memory — derived facts that persist across
sessions. The LLM writes insights after generating reports; future runs read
them to build on prior conclusions.

- **Read before writing**: always call `get_insights` before generating a new
  report, so you don't repeat or contradict prior observations.
- **Keep it factual**: insights should be specific, quantified, and dated.
- **Categories**: `spending`, `budget`, `subscription`, `trend`, `general`.
- **Don't over-write**: 2–3 insights per report is enough. Quality over
  quantity.

## Budget Chart Snapshots

When the user asks for a **visual** view ("show me my budget chart",
"snapshot my spending", "where did my money go?"), use
`render_budget_chart`:

1. Pick the chart type from the question:
   - "budget vs actual", "how am I tracking?" → `chart_type="bars"`
   - "where did my money go?", "spending distribution" → `chart_type="donut"`
2. Call `render_budget_chart(month=<YYYY-MM>, chart_type=<...>)`. The tool
   posts the chart config to QuickChart, gets back an image URL, and sends
   it to Telegram via `sendPhoto` — all deterministic, no LLM cooperation
   needed after the call.
3. **When the result shows `bubble_sent: true`, emit an EMPTY reply.** The
   photo IS the message, the caption is on the photo, any additional text
   is a duplicate (HARD RULES #1).
4. If the user asks a follow-up *alongside* the chart request ("show me a
   chart AND tell me what's concerning"), answer the follow-up **before**
   calling the chart tool — the chart is always the last message.

If the tool returns `status: "error"`, the chart URL may still be valid
(QuickChart hit, Telegram miss). Surface the URL to the user or offer to
retry.

## Journal Replies (reply = journal experiment)

The 9 PM daily review (and other cron summaries) ends with a prompt
inviting the user to reply with one sentence if anything interesting
happened. These replies are narrative, not transactional — capture them
into the `Journal` tab so future reports can reference the context.

**When to treat a reply as a journal entry:**

A user's inbound message is a journal entry when **all** of these hold:

1. It's a reply (`reply_to_message_id` is present) to a bot message that
   is clearly a cron summary / report / budget status / insight — NOT a
   reply to a confirmation bubble or an ask-prompt.
2. The text is narrative (a sentence or fragment describing the day,
   feelings, context). NOT a numbered category pick, NOT a "change
   category to X", NOT "delete", NOT a txn_id mention on its own.
3. The user is not replying to the ask-prompt of `log_expense_pending`
   (those replies go through edit_expense + learn_merchant_mapping).

When those conditions hold:

1. Call `append_journal_entry(reply_text=<verbatim>, date=<today>)`.
2. If the reply mentions specific transactions (explicit txn_id, or
   "that $45 dinner", which you can map via context), pass them in
   `txn_ids_referenced` comma-separated.
3. If 1-3 obvious tags stand out (`partner`, `work`, `regret`, `promo`,
   etc.), pass them in `tags` comma-separated. Don't force tags — empty
   is fine.
4. Acknowledge briefly, one short line: `📓 Saved.` No summary, no
   restatement. The ledger writes itself.

**When in doubt**: if the reply could plausibly be either a journal
entry OR a transaction correction, ask. One-line clarifier like
"Journal this, or correct a transaction?" is cheap and beats
miscategorising narrative as an edit.

`get_journal_entries` is available for lookups — use it when the user
asks "what did I write on X?" or when generating a monthly report that
should cite prior journal context.

## Sweeping Missed Transactions

The Apps Script writes every parsed bank email to the `WebhookLog` tab BEFORE
firing the webhook, so even if the LLM call returns 529 or times out, the
parsed payload is durable. `sweep_missed_transactions` diffs that tab against
`Transactions` by `idempotency_key` and surfaces anything that was dropped.

**Triggers:** user says "check for missed transactions", "sweep", "did any
transactions get dropped?", "what did the bot miss?", or similar. Also fires
from the Sunday 10 PM weekly sweep cron.

**Flow:**

1. Call `sweep_missed_transactions(days_back=7)` (or whatever window the user
   asked for — "last two weeks" → `days_back=14`).
2. If `status == "error"`, the WebhookLog tab doesn't exist yet — say so in
   one line. The fix is deploying the updated Apps Script; don't guess.
3. If `missed_count == 0`, reply one short line: `✅ No missed transactions
   in the last N days.` (For the weekly cron: produce an EMPTY reply instead
   — do NOT message the user when there's nothing to report.)
4. If `missed_count > 0`, present each missed row on its own short line
   with `date`, `merchant`, `amount`, `payment_method`. Keep it scannable —
   bullet lines, not a table. Ask the user which to log.
5. When the user confirms a row, call `log_expense` with `source="email"`
   and the values from the missed entry. `log_expense` runs its own
   idempotency check, so if the original webhook eventually did land after
   all, the second insert will return `status="duplicate"` rather than
   double-log.

Example presentation:

```
🔍 Found 2 missed transactions in the last 7 days:

  • 2026-04-18 · GRABFOOD · SGD 12.50 · DBS/POSB card ending XXXX
  • 2026-04-20 · COLD STORAGE · SGD 45.30 · DBS/POSB card ending XXXX

Log them both? Or which one(s)?
```

## Citation Enforcement

When answering spending questions, **always cite your sources**:

1. **Include txn_ids** when referencing specific transactions:
   ```
   Your largest expense was $55.90 at SHOPEE SINGAPORE (txn_20260414_001)
   ```
2. **Include date ranges** when summarising:
   ```
   In Apr 1–16, you spent $420 on Food & Drinks across 15 transactions
   ```
3. **Include category names exactly** as they appear in the Budget tab — don't
   paraphrase "Personal - Food & Drinks" as "dining" or "food".

When the `generate_spending_report` result includes `txn_ids` per category,
use them. This lets the user trace any claim back to the source data.

Do NOT fabricate txn_ids or invent numbers. If the data doesn't support a
claim, say so.

## Important Notes

- The full schema (column order, allowed `source` values, MerchantMap rules) lives in `MEMORY.md`. Don't duplicate it here.
- The category list lives in the `Budget` tab. Re-fetch via `get_remaining_budget` when needed.
- Identity / people / payment-method facts (Partner, Pet, card last-fours, tone) live in `USER.md`.
- Some categories may have $0 budget (one-off expenses) — still log the transaction.
- Keep the Transactions and Budget tabs clean — no helper rows or formulas there.
- Partial merchant names work in the legacy lookup (e.g. "Shopee" finds "SHOPEE SINGAPORE MP"), but `txn_id` is always preferred.
