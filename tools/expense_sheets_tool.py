"""
Custom Hermes tools for personal expense tracking via Google Sheets.

Registers tools with the Hermes tool registry:
  - log_expense:                 Record a new transaction (returns txn_id)
  - log_expense_pending:         Log as UNCATEGORIZED and send an ask-prompt
  - update_budget:               Change a category's monthly limit
  - get_remaining_budget:        Check spending vs budget
  - edit_expense:                Edit fields on an existing transaction
  - delete_expense:              Remove a transaction
  - link_telegram_message:       Attach a Telegram message_id to a txn
  - get_transaction_by_message_id: Resolve a reply-to-message back to a txn
  - lookup_merchant_category:    Check learned MerchantMap before asking
  - learn_merchant_mapping:      Persist a merchant → category mapping
  - detect_subscription_creep:   Scan for recurring charges and flag creep
  - undo_last_expense:           Delete the most recently logged transaction
  - generate_spending_report:    Detailed monthly spending report with citations
  - write_insight:               Persist a derived insight for future reports
  - get_insights:                Read stored insights (tier-4 memory)
  - render_budget_chart:         Render a budget chart and deliver to Telegram
  - append_journal_entry:        Save user's freeform reply to the Journal tab
  - get_journal_entries:         Read Journal tab entries (most recent first)
  - sweep_missed_transactions:   Diff WebhookLog vs Transactions to find unlogged emails
  - get_card_cap_status:         Per-card per-category MTD cycle spend vs cap
  - recommend_card_for:          Which card to use for a category purchase
  - plan_month:                  Current CardStrategy plan + cycle status
  - review_card_efficiency:      Month-end scorecard: miles earned vs optimal
  - set_category_primary:        Manual promo override — write a CardStrategy row
"""

import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

from tools.registry import registry
from tools import sheets_client


TOOLSET = "expense_tracker"


def _sheets_configured() -> bool:
    return bool(
        os.environ.get("GSPREAD_SPREADSHEET_ID")
        and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    )


# Category-keyword → emoji table for the confirmation bubble prefix. First
# substring hit against the lowercased category wins, so put specific
# keywords (e.g. "pet litter") ABOVE general ones (e.g. "pet"). Pure
# visual scannability — no behaviour depends on the emoji.
_CATEGORY_EMOJI_KEYWORDS = [
    ("pet food",       "🍖"),
    ("pet litter",     "🐾"),
    ("pet grooming",   "✂️"),
    ("pet dental",     "🦷"),
    ("pet",            "🐱"),
    ("food",            "🍜"),
    ("drink",           "🍜"),
    ("groc",            "🛒"),
    ("travel",          "🚇"),
    ("transport",       "🚇"),
    ("bus/mrt",         "🚇"),
    ("fuel",            "⛽"),
    ("car rental",      "🚗"),
    ("bill",            "💡"),
    ("utilit",          "💡"),
    ("electric",        "💡"),
    ("water",           "💧"),
    ("insurance",       "🛡️"),
    ("gift",            "🎁"),
    ("donation",        "🎁"),
    ("clean",           "🧹"),
    ("laundry",         "🧺"),
    ("dental",          "🦷"),
    ("haircut",         "✂️"),
    ("foot massage",    "💆"),
    ("massage",         "💆"),
    ("dinner",          "🍽️"),
    ("date",            "🍽️"),
    ("parent",          "👨‍👩‍👧"),
    ("phone",           "📱"),
    ("internet",        "🌐"),
    ("gym",             "🏋️"),
    ("spotify",         "🎵"),
    ("google",          "☁️"),
    ("icloud",          "☁️"),
    ("onedrive",        "☁️"),
    ("obsidian",        "📝"),
    ("anthropic",       "🤖"),
    ("openai",          "🤖"),
    ("self improv",     "📚"),
    ("savings",         "💰"),
    ("to claim",        "🧾"),
    ("claim",           "🧾"),
    ("misc",            "📦"),
]
_DEFAULT_BUBBLE_EMOJI = "💳"


def _pick_category_emoji(category: str) -> str:
    lower = (category or "").lower()
    for keyword, emoji in _CATEGORY_EMOJI_KEYWORDS:
        if keyword in lower:
            return emoji
    return _DEFAULT_BUBBLE_EMOJI


def _send_telegram_bubble(text: str, chat_id: str = "") -> dict:
    """Send a message via the Telegram Bot API and return the message_id.

    Uses urllib (stdlib) to avoid depending on python-telegram-bot internals.
    Falls back to TELEGRAM_ALLOWED_USERS env var if chat_id is not provided
    (works for this single-user bot).

    Returns {"ok": True, "message_id": "..."} on success or
    {"ok": False, "error": "..."} on failure.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}

    if not chat_id:
        chat_id = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
        if "," in chat_id:
            chat_id = chat_id.split(",")[0].strip()

    if not chat_id:
        return {"ok": False, "error": "No chat_id and TELEGRAM_ALLOWED_USERS not set"}

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return {"ok": True, "message_id": str(data["result"]["message_id"])}
            return {"ok": False, "error": data.get("description", "Telegram API error")}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def _send_telegram_photo(photo_url: str, caption: str, chat_id: str = "") -> dict:
    """Send a photo via the Telegram Bot API using a URL that Telegram fetches
    server-side. Same auth / chat_id resolution as _send_telegram_bubble.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}

    if not chat_id:
        chat_id = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
        if "," in chat_id:
            chat_id = chat_id.split(",")[0].strip()

    if not chat_id:
        return {"ok": False, "error": "No chat_id and TELEGRAM_ALLOWED_USERS not set"}

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    payload = json.dumps({
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return {"ok": True, "message_id": str(data["result"]["message_id"])}
            return {"ok": False, "error": data.get("description", "Telegram API error")}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


# --- log_expense ---

LOG_EXPENSE_SCHEMA = {
    "name": "log_expense",
    "description": (
        "Log a new expense transaction to the Google Sheet. Automatically "
        "sends a Telegram confirmation bubble and links the message_id to "
        "the transaction row (enabling reply-to-message edits). "
        "Do NOT send the confirmation yourself — the tool handles it. "
        "When this tool returns bubble_sent=true, you MUST produce an "
        "EMPTY assistant reply — the user has already seen the confirmation "
        "via the bubble and any extra text is a duplicate message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "merchant": {
                "type": "string",
                "description": "Name of the merchant or store",
            },
            "amount": {
                "type": "number",
                "description": "Transaction amount in the given currency",
            },
            "currency": {
                "type": "string",
                "description": "Currency code (default: SGD)",
                "default": "SGD",
            },
            "category": {
                "type": "string",
                "description": (
                    "Budget category. Must match a category from the Budget sheet. "
                    "Use your best judgment to categorize based on the merchant name."
                ),
            },
            "date": {
                "type": "string",
                "description": "Transaction date in YYYY-MM-DD format. Defaults to today.",
            },
            "payment_method": {
                "type": "string",
                "description": "Payment source (e.g. 'DBS/POSB card ending XXXX', 'PayLah! Wallet')",
                "default": "",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes about the transaction",
                "default": "",
            },
            "source": {
                "type": "string",
                "description": (
                    "How this transaction reached the agent. Allowed values: "
                    "`email` (came from the Gmail Apps Script webhook), "
                    "`manual` (user typed it directly via Telegram), "
                    "`backfill` (imported from a statement, e.g. Partner's "
                    "supplementary card). Defaults to `manual`. The "
                    "subscription-creep detector ignores `backfill` rows."
                ),
                "enum": ["email", "manual", "backfill"],
                "default": "manual",
            },
        },
        "required": ["merchant", "amount", "category"],
    },
}


