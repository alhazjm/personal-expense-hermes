---
name: weekly-summary
description: Generates weekly and monthly expense summaries with spending insights
version: 1.0.0
author: Hadi
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [finance, summary, reporting]
---

# Weekly Summary

## When to Use
- Friday evening cron job triggers weekly summary
- First of the month cron job triggers monthly report
- User asks for a summary ("How did I do this week?")

## Weekly Summary Format

1. Call `get_remaining_budget` for all categories
2. Generate a summary covering the past 7 days:

```
Week in Review (7-13 Apr)

Total spent: $342.50

Top categories:
  1. Food & Dining: $145.00 (12 transactions)
  2. Transport: $87.50 (8 transactions)
  3. Shopping: $110.00 (2 transactions)

Top merchants:
  1. GrabFood: $65.00 (5 orders)
  2. Grab: $52.50 (6 rides)
  3. Shopee: $95.00 (1 order)

Budget pace:
  On track: Groceries, Utilities, Health
  Watch out: Food & Dining (68% used, 55% of month gone)
  Over pace: Transport (72% used)
```

## Monthly Report (1st of Month)

Generate a full month retrospective:

1. Total spending vs total budget
2. Category-by-category breakdown (spent, limit, over/under)
3. Top 5 merchants by total amount
4. Month-over-month comparison if prior month data exists
5. One actionable suggestion for next month

## Tone

Keep it conversational and light. The Friday summary can be playful:
- If under budget: "Solid week! Your wallet thanks you."
- If over budget on food delivery: "GrabFood called... they want to name a menu item after you."
- Always end with encouragement, never guilt.
