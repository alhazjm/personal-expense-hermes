---
name: budget-manager
description: Handles budget queries, warnings, reallocation, and the guilt-free calculator
version: 1.0.0
author: Hadi
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [finance, budget, management]
---

# Budget Manager

## When to Use
- The user asks about their budget or spending
- A cron job triggers a budget check
- The user wants to reallocate budget between categories
- The user asks "Can I afford X?"

## Budget Queries

When the user asks "How much do I have left for food?" or similar:

1. Call `get_remaining_budget` with the relevant category (or all)
2. Format the response clearly:
   ```
   Food & Dining: $245.50 remaining of $800 (30.7% used)
   ```
3. If checking all categories, show a clean table sorted by percent used (highest first)

## Budget Warnings

When any category exceeds 80% usage:
- Proactively warn the user
- Suggest which categories have surplus that could offset

Format:
```
Warning: Transport is at 85% ($255/$300). You have 17 days left this month.
Tip: Entertainment is only at 30% — you could reallocate $50 if needed.
```

## Budget Reallocation

When the user says "Move $50 from Dining to Transport" or "Reallocate my budget to cover the clinic visit":

1. Call `get_remaining_budget` to see current state
2. Calculate the new limits
3. Call `update_budget` for each affected category
4. Confirm the changes:
   ```
   Updated:
   - Dining: $800 → $750
   - Transport: $300 → $350
   ```

## Guilt-Free Calculator

When the user asks "Can I afford a $150 jacket?":

1. Call `get_remaining_budget` for the relevant category (Shopping)
2. Calculate impact:
   - Current remaining vs. the purchase amount
   - Days left in the month
   - Daily spending rate if they make the purchase
3. Give an honest, friendly assessment:
   ```
   You have $280 left in Shopping with 17 days to go.
   After the jacket, you'd have $130 left ($7.65/day).
   Verdict: You can swing it, but keep an eye on other shopping this month.
   ```