def _format_bubble(merchant: str, amount: float, currency: str,
                   category: str, payment_method: str, txn_id: str) -> str:
    """Build a short, scannable confirmation bubble from transaction data.

    Prefixes a category emoji (keyword-matched, deterministic) so the
    message scans faster in a busy Telegram chat. Pure cosmetic — the
    LLM never sees or reasons about this.
    """
    emoji = _pick_category_emoji(category)
    line1 = f"{emoji} Logged {currency} {amount:.2f} at {merchant} \u2192 {category}"
    parts = [line1]
    if payment_method:
        parts.append(payment_method)
    parts.append(f"({txn_id})")
    return "\n".join(parts)


def _retry_link(txn_id: str, message_id: str, max_attempts: int = 3) -> dict:
    """Retry sheets_client.link_telegram_message with exponential backoff.

    The Google Sheets API has a 60 req/min quota per user. When multiple
    webhooks fire concurrently (e.g. 3 bank emails arrive within seconds),
    the link step can fail with a 429 rate-limit error. Retrying with
    backoff (1s, 2s) handles the burst gracefully without losing the link.
    """
    for attempt in range(max_attempts):
        try:
            result = sheets_client.link_telegram_message(txn_id, message_id)
            return result
        except Exception as exc:
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)  # 1s, 2s
            else:
                return {
                    "status": "error",
                    "message": f"Failed after {max_attempts} attempts: {exc}",
                }


def handle_log_expense(args: dict, **kwargs) -> str:
    merchant = args.get("merchant", "")
    amount = float(args.get("amount", 0))
    category = args.get("category", "Miscellaneous")
    currency = args.get("currency", "SGD")
    date = args.get("date") or datetime.now().strftime("%Y-%m-%d")
    payment_method = args.get("payment_method", "")
    notes = args.get("notes", "")
    source = args.get("source", "manual")

    # Step 1: log the transaction (includes idempotency check)
    result = sheets_client.append_transaction(
        date=date,
        merchant=merchant,
        amount=amount,
        currency=currency,
        category=category,
        source=source,
        payment_method=payment_method,
        notes=notes,
    )

    # If duplicate detected, skip bubble and return early
    if result.get("status") == "duplicate":
        result["note"] = (
            "Duplicate transaction detected — this exact transaction was "
            "already logged. Returning existing entry."
        )
        return json.dumps(result)

    txn_id = result.get("txn_id", "")

    # Step 2: automatically send confirmation bubble and link message_id.
    # This is fully deterministic — no LLM cooperation required.
    if txn_id and os.environ.get("TELEGRAM_BOT_TOKEN"):
        bubble = _format_bubble(merchant, amount, currency, category,
                                payment_method, txn_id)
        send_result = _send_telegram_bubble(bubble)
        if send_result.get("ok"):
            message_id = send_result["message_id"]
            link_result = _retry_link(txn_id, message_id)
            result["telegram_message_id"] = message_id
            result["bubble_sent"] = True
            result["linked"] = link_result.get("status") == "ok"
            result["assistant_reply_required"] = False
            result["assistant_reply_policy"] = (
                "STOP. The confirmation bubble has already been delivered "
                "to the user via the Telegram Bot API. You MUST NOT emit "
                "any assistant text after this tool call. Produce an empty "
                "response. Do not write 'Logged.', 'Done.', or any "
                "acknowledgment — the user has already seen the bubble and "
                "any extra text is a duplicate message."
            )
            result["note"] = (
                "DUPLICATE MESSAGE WARNING: bubble_sent=true. Emit EMPTY reply."
            )
        else:
            result["bubble_sent"] = False
            result["bubble_error"] = send_result.get("error", "")

    # Step 3: card-optimiser post-cap nudge. Gated inside the helper
    # behind _check_setup — returns {"sent": false, "reason": "setup"} when
    # the Cards / CardStrategy tabs aren't populated, so this is a no-op
    # until the user activates the card optimiser. Wrapped defensively:
    # card-optimiser failures must NEVER break log_expense.
    try:
        from tools import card_optimiser
        nudge = card_optimiser.maybe_send_post_cap_nudge(
            payment_method=payment_method,
            category=category,
            amount=amount,
            txn_date=date,
        )
        if nudge.get("sent"):
            result["card_nudge_sent"] = True
            result["card_nudge_detail"] = nudge
    except Exception as exc:
        result["card_nudge_error"] = str(exc)

    # Step 4: ensure budget category exists
    cat_result = sheets_client.ensure_category_exists(category)
    if cat_result["status"] == "created":
        result["new_category_created"] = True
        result["new_category"] = category

    return json.dumps(result)


registry.register(
    name="log_expense",
    toolset=TOOLSET,
    schema=LOG_EXPENSE_SCHEMA,
    handler=handle_log_expense,
    check_fn=_sheets_configured,
)


# --- log_expense_pending ---
#
# Paired with log_expense. Use when a transaction arrives but categorisation
# is ambiguous (always-ask merchant, LLM confidence <90%, PayLah! transfer to
# an individual). Unlike log_expense, this:
#   - forces category="UNCATEGORIZED" (the row gets logged anyway so the
#     ledger reflects that the transaction actually happened)
#   - sends a Telegram "ask-prompt" bubble with numbered options instead of
#     the normal confirmation bubble
#   - links the ask-prompt's message_id to the row
#
# When the user replies to the ask-prompt, the existing reply-to-message
# flow resolves the txn_id via either the message_id link OR the reply-to
# text fallback (which scans for "txn_YYYYMMDD_NNN" in the quoted text —
# the txn_id is always embedded in the ask-prompt for this fallback). The
# skill then calls edit_expense + learn_merchant_mapping as normal.

_PENDING_CATEGORY = "UNCATEGORIZED"


LOG_EXPENSE_PENDING_SCHEMA = {
    "name": "log_expense_pending",
    "description": (
        "Log a transaction with category='UNCATEGORIZED' and send a Telegram "
        "ask-prompt asking the user which category to use. The ask-prompt "
        "includes the txn_id so the user's reply resolves back to the row. "
        "Use this instead of log_expense when categorisation is ambiguous — "
        "always-ask merchants (supermarkets, marketplaces, convenience), "
        "PayLah!/PayNow transfers to an individual, or when your confidence "
        "is below ~90%. You provide the numbered category options as a list. "
        "When the user replies with a choice, call edit_expense(txn_id=..., "
        "new_category=<picked>) then learn_merchant_mapping. "
        "When this tool returns bubble_sent=true, you MUST produce an EMPTY "
        "assistant reply — the user has already received the ask-prompt."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "merchant": {
                "type": "string",
                "description": "Name of the merchant or store",
            },
            "amount": {
                "type": "number",
                "description": "Transaction amount in the given currency",
            },
            "currency": {
                "type": "string",
                "description": "Currency code (default: SGD)",
                "default": "SGD",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Numbered category options to offer the user. Include "
                    "the category emoji if available, e.g. "
                    "['🍜 Food & Dining', '🛒 Groceries']. "
                    "Keep to 2-4 plausible options; the user can also "
                    "reply with a freeform category name."
                ),
            },
            "date": {
                "type": "string",
                "description": "Transaction date in YYYY-MM-DD format. Defaults to today.",
            },
            "payment_method": {
                "type": "string",
                "description": "Payment source (e.g. 'DBS/POSB card ending XXXX', 'PayLah! Wallet')",
                "default": "",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes about the transaction",
                "default": "",
            },
            "source": {
                "type": "string",
                "description": (
                    "How this transaction reached the agent. Same enum as "
                    "log_expense: `email`, `manual`, `backfill`."
                ),
                "enum": ["email", "manual", "backfill"],
                "default": "manual",
            },
        },
        "required": ["merchant", "amount", "options"],
    },
}


def _format_pending_prompt(merchant: str, amount: float, currency: str,
                           options: list[str], txn_id: str) -> str:
    """Build the ask-prompt bubble text. The trailing `(txn_id)` is critical —
    the reply-to fallback in SKILL.md scans the replied-to message for the
    txn_YYYYMMDD_NNN pattern, so if the link_telegram_message step fails
    (rate limit, race) the txn_id is still recoverable from the message text.
    """
    lines = [f"🤔 {currency} {amount:.2f} at {merchant} — which category?"]
    for i, opt in enumerate(options, start=1):
        lines.append(f"{i}. {opt}")
    lines.append(f"(or reply with another category name) ({txn_id})")
    return "\n".join(lines)


def handle_log_expense_pending(args: dict, **kwargs) -> str:
    merchant = args.get("merchant", "")
    amount = float(args.get("amount", 0))
    currency = args.get("currency", "SGD")
    options = args.get("options", []) or []
    date = args.get("date") or datetime.now().strftime("%Y-%m-%d")
    payment_method = args.get("payment_method", "")
    notes = args.get("notes", "")
    source = args.get("source", "manual")

    if not options:
        return json.dumps({
            "status": "error",
            "message": (
                "log_expense_pending requires `options` (at least one "
                "numbered category to offer the user). If you don't need "
                "to ask, call log_expense directly."
            ),
        })

    # Ensure the placeholder category exists so reports/budget calcs don't
    # fail on a missing-row lookup. This is idempotent — the Budget tab
    # only gets an UNCATEGORIZED row created once.
    sheets_client.ensure_category_exists(_PENDING_CATEGORY)

    result = sheets_client.append_transaction(
        date=date,
        merchant=merchant,
        amount=amount,
        currency=currency,
        category=_PENDING_CATEGORY,
        source=source,
        payment_method=payment_method,
        notes=notes,
    )

    if result.get("status") == "duplicate":
        result["note"] = (
            "Duplicate transaction — already logged. Not sending another "
            "ask-prompt. If the existing row needs recategorising, "
            "edit_expense directly."
        )
        return json.dumps(result)

    txn_id = result.get("txn_id", "")

    if txn_id and os.environ.get("TELEGRAM_BOT_TOKEN"):
        prompt = _format_pending_prompt(merchant, amount, currency, options, txn_id)
        send_result = _send_telegram_bubble(prompt)
        if send_result.get("ok"):
            message_id = send_result["message_id"]
            link_result = _retry_link(txn_id, message_id)
            result["telegram_message_id"] = message_id
            result["bubble_sent"] = True
            result["linked"] = link_result.get("status") == "ok"
            result["pending"] = True
            result["assistant_reply_required"] = False
            result["assistant_reply_policy"] = (
                "STOP. The ask-prompt has already been delivered to the "
                "user via the Telegram Bot API. You MUST NOT emit any "
                "assistant text after this tool call — the user will reply "
                "to the prompt and a later turn will handle the category "
                "pick. Produce an empty response."
            )
            result["note"] = (
                "DUPLICATE MESSAGE WARNING: bubble_sent=true. Emit EMPTY reply."
            )
        else:
            result["bubble_sent"] = False
            result["bubble_error"] = send_result.get("error", "")

    return json.dumps(result)


registry.register(
    name="log_expense_pending",
    toolset=TOOLSET,
    schema=LOG_EXPENSE_PENDING_SCHEMA,
    handler=handle_log_expense_pending,
    check_fn=_sheets_configured,
)


# --- update_budget ---

UPDATE_BUDGET_SCHEMA = {
    "name": "update_budget",
    "description": (
        "Update the monthly spending limit for a budget category. "
        "Use when the user wants to change how much they allocate to a category. "
        "Optionally specify a month (1-12) to update a specific month's budget."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "The budget category to update",
            },
            "monthly_limit": {
                "type": "number",
                "description": "New monthly spending limit in SGD",
            },
            "month": {
                "type": "integer",
                "description": "Month number (1-12). Defaults to current month. Use when user says 'set budget for June' etc.",
                "minimum": 1,
                "maximum": 12,
            },
        },
        "required": ["category", "monthly_limit"],
    },
}


def handle_update_budget(args: dict, **kwargs) -> str:
    category = args.get("category", "")
    monthly_limit = float(args.get("monthly_limit", 0))
    month = args.get("month")
    if month is not None:
        month = int(month)
    result = sheets_client.update_budget_row(category, monthly_limit, month_num=month)
    return json.dumps(result)


registry.register(
    name="update_budget",
    toolset=TOOLSET,
    schema=UPDATE_BUDGET_SCHEMA,
    handler=handle_update_budget,
    check_fn=_sheets_configured,
)


# --- get_remaining_budget ---

GET_BUDGET_SCHEMA = {
    "name": "get_remaining_budget",
    "description": (
        "Get the remaining budget for one or all categories for a given month. "
        "Shows limit, spent, remaining, and percent used."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Specific category to check. Omit for all categories.",
            },
            "month": {
                "type": "string",
                "description": "Month in YYYY-MM format. Defaults to current month.",
            },
        },
        "required": [],
    },
}


def handle_get_remaining_budget(args: dict, **kwargs) -> str:
    category = args.get("category")
    month = args.get("month")
    summary = sheets_client.get_spending_summary(month)
    if category:
        if category in summary:
            return json.dumps({category: summary[category]})
        return json.dumps({"error": f"Category '{category}' not found"})
    return json.dumps(summary)


registry.register(
    name="get_remaining_budget",
    toolset=TOOLSET,
    schema=GET_BUDGET_SCHEMA,
    handler=handle_get_remaining_budget,
    check_fn=_sheets_configured,
)


# --- edit_expense ---

EDIT_EXPENSE_SCHEMA = {
    "name": "edit_expense",
    "description": (
        "Edit an existing logged expense. Prefer locating the row by `txn_id` "
        "(unambiguous). If `txn_id` is not known, falls back to merchant + amount "
        "(+ optional date) — used for rows that pre-date the schema migration. "
        "Use when the user wants to correct a miscategorized transaction."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "txn_id": {
                "type": "string",
                "description": (
                    "Canonical transaction ID (format `txn_<YYYYMMDD>_<NNN>`). "
                    "Returned by `log_expense` and by `get_transaction_by_message_id`. "
                    "Always prefer this over merchant+amount."
                ),
            },
            "merchant": {
                "type": "string",
                "description": (
                    "Merchant name of the transaction to edit. "
                    "Only used when `txn_id` is not provided."
                ),
            },
            "amount": {
                "type": "number",
                "description": (
                    "Amount of the transaction to edit. "
                    "Only used when `txn_id` is not provided."
                ),
            },
            "date": {
                "type": "string",
                "description": "Date of the transaction (YYYY-MM-DD) to narrow the merchant+amount search. Optional.",
            },
            "new_category": {
                "type": "string",
                "description": "New category to assign",
            },
            "new_notes": {
                "type": "string",
                "description": "New notes for the transaction",
            },
            "new_merchant": {
                "type": "string",
                "description": "New merchant name to replace the existing one (e.g. rename 'SHOPEE SINGAPORE MP' to 'Pet Litter - Shopee')",
            },
        },
        "required": [],
    },
}


def handle_edit_expense(args: dict, **kwargs) -> str:
    txn_id = args.get("txn_id") or None
    merchant = args.get("merchant", "")
    amount_raw = args.get("amount")
    amount = float(amount_raw) if amount_raw is not None else 0.0
    date = args.get("date")

    if not txn_id and not merchant:
        return json.dumps({
            "status": "error",
            "message": "Provide either `txn_id` or `merchant` + `amount`",
        })

    updates = {}
    if "new_category" in args:
        updates["Category"] = args["new_category"]
    if "new_notes" in args:
        updates["Notes"] = args["new_notes"]
    if "new_merchant" in args:
        updates["Merchant"] = args["new_merchant"]

    result = sheets_client.edit_transaction(
        merchant, amount, date, updates, txn_id=txn_id
    )
    return json.dumps(result)


registry.register(
    name="edit_expense",
    toolset=TOOLSET,
    schema=EDIT_EXPENSE_SCHEMA,
    handler=handle_edit_expense,
    check_fn=_sheets_configured,
)


# --- delete_expense ---

DELETE_EXPENSE_SCHEMA = {
    "name": "delete_expense",
    "description": (
        "Delete an existing logged expense from the Transactions sheet. "
        "Prefer locating the row by `txn_id`. Falls back to merchant + amount "
        "(+ optional date) for legacy rows. "
        "Use when the user wants to remove a duplicate or incorrect entry."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "txn_id": {
                "type": "string",
                "description": "Canonical transaction ID (preferred)",
            },
            "merchant": {
                "type": "string",
                "description": "Merchant name of the transaction to delete (used when txn_id is absent)",
            },
            "amount": {
                "type": "number",
                "description": "Amount of the transaction to delete (used when txn_id is absent)",
            },
            "date": {
                "type": "string",
                "description": "Date of the transaction (YYYY-MM-DD) to narrow the search. Optional.",
            },
        },
        "required": [],
    },
}


def handle_delete_expense(args: dict, **kwargs) -> str:
    txn_id = args.get("txn_id") or None
    merchant = args.get("merchant", "")
    amount_raw = args.get("amount")
    amount = float(amount_raw) if amount_raw is not None else 0.0
    date = args.get("date")

    if not txn_id and not merchant:
        return json.dumps({
            "status": "error",
            "message": "Provide either `txn_id` or `merchant` + `amount`",
        })

    result = sheets_client.delete_transaction(
        merchant, amount, date, txn_id=txn_id
    )
    return json.dumps(result)


registry.register(
    name="delete_expense",
    toolset=TOOLSET,
    schema=DELETE_EXPENSE_SCHEMA,
    handler=handle_delete_expense,
    check_fn=_sheets_configured,
)


# --- link_telegram_message ---

LINK_TELEGRAM_MESSAGE_SCHEMA = {
    "name": "link_telegram_message",
    "description": (
        "Attach a Telegram message_id to an already-logged transaction. "
        "Call this immediately after sending a confirmation bubble for a freshly "
        "logged expense — pass the `txn_id` returned by `log_expense` and the "
        "`message_id` of the bubble Telegram just sent. This is what makes "
        "reply-to-message edits possible later."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "txn_id": {
                "type": "string",
                "description": "The txn_id returned by log_expense",
            },
            "telegram_message_id": {
                "type": "string",
                "description": "The Telegram message_id of the confirmation bubble",
            },
        },
        "required": ["txn_id", "telegram_message_id"],
    },
}


def handle_link_telegram_message(args: dict, **kwargs) -> str:
    txn_id = args.get("txn_id", "")
    telegram_message_id = str(args.get("telegram_message_id", ""))
    result = sheets_client.link_telegram_message(txn_id, telegram_message_id)
    return json.dumps(result)


registry.register(
    name="link_telegram_message",
    toolset=TOOLSET,
    schema=LINK_TELEGRAM_MESSAGE_SCHEMA,
    handler=handle_link_telegram_message,
    check_fn=_sheets_configured,
)


# --- get_transaction_by_message_id ---

GET_TXN_BY_MESSAGE_ID_SCHEMA = {
    "name": "get_transaction_by_message_id",
    "description": (
        "Look up a transaction by the Telegram message_id of the confirmation "
        "bubble that the bot originally sent for it. Use this when the user "
        "REPLIES to a logged transaction's confirmation message — the inbound "
        "payload's `reply_to_message_id` is what you pass here. Returns the "
        "transaction (including its `txn_id`) so you can pass that `txn_id` "
        "to `edit_expense` or `delete_expense`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "telegram_message_id": {
                "type": "string",
                "description": "The Telegram message_id from the user's reply_to_message_id field",
            },
        },
        "required": ["telegram_message_id"],
    },
}


def handle_get_transaction_by_message_id(args: dict, **kwargs) -> str:
    telegram_message_id = str(args.get("telegram_message_id", ""))
    result = sheets_client.find_transaction_by_message_id(telegram_message_id)
    if result is None:
        return json.dumps({
            "status": "not_found",
            "telegram_message_id": telegram_message_id,
        })
    row_num, row_dict = result
    return json.dumps({
        "status": "ok",
        "row": row_num,
        "transaction": row_dict,
    })


registry.register(
    name="get_transaction_by_message_id",
    toolset=TOOLSET,
    schema=GET_TXN_BY_MESSAGE_ID_SCHEMA,
    handler=handle_get_transaction_by_message_id,
    check_fn=_sheets_configured,
)


# --- lookup_merchant_category ---

LOOKUP_MERCHANT_CATEGORY_SCHEMA = {
    "name": "lookup_merchant_category",
    "description": (
        "Check the MerchantMap tab for a learned merchant → category mapping. "
        "Call this BEFORE asking the user to disambiguate a category — if a "
        "mapping exists, use it directly instead of asking. Returns the matching "
        "mapping (with `merchant_pattern` and `category`) or null."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "merchant": {
                "type": "string",
                "description": "The merchant string from the incoming transaction",
            },
        },
        "required": ["merchant"],
    },
}


def handle_lookup_merchant_category(args: dict, **kwargs) -> str:
    merchant = args.get("merchant", "")
    result = sheets_client.lookup_merchant_category(merchant)
    if result is None:
        return json.dumps({"status": "no_match", "merchant": merchant})
    return json.dumps({"status": "match", "merchant": merchant, "mapping": result})


registry.register(
    name="lookup_merchant_category",
    toolset=TOOLSET,
    schema=LOOKUP_MERCHANT_CATEGORY_SCHEMA,
    handler=handle_lookup_merchant_category,
    check_fn=_sheets_configured,
)


# --- learn_merchant_mapping ---

LEARN_MERCHANT_MAPPING_SCHEMA = {
    "name": "learn_merchant_mapping",
    "description": (
        "Persist a merchant → category mapping to the MerchantMap tab so future "
        "transactions matching this pattern get categorised automatically. Call "
        "this after the user corrects a categorisation (e.g. moves a Shopee "
        "transaction to Pet Litter) so you don't have to ask again next time. "
        "If a mapping with the same `merchant_pattern` already exists, its "
        "category is updated in place — no duplicates."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "merchant_pattern": {
                "type": "string",
                "description": (
                    "Substring to match against future merchant strings, "
                    "case-insensitive. Be specific enough to avoid collisions: "
                    "prefer 'GRABFOOD' over 'GRAB' if you want to distinguish "
                    "ride-hailing from food delivery."
                ),
            },
            "category": {
                "type": "string",
                "description": "The category to assign to matching merchants",
            },
        },
        "required": ["merchant_pattern", "category"],
    },
}


def handle_learn_merchant_mapping(args: dict, **kwargs) -> str:
    merchant_pattern = args.get("merchant_pattern", "")
    category = args.get("category", "")
    result = sheets_client.add_merchant_mapping(merchant_pattern, category)
    return json.dumps(result)


registry.register(
    name="learn_merchant_mapping",
    toolset=TOOLSET,
    schema=LEARN_MERCHANT_MAPPING_SCHEMA,
    handler=handle_learn_merchant_mapping,
    check_fn=_sheets_configured,
)


# --- detect_subscription_creep ---

DETECT_SUBSCRIPTION_CREEP_SCHEMA = {
    "name": "detect_subscription_creep",
    "description": (
        "Scan recent transactions for recurring subscription patterns. "
        "Identifies charges that repeat monthly with consistent amounts, "
        "flags new subscriptions, price changes, and possibly cancelled ones. "
        "Excludes backfill rows. Use when the user asks about subscriptions, "
        "recurring charges, or monthly fixed costs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "months_back": {
                "type": "integer",
                "description": (
                    "How many months of history to analyse (default 3). "
                    "Needs at least 2 months of data to detect patterns."
                ),
                "default": 3,
                "minimum": 2,
                "maximum": 12,
            },
        },
        "required": [],
    },
}


def handle_detect_subscription_creep(args: dict, **kwargs) -> str:
    months_back = int(args.get("months_back", 3))
    result = sheets_client.detect_subscription_creep(months_back)
    return json.dumps(result)


registry.register(
    name="detect_subscription_creep",
    toolset=TOOLSET,
    schema=DETECT_SUBSCRIPTION_CREEP_SCHEMA,
    handler=handle_detect_subscription_creep,
    check_fn=_sheets_configured,
)


# --- undo_last_expense ---

UNDO_LAST_EXPENSE_SCHEMA = {
    "name": "undo_last_expense",
    "description": (
        "Delete the most recently logged transaction. Use when the user says "
        "'undo', 'oops', 'remove that last one', or '/undo'. Shows the "
        "transaction details before deleting so the user can confirm."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "confirm": {
                "type": "boolean",
                "description": (
                    "Set to true to actually delete. When false (or omitted), "
                    "returns the last transaction for preview without deleting."
                ),
                "default": False,
            },
        },
        "required": [],
    },
}


def handle_undo_last_expense(args: dict, **kwargs) -> str:
    confirm = args.get("confirm", False)

    last = sheets_client.get_last_transaction()
    if last is None:
        return json.dumps({
            "status": "error",
            "message": "No transactions found.",
        })

    row_num, txn = last
    txn_id = txn.get("txn_id", "")

    if not confirm:
        return json.dumps({
            "status": "preview",
            "message": "This is the last transaction. Confirm to delete.",
            "transaction": txn,
            "row": row_num,
        })

    # Delete by txn_id if available, otherwise by row position
    if txn_id:
        result = sheets_client.delete_transaction("", 0.0, txn_id=txn_id)
    else:
        merchant = txn.get("Merchant", "")
        amount = float(txn.get("Amount", 0))
        result = sheets_client.delete_transaction(merchant, amount)

    return json.dumps(result)


registry.register(
    name="undo_last_expense",
    toolset=TOOLSET,
    schema=UNDO_LAST_EXPENSE_SCHEMA,
    handler=handle_undo_last_expense,
    check_fn=_sheets_configured,
)


# --- generate_spending_report ---

GENERATE_SPENDING_REPORT_SCHEMA = {
    "name": "generate_spending_report",
    "description": (
        "Generate a detailed monthly spending report with category breakdown, "
        "top merchants, daily spending, and month-over-month comparison. "
        "Includes txn_ids for citation. Use when the user asks for a summary, "
        "report, or overview of their spending."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": (
                    "Month in YYYY-MM format. Defaults to current month."
                ),
            },
        },
        "required": [],
    },
}


def handle_generate_spending_report(args: dict, **kwargs) -> str:
    month = args.get("month")
    result = sheets_client.generate_spending_report(month)
    return json.dumps(result)


registry.register(
    name="generate_spending_report",
    toolset=TOOLSET,
    schema=GENERATE_SPENDING_REPORT_SCHEMA,
    handler=handle_generate_spending_report,
    check_fn=_sheets_configured,
)


# --- write_insight ---

WRITE_INSIGHT_SCHEMA = {
    "name": "write_insight",
    "description": (
        "Persist a derived insight to the Insights tab for future reference. "
        "Call this after generating a spending report or summary to save key "
        "findings (e.g. 'User overspent Dining by 22% in week 14'). Future "
        "report runs read stored insights first, so summaries build on prior "
        "conclusions instead of recomputing from scratch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "insight": {
                "type": "string",
                "description": "Short factual statement about spending patterns",
            },
            "category": {
                "type": "string",
                "description": (
                    "Insight category: 'spending', 'budget', 'subscription', "
                    "'trend', or 'general'. Defaults to 'general'."
                ),
                "default": "general",
            },
            "month": {
                "type": "string",
                "description": "Month the insight relates to (YYYY-MM). Defaults to current month.",
            },
        },
        "required": ["insight"],
    },
}


def handle_write_insight(args: dict, **kwargs) -> str:
    insight = args.get("insight", "")
    category = args.get("category", "general")
    month = args.get("month")
    result = sheets_client.write_insight(insight, category, month)
    return json.dumps(result)


registry.register(
    name="write_insight",
    toolset=TOOLSET,
    schema=WRITE_INSIGHT_SCHEMA,
    handler=handle_write_insight,
    check_fn=_sheets_configured,
)


# --- get_insights ---

GET_INSIGHTS_SCHEMA = {
    "name": "get_insights",
    "description": (
        "Read stored insights from the Insights tab. Call this BEFORE "
        "generating a new spending report to build on prior conclusions. "
        "Filter by month and/or category. Returns most recent first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": "Filter by month (YYYY-MM). Omit for all months.",
            },
            "category": {
                "type": "string",
                "description": "Filter by insight category. Omit for all categories.",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of insights to return (default 20).",
                "default": 20,
            },
        },
        "required": [],
    },
}


def handle_get_insights(args: dict, **kwargs) -> str:
    month = args.get("month")
    category = args.get("category")
    limit = int(args.get("limit", 20))
    result = sheets_client.get_insights(month, category, limit)
    return json.dumps(result)


registry.register(
    name="get_insights",
    toolset=TOOLSET,
    schema=GET_INSIGHTS_SCHEMA,
    handler=handle_get_insights,
    check_fn=_sheets_configured,
)


# --- render_budget_chart ---

def _create_quickchart_url(config: dict, width: int = 700, height: int = 420) -> dict:
    """POST chart config to QuickChart.io and get back a short image URL.

    Using POST avoids URL-length concerns and returns a permanent short
    URL that Telegram can fetch server-side via sendPhoto. On any
    network/HTTP failure, returns {"ok": False, "error": ...} and the
    caller decides whether to surface or retry.
    """
    url = "https://quickchart.io/chart/create"
    payload = json.dumps({
        "chart": config,
        "width": width,
        "height": height,
        "format": "png",
        "backgroundColor": "white",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("success") and data.get("url"):
                return {"ok": True, "url": data["url"]}
            return {"ok": False, "error": data.get("message", "QuickChart error")}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def _build_budget_chart_config(summary: dict, chart_type: str, month_label: str) -> dict:
    """Build a QuickChart config from get_spending_summary output.

    summary is {category: {"limit": float, "spent": float, "remaining": float,
    "percent_used": float}} with possibly "error" string entries for
    categories that failed. Filters to rows with a real limit or real
    spend, sorts by spent desc, takes top 10.
    """
    budgeted = []
    for cat, data in summary.items():
        if isinstance(data, dict):
            limit = float(data.get("limit", 0) or 0)
            spent = float(data.get("spent", 0) or 0)
            if limit > 0 or spent > 0:
                budgeted.append((cat, limit, spent))
    budgeted.sort(key=lambda row: row[2], reverse=True)
    top = budgeted[:10]
    labels = [row[0] for row in top]
    spent_vals = [row[2] for row in top]
    limit_vals = [row[1] for row in top]

    if chart_type == "donut":
        return {
            "type": "doughnut",
            "data": {
                "labels": labels,
                "datasets": [{
                    "data": spent_vals,
                    "backgroundColor": [
                        "#ef4444", "#f97316", "#f59e0b", "#eab308", "#84cc16",
                        "#22c55e", "#14b8a6", "#06b6d4", "#3b82f6", "#8b5cf6",
                    ],
                }],
            },
            "options": {
                "plugins": {
                    "title": {
                        "display": True,
                        "text": f"Spending by Category \u2014 {month_label}",
                        "font": {"size": 16},
                    },
                    "legend": {"position": "right"},
                },
            },
        }
    # Default: horizontal bars, Spent vs Budget side-by-side
    return {
        "type": "horizontalBar",
        "data": {
            "labels": labels,
            "datasets": [
                {"label": "Spent",  "data": spent_vals, "backgroundColor": "#ef4444"},
                {"label": "Budget", "data": limit_vals, "backgroundColor": "#3b82f6"},
            ],
        },
        "options": {
            "title": {
                "display": True,
                "text": f"Budget vs Spent \u2014 {month_label}",
                "fontSize": 16,
            },
            "scales": {
                "xAxes": [{"ticks": {"beginAtZero": True}}],
            },
            "legend": {"position": "bottom"},
        },
    }


RENDER_BUDGET_CHART_SCHEMA = {
    "name": "render_budget_chart",
    "description": (
        "Render a budget chart (bars or donut) for the given month and "
        "deliver it as a photo to the user's Telegram. Use when the user "
        "asks for a visual snapshot, chart, or 'show me' their spending "
        "distribution or budget vs actual. The tool sends the photo "
        "automatically via the Bot API \u2014 do NOT call send_message after. "
        "When bubble_sent=true, emit an EMPTY assistant reply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": "Month in YYYY-MM format. Defaults to current month.",
            },
            "chart_type": {
                "type": "string",
                "enum": ["bars", "donut"],
                "description": (
                    "'bars' = horizontal bars with Spent vs Budget side-by-"
                    "side (use for budget vs actual comparisons). 'donut' = "
                    "share of spend by category (use for 'where did my money "
                    "go?'). Defaults to bars."
                ),
                "default": "bars",
            },
        },
        "required": [],
    },
}


def handle_render_budget_chart(args: dict, **kwargs) -> str:
    month = args.get("month")
    chart_type = args.get("chart_type", "bars")

    try:
        summary = sheets_client.get_spending_summary(month)
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "message": f"Could not read summary: {exc}",
        })

    month_label = month or datetime.now().strftime("%B %Y")
    config = _build_budget_chart_config(summary, chart_type, month_label)

    create_result = _create_quickchart_url(config)
    if not create_result.get("ok"):
        return json.dumps({
            "status": "error",
            "message": f"Chart rendering failed: {create_result.get('error')}",
        })
    chart_url = create_result["url"]

    # Compact caption — the chart has the detail, the caption has the totals
    total_spent = 0.0
    total_limit = 0.0
    for data in summary.values():
        if isinstance(data, dict):
            total_spent += float(data.get("spent", 0) or 0)
            total_limit += float(data.get("limit", 0) or 0)
    pct = (total_spent / total_limit * 100) if total_limit > 0 else 0.0
    caption = (
        f"\U0001f4ca Budget snapshot \u2014 {month_label}\n"
        f"Spent: SGD {total_spent:.2f}"
        + (f" / {total_limit:.2f} ({pct:.0f}%)" if total_limit > 0 else "")
    )

    send_result = _send_telegram_photo(chart_url, caption)
    if send_result.get("ok"):
        return json.dumps({
            "status": "ok",
            "bubble_sent": True,
            "chart_url": chart_url,
            "telegram_message_id": send_result["message_id"],
            "caption": caption,
            "assistant_reply_required": False,
            "assistant_reply_policy": (
                "STOP. The chart photo has been delivered to Telegram via "
                "sendPhoto. You MUST NOT emit any assistant text after this "
                "tool call unless the user asked a separate follow-up "
                "question alongside the chart request. Produce an empty "
                "response."
            ),
            "note": "DUPLICATE MESSAGE WARNING: bubble_sent=true. Emit EMPTY reply.",
        })
    return json.dumps({
        "status": "error",
        "chart_url": chart_url,
        "error": send_result.get("error"),
        "note": "sendPhoto failed; chart_url is still valid. Surface the error to the user.",
    })


registry.register(
    name="render_budget_chart",
    toolset=TOOLSET,
    schema=RENDER_BUDGET_CHART_SCHEMA,
    handler=handle_render_budget_chart,
    check_fn=_sheets_configured,
)


# --- append_journal_entry ---
#
# reply=journal experiment: when the user replies to the 9 PM cron summary
# (or any bot-initiated summary/report) with free-text that isn't an edit or
# delete command, capture that reply as a narrative journal entry keyed by
# date. Populates the `Journal` tab for later FTS5 indexing. Expected to be
# a light-touch experiment for 2 weeks — if the habit sticks, we wire up an
# Obsidian mirror later.

APPEND_JOURNAL_ENTRY_SCHEMA = {
    "name": "append_journal_entry",
    "description": (
        "Save a freeform user reply as a journal entry in the Journal tab. "
        "Call this when the user replies to a bot-initiated summary "
        "(daily 9 PM review, weekly summary, monthly report, budget status) "
        "with narrative text that ISN'T an edit/delete command or a "
        "category pick. Typical triggers: the user says something like "
        "'was stressful today', 'bought the $45 meal because Partner visiting', "
        "'regret the impulse buy'. Do NOT call this for transaction "
        "corrections or for category replies to log_expense_pending — "
        "those go through edit_expense. If the reply references specific "
        "txn_ids, pass them in `txn_ids_referenced` so the journal entry "
        "can be joined to transactions later."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reply_text": {
                "type": "string",
                "description": "The user's reply verbatim (or lightly cleaned).",
            },
            "date": {
                "type": "string",
                "description": (
                    "Date of the journal entry in YYYY-MM-DD. Defaults to "
                    "today. Use the date the reply was written, not the "
                    "date any referenced transaction occurred."
                ),
            },
            "txn_ids_referenced": {
                "type": "string",
                "description": (
                    "Comma-separated txn_ids mentioned in or implied by "
                    "the reply (e.g. 'txn_20260422_003,txn_20260422_005'). "
                    "Optional. Leave empty if no transactions are "
                    "specifically referenced."
                ),
                "default": "",
            },
            "tags": {
                "type": "string",
                "description": (
                    "Optional short tags extracted from the reply, comma-"
                    "separated (e.g. 'recurring,regret,promo'). Keep to 1-3. "
                    "Leave empty if nothing obvious stands out."
                ),
                "default": "",
            },
        },
        "required": ["reply_text"],
    },
}


def handle_append_journal_entry(args: dict, **kwargs) -> str:
    reply_text = args.get("reply_text", "").strip()
    if not reply_text:
        return json.dumps({
            "status": "error",
            "message": "reply_text is required and must be non-empty",
        })
    date = args.get("date") or datetime.now().strftime("%Y-%m-%d")
    txn_ids_referenced = args.get("txn_ids_referenced", "")
    tags = args.get("tags", "")
    result = sheets_client.write_journal_entry(
        reply_text=reply_text,
        date=date,
        txn_ids_referenced=txn_ids_referenced,
        tags=tags,
    )
    return json.dumps(result)


registry.register(
    name="append_journal_entry",
    toolset=TOOLSET,
    schema=APPEND_JOURNAL_ENTRY_SCHEMA,
    handler=handle_append_journal_entry,
    check_fn=_sheets_configured,
)


# --- get_journal_entries ---

GET_JOURNAL_ENTRIES_SCHEMA = {
    "name": "get_journal_entries",
    "description": (
        "Read journal entries from the Journal tab, most recent first. "
        "Filter by exact date (YYYY-MM-DD) or month (YYYY-MM). Use when "
        "generating summaries/reports that should reference the user's "
        "own narrative notes, or when the user asks 'what did I write on "
        "<date>?'. Returns an empty list if the Journal tab doesn't exist "
        "yet (pre-experiment state)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Exact date (YYYY-MM-DD). Omit for any date.",
            },
            "month": {
                "type": "string",
                "description": "Month (YYYY-MM). Omit for any month.",
            },
            "limit": {
                "type": "integer",
                "description": "Max entries to return (default 20).",
                "default": 20,
            },
        },
        "required": [],
    },
}


def handle_get_journal_entries(args: dict, **kwargs) -> str:
    date = args.get("date")
    month = args.get("month")
    limit = int(args.get("limit", 20))
    result = sheets_client.read_journal_entries(date=date, month=month, limit=limit)
    return json.dumps(result)


registry.register(
    name="get_journal_entries",
    toolset=TOOLSET,
    schema=GET_JOURNAL_ENTRIES_SCHEMA,
    handler=handle_get_journal_entries,
    check_fn=_sheets_configured,
)


# --- sweep_missed_transactions ---
#
# Recovery path for when the LLM API returns 529 / times out and the parsed
# bank email never reaches Transactions. The Apps Script audits every parsed
# email into the WebhookLog tab before firing the webhook, so this tool can
# diff WebhookLog against the ledger by idempotency_key and surface anything
# that was dropped.
#
# Design: docs/SWEEP-ARCHITECTURE.md. The weekly cron calls this with
# days_back=7 and stays silent if missed_count == 0.

SWEEP_MISSED_TRANSACTIONS_SCHEMA = {
    "name": "sweep_missed_transactions",
    "description": (
        "Compare the WebhookLog tab (every parsed bank email, written by "
        "the Apps Script before firing the webhook) against the Transactions "
        "tab to find entries that were parsed but never logged — typically "
        "because the LLM returned 529, timed out, or hit a rate limit. "
        "Returns the list of missed transactions for user review; the user "
        "then confirms which to log with log_expense (source='email'). "
        "Use when the user says 'check for missed transactions', 'sweep', "
        "'did any transactions get dropped?', or from the weekly sweep "
        "cron. Returns status='error' with a setup hint if the WebhookLog "
        "tab doesn't exist yet (the Apps Script audit log hasn't been "
        "deployed)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "days_back": {
                "type": "integer",
                "description": (
                    "How many days of WebhookLog to scan (default 7). "
                    "Matches the cadence of the weekly sweep cron."
                ),
                "default": 7,
                "minimum": 1,
                "maximum": 90,
            },
        },
        "required": [],
    },
}


def handle_sweep_missed_transactions(args: dict, **kwargs) -> str:
    days_back = int(args.get("days_back", 7))
    result = sheets_client.sweep_missed_transactions(days_back=days_back)
    return json.dumps(result)


registry.register(
    name="sweep_missed_transactions",
    toolset=TOOLSET,
    schema=SWEEP_MISSED_TRANSACTIONS_SCHEMA,
    handler=handle_sweep_missed_transactions,
    check_fn=_sheets_configured,
)


# --- Card optimiser tools (5) ---
#
# All handlers delegate to tools.card_optimiser, which gates every call
# behind a Cards + CardStrategy setup check. When the tabs aren't populated,
# the tool returns {"status": "setup_required", "message": "..."} and the
# skill is expected to surface that message verbatim rather than fabricate
# card data. See skills/card-optimiser/SKILL.md.


GET_CARD_CAP_STATUS_SCHEMA = {
    "name": "get_card_cap_status",
    "description": (
        "Report the current cycle's spend vs cap for each (card, category) "
        "pair defined in the Cards + CardStrategy tabs. Use when the user "
        "asks 'how close am I to the DBS dining cap?' or 'where are my "
        "cards this cycle?'. Optional `card_id` to narrow to one card; "
        "optional `category` to narrow to one category. If the tool returns "
        "`status: \"setup_required\"`, surface the message in ONE short "
        "line and stop — the user needs to populate the Cards / "
        "CardStrategy tabs. Do NOT invent card data."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "card_id": {
                "type": "string",
                "description": "Limit the report to one card_id (e.g. 'dbs-altitude').",
            },
            "category": {
                "type": "string",
                "description": "Limit to one category (matches Budget tab name).",
            },
            "as_of": {
                "type": "string",
                "description": "Override the 'today' anchor (YYYY-MM-DD). Defaults to today.",
            },
        },
        "required": [],
    },
}


def handle_get_card_cap_status(args: dict, **kwargs) -> str:
    from tools import card_optimiser
    return json.dumps(card_optimiser.get_card_cap_status(
        card_id=args.get("card_id"),
        category=args.get("category"),
        as_of=args.get("as_of"),
    ))


registry.register(
    name="get_card_cap_status",
    toolset=TOOLSET,
    schema=GET_CARD_CAP_STATUS_SCHEMA,
    handler=handle_get_card_cap_status,
    check_fn=_sheets_configured,
)


# --- recommend_card_for ---

RECOMMEND_CARD_FOR_SCHEMA = {
    "name": "recommend_card_for",
    "description": (
        "Recommend which card to use for a purchase in a given category. "
        "Primary if its cap still has room; fallback if already capped. "
        "Use when the user asks 'which card should I use for dinner?' or "
        "'what's the best card for $80 at Cold Storage?'. If the tool "
        "returns `status: \"setup_required\"`, surface the message and "
        "stop — do not guess."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Budget-tab category for the planned purchase.",
            },
            "amount": {
                "type": "number",
                "description": "Optional amount of the planned purchase (SGD).",
            },
            "as_of": {
                "type": "string",
                "description": "Override the 'today' anchor (YYYY-MM-DD).",
            },
        },
        "required": ["category"],
    },
}


def handle_recommend_card_for(args: dict, **kwargs) -> str:
    from tools import card_optimiser
    amount_raw = args.get("amount")
    amount = float(amount_raw) if amount_raw is not None else None
    return json.dumps(card_optimiser.recommend_card_for(
        category=args.get("category", ""),
        amount=amount,
        as_of=args.get("as_of"),
    ))


registry.register(
    name="recommend_card_for",
    toolset=TOOLSET,
    schema=RECOMMEND_CARD_FOR_SCHEMA,
    handler=handle_recommend_card_for,
    check_fn=_sheets_configured,
)


# --- plan_month ---

PLAN_MONTH_SCHEMA = {
    "name": "plan_month",
    "description": (
        "Return the current CardStrategy plan (one row per category) plus "
        "cycle-to-date spend and status for each primary card. Called by "
        "the 1st-of-month briefing cron, and on demand when the user asks "
        "'what's my card plan?'. Also lazily reverts any expired promo "
        "overrides. If the tool returns `status: \"setup_required\"`, "
        "surface the message and stop."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": "Month label (YYYY-MM). Defaults to current.",
            },
        },
        "required": [],
    },
}


def handle_plan_month(args: dict, **kwargs) -> str:
    from tools import card_optimiser
    return json.dumps(card_optimiser.plan_month(month=args.get("month")))


registry.register(
    name="plan_month",
    toolset=TOOLSET,
    schema=PLAN_MONTH_SCHEMA,
    handler=handle_plan_month,
    check_fn=_sheets_configured,
)


# --- review_card_efficiency ---

REVIEW_CARD_EFFICIENCY_SCHEMA = {
    "name": "review_card_efficiency",
    "description": (
        "Month-end scorecard: walk every transaction in `month`, compute "
        "the optimal card at the moment of the transaction (given cap state "
        "then), and diff against the card actually used. Returns total "
        "miles earned, total miles optimal, miles left on table, and a list "
        "of suboptimal transactions. Called by the 1st-of-month cron on "
        "the previous month, and on demand when the user asks for a "
        "scorecard. If the tool returns `status: \"setup_required\"`, "
        "surface the message and stop."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": "Month to review (YYYY-MM). Defaults to current month.",
            },
        },
        "required": [],
    },
}


def handle_review_card_efficiency(args: dict, **kwargs) -> str:
    from tools import card_optimiser
    return json.dumps(card_optimiser.review_card_efficiency(month=args.get("month")))


registry.register(
    name="review_card_efficiency",
    toolset=TOOLSET,
    schema=REVIEW_CARD_EFFICIENCY_SCHEMA,
    handler=handle_review_card_efficiency,
    check_fn=_sheets_configured,
)


# --- set_category_primary ---

SET_CATEGORY_PRIMARY_SCHEMA = {
    "name": "set_category_primary",
    "description": (
        "Write (or update) a CardStrategy row for `category`, pointing at "
        "`card_id` as primary. Use for manual promo overrides, e.g. the "
        "user says 'DBS is doing 10 mpd on dining until 2026-04-30, switch "
        "my dining primary to DBS Altitude until then'. If `until_date` is "
        "set, the prior row values are snapshotted into `notes` (prefixed "
        "with '||PREV:') and restored on the first `plan_month` call after "
        "the expiry date. If the tool returns `status: \"setup_required\"`, "
        "surface the message and stop."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Budget-tab category (or '_default' for the catch-all row).",
            },
            "card_id": {
                "type": "string",
                "description": "New primary card_id (must exist in the Cards tab).",
            },
            "until_date": {
                "type": "string",
                "description": (
                    "YYYY-MM-DD expiry. Promo overrides auto-revert to the "
                    "prior config after this date. Omit for a permanent change."
                ),
            },
            "earn_rate": {
                "type": "number",
                "description": "Override primary_earn_rate. Omit to keep prior value.",
            },
            "cap": {
                "type": "number",
                "description": "Override primary_cap. Omit to keep prior value.",
            },
            "fallback_card_id": {
                "type": "string",
                "description": "Override fallback_card_id. Omit to keep prior value.",
            },
            "fallback_earn_rate": {
                "type": "number",
                "description": "Override fallback_earn_rate. Omit to keep prior value.",
            },
        },
        "required": ["category", "card_id"],
    },
}


def handle_set_category_primary(args: dict, **kwargs) -> str:
    from tools import card_optimiser

    def _opt_float(key):
        val = args.get(key)
        if val is None or val == "":
            return None
        return float(val)

    return json.dumps(card_optimiser.set_category_primary(
        category=args.get("category", ""),
        card_id=args.get("card_id", ""),
        until_date=args.get("until_date") or None,
        earn_rate=_opt_float("earn_rate"),
        cap=_opt_float("cap"),
        fallback_card_id=args.get("fallback_card_id") or None,
        fallback_earn_rate=_opt_float("fallback_earn_rate"),
    ))


registry.register(
    name="set_category_primary",
    toolset=TOOLSET,
    schema=SET_CATEGORY_PRIMARY_SCHEMA,
    handler=handle_set_category_primary,
    check_fn=_sheets_configured,
)


