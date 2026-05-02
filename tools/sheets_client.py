import hashlib
import os
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

TRANSACTIONS_SHEET = "Transactions"
BUDGET_SHEET = "Budget"
MERCHANT_MAP_SHEET = "MerchantMap"
INSIGHTS_SHEET = "Insights"
JOURNAL_SHEET = "Journal"
WEBHOOK_LOG_SHEET = "WebhookLog"

# Placeholder category used by log_expense_pending. Rows with this category
# are excluded from spending aggregates (get_spending_summary,
# generate_spending_report, detect_subscription_creep) so they don't pollute
# totals until the user picks a real category.
PENDING_CATEGORY = "UNCATEGORIZED"


def _is_pending(row: dict) -> bool:
    """True if a transaction row is still awaiting user categorisation."""
    return str(row.get("Category", "")).strip().upper() == PENDING_CATEGORY

MONTH_COLUMNS = {
    1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7,
    7: 8, 8: 9, 9: 10, 10: 11, 11: 12, 12: 13,
}

# Canonical Transactions tab schema (1-indexed). Used for both fixed-column
# fallback writes and as the authoritative ordering when appending new rows.
# The runtime layout is detected from the live header row via
# `_get_column_index` so legacy 8-column sheets keep working until the user
# applies the schema migration documented in sheets-template/README.md.
TRANSACTION_COLUMNS = {
    "Date": 1,
    "Merchant": 2,
    "Amount": 3,
    "Currency": 4,
    "Category": 5,
    "Source": 6,
    "Payment Method": 7,
    "Notes": 8,
    "txn_id": 9,
    "telegram_message_id": 10,
    "idempotency_key": 11,
}

TXN_ID_PATTERN = re.compile(r"^txn_(\d{8})_(\d{3,})$")


@lru_cache(maxsize=1)
def get_client():
    sa_value = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    try:
        info = json.loads(sa_value)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    except (json.JSONDecodeError, ValueError):
        creds = Credentials.from_service_account_file(sa_value, scopes=SCOPES)
    return gspread.authorize(creds)


_TRANSIENT_GSPREAD_STATUSES = {429, 500, 502, 503, 504}


def _is_transient_gspread_error(exc: Exception) -> bool:
    """Return True if an exception looks like a transient Google Sheets
    outage worth retrying. gspread raises `APIError` with a `response`
    attribute on HTTP errors; we retry 429 and 5xx, fail fast on 4xx."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status in _TRANSIENT_GSPREAD_STATUSES


def get_spreadsheet():
    """Open the expense sheet. Retries transient 429 / 5xx errors with
    exponential backoff (0.5s, 1s, 2s) before surfacing the failure.

    Historical 503s from Google's fetch_sheet_metadata caused the agent
    to crash mid-cron even when the outage was measured in seconds; the
    retry absorbs those so the next tool call doesn't see the blip."""
    sheet_id = os.environ["GSPREAD_SPREADSHEET_ID"]
    last_exc = None
    for attempt in range(3):
        try:
            return get_client().open_by_key(sheet_id)
        except gspread.exceptions.APIError as exc:
            last_exc = exc
            if not _is_transient_gspread_error(exc) or attempt == 2:
                raise
            time.sleep(0.5 * (2 ** attempt))
    # Defensive — loop always either returns or raises above.
    raise last_exc  # type: ignore[misc]


def _get_column_index(ws, column_name: str) -> int | None:
    """Return the 1-indexed column position of `column_name` in the worksheet's
    header row, or None if the header doesn't contain that column. Lets the
    code tolerate sheets that haven't been migrated to the new schema yet."""
    header = ws.row_values(1)
    try:
        return header.index(column_name) + 1
    except ValueError:
        return None


def _generate_txn_id(ws, date_str: str) -> str:
    """Build the next `txn_<YYYYMMDD>_<NNN>` ID for the given date by scanning
    the existing txn_id column for that date prefix and incrementing the max
    sequence. Falls back to `txn_<YYYYMMDD>_001` if no prior IDs exist or if
    the txn_id column is absent."""
    compact_date = date_str.replace("-", "")
    prefix = f"txn_{compact_date}_"

    col_idx = _get_column_index(ws, "txn_id")
    if col_idx is None:
        return f"{prefix}001"

    existing_ids = ws.col_values(col_idx)[1:]  # skip header
    max_seq = 0
    for value in existing_ids:
        match = TXN_ID_PATTERN.match(value or "")
        if match and match.group(1) == compact_date:
            seq = int(match.group(2))
            if seq > max_seq:
                max_seq = seq

    return f"{prefix}{max_seq + 1:03d}"


def _compute_idempotency_key(date: str, merchant: str, amount: float,
                             payment_method: str) -> str:
    """Deterministic hash of a transaction's natural key fields.

    Used to prevent duplicate inserts when the same webhook fires twice
    or when network retries cause double-writes. The hash is truncated
    to 16 hex chars (64 bits) — collision-safe for a personal ledger.
    """
    raw = f"{date}|{merchant.strip().upper()}|{amount:.2f}|{payment_method.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _find_by_idempotency_key(ws, key: str) -> tuple[int, dict] | None:
    """Check if a row with the given idempotency_key already exists.

    Returns (row_number, row_dict) or None. Returns None gracefully if the
    idempotency_key column doesn't exist yet (pre-migration sheets)."""
    col_idx = _get_column_index(ws, "idempotency_key")
    if col_idx is None:
        return None

    cell = ws.find(key, in_column=col_idx)
    if cell is None:
        return None

    header = ws.row_values(1)
    row_values = ws.row_values(cell.row)
    row_values += [""] * (len(header) - len(row_values))
    row_dict = dict(zip(header, row_values))
    return (cell.row, row_dict)


def append_transaction(date: str, merchant: str, amount: float, currency: str,
                       category: str, source: str = "email",
                       payment_method: str = "", notes: str = ""):
    """Append a transaction to the Transactions tab.

    Generates a `txn_id` of the form `txn_<YYYYMMDD>_<NNN>` and returns it in
    the result so the caller can link a Telegram message_id afterwards via
    `link_telegram_message`.

    The `telegram_message_id` column is left empty here — it gets filled
    automatically by `handle_log_expense` after the confirmation bubble is
    sent via the Telegram Bot API.

    **Idempotency:** a deterministic hash of (date, merchant, amount,
    payment_method) is written to the `idempotency_key` column. If a row
    with the same key already exists, the function returns the existing
    `txn_id` with `status: "duplicate"` instead of double-inserting. This
    prevents duplicates when the same webhook fires twice or network retries
    cause re-sends.

    The row layout matches `TRANSACTION_COLUMNS`. If the live sheet still has
    the legacy 8-column layout (no txn_id / telegram_message_id /
    idempotency_key columns), those trailing values are simply written into
    nonexistent cells, which gspread silently extends — so the migration is
    forward-compatible.
    """
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)

    # Idempotency check: reject duplicate inserts
    idem_key = _compute_idempotency_key(date, merchant, amount, payment_method)
    existing = _find_by_idempotency_key(ws, idem_key)
    if existing:
        _, row_dict = existing
        return {
            "status": "duplicate",
            "txn_id": row_dict.get("txn_id", ""),
            "idempotency_key": idem_key,
            "message": "Duplicate transaction — already logged.",
        }

    txn_id = _generate_txn_id(ws, date)
    row = [
        date,
        merchant,
        amount,
        currency,
        category,
        source,
        payment_method,
        notes,
        txn_id,
        "",          # telegram_message_id — filled by link_telegram_message
        idem_key,    # idempotency_key — dedup on re-send
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return {"status": "ok", "row": row, "txn_id": txn_id, "idempotency_key": idem_key}


def read_transactions(month: str | None = None) -> list[dict]:
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    records = ws.get_all_records()

    if not month:
        month = datetime.now().strftime("%Y-%m")

    filtered = []
    for r in records:
        try:
            row_date = str(r.get("Date", ""))
            if row_date.startswith(month):
                filtered.append(r)
        except (ValueError, TypeError):
            continue
    return filtered


def read_budgets(month_num: int | None = None) -> list[dict]:
    """Read budget limits. Supports per-month columns (Jan=col2 ... Dec=col13)
    or a simple 2-column layout (Category | Monthly Limit)."""
    ws = get_spreadsheet().worksheet(BUDGET_SHEET)
    all_values = ws.get_all_values()

    if not all_values:
        return []

    header = all_values[0]

    if month_num is None:
        month_num = datetime.now().month

    # Detect layout: if header has month names or 12+ columns, it's per-month
    is_per_month = len(header) >= 13 or any(
        m in header[1].lower() for m in ["jan", "feb", "mar", "1", "2"]
    )

    budgets = []
    if is_per_month:
        col_idx = MONTH_COLUMNS.get(month_num, 2)
        for row in all_values[1:]:
            if not row[0].strip():
                continue
            try:
                limit = float(row[col_idx - 1]) if len(row) >= col_idx and row[col_idx - 1] else 0
            except (ValueError, IndexError):
                limit = 0
            budgets.append({"Category": row[0].strip(), "Monthly Limit": limit})
    else:
        for row in all_values[1:]:
            if not row[0].strip():
                continue
            try:
                limit = float(row[1]) if len(row) > 1 and row[1] else 0
            except ValueError:
                limit = 0
            budgets.append({"Category": row[0].strip(), "Monthly Limit": limit})

    return budgets


def update_budget_row(category: str, monthly_limit: float, month_num: int | None = None):
    """Update budget limit for a category. If per-month layout, updates the specific month column."""
    ws = get_spreadsheet().worksheet(BUDGET_SHEET)
    all_values = ws.get_all_values()
    header = all_values[0] if all_values else []

    if month_num is None:
        month_num = datetime.now().month

    cell = ws.find(category, in_column=1)
    if cell is None:
        raise ValueError(f"Category '{category}' not found in Budget sheet")

    is_per_month = len(header) >= 13
    if is_per_month:
        col_idx = MONTH_COLUMNS.get(month_num, 2)
    else:
        col_idx = 2

    ws.update_cell(cell.row, col_idx, monthly_limit)
    return {"status": "ok", "category": category, "month": month_num, "new_limit": monthly_limit}


def find_transaction_by_id(txn_id: str) -> tuple[int, dict] | None:
    """Find a transaction row by its canonical `txn_id`. This is the preferred
    lookup path for edit/delete since it's unambiguous. Returns (row_number,
    row_dict) or None. Returns None gracefully if the txn_id column doesn't
    exist on the live sheet (legacy layout) — callers should fall back to
    `find_transaction_row` in that case."""
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    col_idx = _get_column_index(ws, "txn_id")
    if col_idx is None:
        return None

    cell = ws.find(txn_id, in_column=col_idx)
    if cell is None:
        return None

    header = ws.row_values(1)
    row_values = ws.row_values(cell.row)
    # Pad row_values to header length so dict zip is complete
    row_values += [""] * (len(header) - len(row_values))
    row_dict = dict(zip(header, row_values))
    return (cell.row, row_dict)


def find_transaction_by_message_id(telegram_message_id: str) -> tuple[int, dict] | None:
    """Find a transaction row by the Telegram message_id of the bot's
    confirmation bubble. Used to resolve reply-to-message edits: when the user
    replies "that should be Groceries" to a confirmation, the inbound payload
    carries `reply_to_message_id`, which this function maps back to the
    original transaction row.

    Returns (row_number, row_dict) or None. Returns None if the column doesn't
    exist on the live sheet (legacy layout) or if no row matches."""
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    col_idx = _get_column_index(ws, "telegram_message_id")
    if col_idx is None:
        return None

    cell = ws.find(str(telegram_message_id), in_column=col_idx)
    if cell is None:
        return None

    header = ws.row_values(1)
    row_values = ws.row_values(cell.row)
    row_values += [""] * (len(header) - len(row_values))
    row_dict = dict(zip(header, row_values))
    return (cell.row, row_dict)


def find_transaction_row(merchant: str, amount: float, date: str | None = None) -> tuple[int, dict] | None:
    """Find a transaction row by merchant + amount (+ optional date).
    Returns (row_number, row_dict) or None. Row number is 1-indexed (Sheet rows).

    Legacy fallback used when the caller doesn't have a `txn_id` (e.g. for
    rows that pre-date the schema migration). Prefer `find_transaction_by_id`
    or `find_transaction_by_message_id` whenever possible."""
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    records = ws.get_all_records()

    # Search from bottom (most recent) to top
    for i in range(len(records) - 1, -1, -1):
        r = records[i]
        row_merchant = str(r.get("Merchant", "")).strip().lower()
        row_amount = float(r.get("Amount", 0))

        search = merchant.strip().lower()
        merchant_match = (
            len(search) >= 3 and (search in row_merchant or row_merchant in search)
        )
        if merchant_match and abs(row_amount - amount) < 0.01:
            if date and str(r.get("Date", "")) != date:
                continue
            # +2 because: row 1 is header, records are 0-indexed
            return (i + 2, r)
    return None


def edit_transaction(merchant: str, amount: float, date: str | None = None,
                     updates: dict | None = None,
                     txn_id: str | None = None) -> dict:
    """Edit fields of an existing transaction. `updates` is a dict of
    column_name → new_value.

    Lookup priority:
      1. `txn_id` (canonical, unambiguous)
      2. merchant + amount (+ optional date) — legacy fallback for rows that
         pre-date the schema migration

    Updates use live header lookup (`_get_column_index`) rather than the
    static `TRANSACTION_COLUMNS` map, so the function works against both the
    legacy 8-column layout and the new 10-column layout."""
    if not updates:
        return {"status": "error", "message": "No updates provided"}

    if txn_id:
        result = find_transaction_by_id(txn_id)
    else:
        result = find_transaction_row(merchant, amount, date)

    if result is None:
        identifier = f"txn_id={txn_id}" if txn_id else f"{merchant} ${amount}"
        return {"status": "error", "message": f"Transaction not found: {identifier}"}

    row_num, existing = result
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)

    changed = {}
    for field, value in updates.items():
        col_idx = _get_column_index(ws, field)
        if col_idx is None:
            # Fall back to canonical schema for known columns
            col_idx = TRANSACTION_COLUMNS.get(field)
        if col_idx:
            ws.update_cell(row_num, col_idx, value)
            changed[field] = {"from": existing.get(field, ""), "to": value}

    return {
        "status": "ok",
        "row": row_num,
        "txn_id": existing.get("txn_id", ""),
        "changes": changed,
    }


def delete_transaction(merchant: str, amount: float, date: str | None = None,
                       txn_id: str | None = None) -> dict:
    """Delete a transaction row.

    Lookup priority matches `edit_transaction`:
      1. `txn_id` (canonical, unambiguous)
      2. merchant + amount (+ optional date) — legacy fallback
    """
    if txn_id:
        result = find_transaction_by_id(txn_id)
    else:
        result = find_transaction_row(merchant, amount, date)

    if result is None:
        identifier = f"txn_id={txn_id}" if txn_id else f"{merchant} ${amount}"
        return {"status": "error", "message": f"Transaction not found: {identifier}"}

    row_num, existing = result
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    ws.delete_rows(row_num)
    return {"status": "ok", "deleted_row": row_num, "transaction": existing}


def get_last_transaction() -> tuple[int, dict] | None:
    """Return the most recent transaction row (bottom of the sheet).

    Returns (row_number, row_dict) or None if the sheet is empty.
    Used by undo_last_expense to delete the last logged transaction."""
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    records = ws.get_all_records()
    if not records:
        return None
    last = records[-1]
    row_num = len(records) + 1  # +1 for header row
    return (row_num, last)


def link_telegram_message(txn_id: str, telegram_message_id: str) -> dict:
    """Attach a Telegram message_id to an already-logged transaction.

    Called by the expense-tracker skill after it sends the confirmation
    bubble: `log_expense` returns a `txn_id`, the bot sends the bubble and
    receives a `message_id` back from Telegram, then this function records
    the link so future reply-to-message edits resolve correctly."""
    result = find_transaction_by_id(txn_id)
    if result is None:
        return {"status": "error", "message": f"Transaction not found: txn_id={txn_id}"}

    row_num, _ = result
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    col_idx = _get_column_index(ws, "telegram_message_id")
    if col_idx is None:
        return {
            "status": "error",
            "message": (
                "Sheet has no `telegram_message_id` column — "
                "apply the schema migration in sheets-template/README.md"
            ),
        }

    ws.update_cell(row_num, col_idx, str(telegram_message_id))
    return {
        "status": "ok",
        "txn_id": txn_id,
        "telegram_message_id": str(telegram_message_id),
        "row": row_num,
    }


def ensure_category_exists(category: str) -> dict:
    """Ensure a budget category row exists. Creates it with $0 limit if missing.
    Returns {"status": "exists"|"created", "category": str}."""
    ws = get_spreadsheet().worksheet(BUDGET_SHEET)
    all_values = ws.get_all_values()

    # Check if category already exists (case-insensitive)
    for row in all_values[1:]:
        if row and row[0].strip().lower() == category.strip().lower():
            return {"status": "exists", "category": category}

    # Determine layout: per-month (13+ cols) or simple (2 cols)
    header = all_values[0] if all_values else []
    is_per_month = len(header) >= 13

    if is_per_month:
        new_row = [category] + [0] * 12
    else:
        new_row = [category, 0]

    ws.append_row(new_row, value_input_option="USER_ENTERED")
    return {"status": "created", "category": category}


def get_spending_summary(month: str | None = None) -> dict:
    # `if not month` (not `is None`) so empty strings from LLM-constructed
    # tool calls also fall back to the current month. Previously crashed
    # with IndexError on `"".split("-")[1]`.
    if not month:
        month = datetime.now().strftime("%Y-%m")

    month_num = int(month.split("-")[1])
    transactions = read_transactions(month)
    budgets = {b["Category"]: b["Monthly Limit"] for b in read_budgets(month_num)}

    spending = {}
    pending_count = 0
    for t in transactions:
        if _is_pending(t):
            pending_count += 1
            continue
        cat = t.get("Category", "Miscellaneous")
        amt = float(t.get("Amount", 0))
        spending[cat] = spending.get(cat, 0) + amt

    summary = {}
    for cat, limit in budgets.items():
        spent = spending.get(cat, 0)
        summary[cat] = {
            "limit": limit,
            "spent": round(spent, 2),
            "remaining": round(limit - spent, 2),
            "percent_used": round((spent / limit) * 100, 1) if limit > 0 else 0,
        }

    unbudgeted = set(spending.keys()) - set(budgets.keys())
    for cat in unbudgeted:
        summary[cat] = {
            "limit": 0,
            "spent": round(spending[cat], 2),
            "remaining": 0,
            "percent_used": 100,
            "note": "No budget set for this category",
        }

    if pending_count:
        summary["_pending_review"] = {
            "count": pending_count,
            "note": (
                f"{pending_count} transaction(s) still UNCATEGORIZED — "
                "awaiting user pick via reply-to-ask-prompt. Not included "
                "in category totals."
            ),
        }

    return summary


def generate_spending_report(month: str | None = None) -> dict:
    """Build a detailed spending report for a given month.

    Returns category breakdown with budget comparisons, top merchants by
    spend, daily spending totals, and month-over-month comparison vs the
    prior month. Designed to give the LLM everything it needs to present
    a rich summary with citations (txn_ids, date ranges).
    """
    if not month:
        month = datetime.now().strftime("%Y-%m")

    month_num = int(month.split("-")[1])
    year = int(month.split("-")[0])

    # Current month data
    transactions = read_transactions(month)
    budgets = {b["Category"]: b["Monthly Limit"] for b in read_budgets(month_num)}

    # Prior month data for comparison
    prev_month_num = month_num - 1 if month_num > 1 else 12
    prev_year = year if month_num > 1 else year - 1
    prev_month_str = f"{prev_year}-{prev_month_num:02d}"
    prev_transactions = read_transactions(prev_month_str)

    # Category breakdown. Pending rows are collected separately so they
    # don't pollute totals but stay surfaced to the LLM for a review nudge.
    category_spend: dict[str, float] = defaultdict(float)
    category_txns: dict[str, list[str]] = defaultdict(list)
    pending_rows = []
    for t in transactions:
        if _is_pending(t):
            pending_rows.append({
                "txn_id": t.get("txn_id", ""),
                "merchant": t.get("Merchant", ""),
                "amount": float(t.get("Amount", 0)),
                "date": str(t.get("Date", "")),
            })
            continue
        cat = t.get("Category", "Miscellaneous")
        amt = float(t.get("Amount", 0))
        category_spend[cat] += amt
        txn_id = t.get("txn_id", "")
        if txn_id:
            category_txns[cat].append(txn_id)

    categories = []
    all_cats = set(budgets.keys()) | set(category_spend.keys())
    for cat in sorted(all_cats):
        limit = budgets.get(cat, 0)
        spent = round(category_spend.get(cat, 0), 2)
        categories.append({
            "category": cat,
            "limit": limit,
            "spent": spent,
            "remaining": round(limit - spent, 2),
            "percent_used": round((spent / limit) * 100, 1) if limit > 0 else 0,
            "txn_ids": category_txns.get(cat, []),
        })
    categories.sort(key=lambda c: c["spent"], reverse=True)

    # Top merchants by total spend (pending rows excluded — same reasoning
    # as the category breakdown above).
    merchant_spend: dict[str, float] = defaultdict(float)
    merchant_count: dict[str, int] = defaultdict(int)
    for t in transactions:
        if _is_pending(t):
            continue
        m = t.get("Merchant", "Unknown")
        merchant_spend[m] += float(t.get("Amount", 0))
        merchant_count[m] += 1

    top_merchants = sorted(
        [{"merchant": m, "total": round(s, 2), "count": merchant_count[m]}
         for m, s in merchant_spend.items()],
        key=lambda x: x["total"],
        reverse=True,
    )[:10]

    # Daily spending (excludes pending rows — they have no real category
    # yet and shouldn't skew a "spent per day" chart).
    daily_spend: dict[str, float] = defaultdict(float)
    for t in transactions:
        if _is_pending(t):
            continue
        d = str(t.get("Date", ""))
        daily_spend[d] += float(t.get("Amount", 0))
    daily = [{"date": d, "total": round(s, 2)}
             for d, s in sorted(daily_spend.items())]

    # Month-over-month comparison (pending excluded both sides)
    prev_total = sum(
        float(t.get("Amount", 0)) for t in prev_transactions if not _is_pending(t)
    )
    curr_total = sum(
        float(t.get("Amount", 0)) for t in transactions if not _is_pending(t)
    )

    prev_cat_spend: dict[str, float] = defaultdict(float)
    for t in prev_transactions:
        if _is_pending(t):
            continue
        cat = t.get("Category", "Miscellaneous")
        prev_cat_spend[cat] += float(t.get("Amount", 0))

    mom_changes = []
    for cat in sorted(all_cats):
        prev = round(prev_cat_spend.get(cat, 0), 2)
        curr = round(category_spend.get(cat, 0), 2)
        if prev > 0 or curr > 0:
            change_pct = round(((curr - prev) / prev) * 100, 1) if prev > 0 else 0
            mom_changes.append({
                "category": cat,
                "previous": prev,
                "current": curr,
                "change_pct": change_pct,
            })

    total_budget = sum(budgets.values())

    return {
        "month": month,
        "total_spent": round(curr_total, 2),
        "total_budget": round(total_budget, 2),
        "transaction_count": len(transactions),
        "categories": categories,
        "top_merchants": top_merchants,
        "daily_spending": daily,
        "month_over_month": {
            "previous_month": prev_month_str,
            "previous_total": round(prev_total, 2),
            "current_total": round(curr_total, 2),
            "change_pct": round(((curr_total - prev_total) / prev_total) * 100, 1) if prev_total > 0 else 0,
            "category_changes": mom_changes,
        },
        "pending_review": {
            "count": len(pending_rows),
            "rows": pending_rows,
        },
    }


# --- Insights store ---
#
# Tier-4 semantic memory: derived facts persisted to an Insights tab in the
# Google Sheet. Weekly/monthly summaries write short facts here so future
# reports can build on prior conclusions instead of recomputing from scratch.

def write_insight(insight: str, category: str = "general",
                  month: str | None = None) -> dict:
    """Persist a derived insight to the Insights tab.

    Each insight is a short fact (e.g. "User overspent Dining by 22% in
    week 14", "Grab rides trend up on rainy weeks"). The LLM writes these
    after generating summaries; future report runs read them first.
    """
    ss = get_spreadsheet()
    try:
        ws = ss.worksheet(INSIGHTS_SHEET)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=INSIGHTS_SHEET, rows=200, cols=4)
        ws.append_row(
            ["date", "category", "month", "insight"],
            value_input_option="USER_ENTERED",
        )

    if not month:
        month = datetime.now().strftime("%Y-%m")

    today = datetime.now().strftime("%Y-%m-%d")
    ws.append_row(
        [today, category, month, insight],
        value_input_option="USER_ENTERED",
    )
    return {"status": "ok", "insight": insight, "category": category, "month": month}


def get_insights(month: str | None = None, category: str | None = None,
                 limit: int = 20) -> list[dict]:
    """Read insights from the Insights tab, optionally filtered by month
    and/or category. Returns most recent first, up to `limit` entries."""
    try:
        ws = get_spreadsheet().worksheet(INSIGHTS_SHEET)
    except gspread.WorksheetNotFound:
        return []

    records = ws.get_all_records()

    filtered = []
    for r in records:
        if month and str(r.get("month", "")) != month:
            continue
        if category and str(r.get("category", "")).lower() != category.lower():
            continue
        filtered.append({
            "date": str(r.get("date", "")),
            "category": str(r.get("category", "")),
            "month": str(r.get("month", "")),
            "insight": str(r.get("insight", "")),
        })

    # Most recent first
    filtered.reverse()
    return filtered[:limit]


def detect_subscription_creep(months_back: int = 3) -> dict:
    """Scan transactions for recurring subscription patterns.

    Groups transactions by merchant across recent months, identifies charges
    that repeat monthly with a consistent amount (variance < 30%), and flags:
      - **active** — recurring charge still appearing this month
      - **new** — first appeared this month (exists in current month only
        among the analysis window, but is flagged so the user can confirm)
      - **price_change** — same merchant, amount changed >5% vs prior month
      - **possibly_cancelled** — appeared in prior months but not this month

    Excludes `backfill` rows (imported statements would otherwise trigger
    false "new subscription" alerts).

    Returns a structured report the LLM can summarise for the user.
    """
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    records = ws.get_all_records()

    now = datetime.now()
    current_month = now.strftime("%Y-%m")

    # Build list of month strings in the analysis window
    months = []
    for i in range(months_back):
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        months.append(f"{y}-{m:02d}")
    months_set = set(months)

    # Filter to relevant months, exclude backfill and pending rows.
    # Pending (UNCATEGORIZED) rows have no real category yet so they can't
    # be a confirmed recurring charge; skipping them avoids spurious
    # "possibly_cancelled" flags while the user hasn't replied to the
    # ask-prompt.
    relevant = []
    for r in records:
        source = str(r.get("Source", "")).strip().lower()
        if source == "backfill":
            continue
        if _is_pending(r):
            continue
        date_str = str(r.get("Date", ""))
        month = date_str[:7]
        if month in months_set:
            relevant.append(r)

    # Group by merchant (case-insensitive) per month
    merchant_months: dict[str, dict[str, list[float]]] = {}
    merchant_original: dict[str, str] = {}
    merchant_categories: dict[str, str] = {}

    for r in relevant:
        merchant = str(r.get("Merchant", "")).strip()
        merchant_key = merchant.lower()
        amount = float(r.get("Amount", 0))
        date_str = str(r.get("Date", ""))
        month = date_str[:7]
        category = str(r.get("Category", ""))

        if merchant_key not in merchant_months:
            merchant_months[merchant_key] = {}
            merchant_original[merchant_key] = merchant
        merchant_months[merchant_key].setdefault(month, []).append(amount)
        merchant_categories[merchant_key] = category

    # Identify subscriptions
    subscriptions = []
    prev_month = months[1] if len(months) > 1 else None

    for merchant_key, month_data in merchant_months.items():
        # Need at least 2 months to call something recurring
        if len(month_data) < 2:
            continue

        # Each month should have exactly 1 charge (subscriptions are 1x/month)
        # Allow at most 2 charges per month (e.g. mid-month price change)
        if any(len(amounts) > 2 for amounts in month_data.values()):
            continue

        # Check amount consistency — variance < 30% of mean
        all_amounts = [a for amounts in month_data.values() for a in amounts]
        avg_amount = sum(all_amounts) / len(all_amounts)
        if avg_amount < 0.50:
            continue  # skip trivial amounts
        max_variance = max(abs(a - avg_amount) / avg_amount for a in all_amounts)
        if max_variance > 0.30:
            continue

        months_seen = sorted(month_data.keys())
        latest_month = months_seen[-1]
        latest_amount = month_data[latest_month][-1]

        # Determine status
        status = "active"
        price_change_pct = 0.0

        if current_month not in month_data:
            status = "possibly_cancelled"
        elif prev_month and prev_month in month_data:
            prev_avg = sum(month_data[prev_month]) / len(month_data[prev_month])
            if prev_avg > 0:
                price_change_pct = ((latest_amount - prev_avg) / prev_avg) * 100
                if abs(price_change_pct) > 5:
                    status = "price_change"

        subscriptions.append({
            "merchant": merchant_original[merchant_key],
            "category": merchant_categories[merchant_key],
            "avg_amount": round(avg_amount, 2),
            "months_seen": months_seen,
            "latest_amount": round(latest_amount, 2),
            "status": status,
            "price_change_pct": round(price_change_pct, 1),
        })

    subscriptions.sort(key=lambda s: s["avg_amount"], reverse=True)

    active = [s for s in subscriptions if s["status"] in ("active", "price_change")]
    total = sum(s["latest_amount"] for s in active)

    return {
        "subscriptions": subscriptions,
        "total_monthly_subscriptions": round(total, 2),
        "new_this_month": [s for s in subscriptions if s["status"] == "new"],
        "price_changes": [s for s in subscriptions if s["status"] == "price_change"],
        "possibly_cancelled": [s for s in subscriptions if s["status"] == "possibly_cancelled"],
        "months_analyzed": sorted(months),
    }


# --- Journal: reply=journal narrative store ---
#
# Free-text replies the user sends to cron summary messages (9 PM daily
# review etc.) land here, keyed by date. The schema is deliberately thin:
# the point is to accumulate narrative content as a join key for later
# FTS5 indexing — not to impose structure up-front. Columns:
#   date | reply_text | txn_ids_referenced | tags
# `txn_ids_referenced` is comma-separated; `tags` is optional free-text.

def write_journal_entry(reply_text: str, date: str | None = None,
                        txn_ids_referenced: str = "",
                        tags: str = "") -> dict:
    """Append a journal entry. Creates the Journal tab on first call."""
    ss = get_spreadsheet()
    try:
        ws = ss.worksheet(JOURNAL_SHEET)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=JOURNAL_SHEET, rows=500, cols=4)
        ws.append_row(
            ["date", "reply_text", "txn_ids_referenced", "tags"],
            value_input_option="USER_ENTERED",
        )

    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    ws.append_row(
        [date, reply_text, txn_ids_referenced, tags],
        value_input_option="USER_ENTERED",
    )
    return {
        "status": "ok",
        "date": date,
        "reply_text": reply_text,
        "txn_ids_referenced": txn_ids_referenced,
        "tags": tags,
    }


def read_journal_entries(date: str | None = None, month: str | None = None,
                         limit: int = 20) -> list[dict]:
    """Read journal entries, most recent first. Returns empty list if the
    Journal tab doesn't exist yet (so callers don't special-case pre-
    experiment state)."""
    try:
        ws = get_spreadsheet().worksheet(JOURNAL_SHEET)
    except gspread.WorksheetNotFound:
        return []

    records = ws.get_all_records()
    filtered = []
    for r in records:
        row_date = str(r.get("date", ""))
        if date and row_date != date:
            continue
        if month and not row_date.startswith(month):
            continue
        filtered.append({
            "date": row_date,
            "reply_text": str(r.get("reply_text", "")),
            "txn_ids_referenced": str(r.get("txn_ids_referenced", "")),
            "tags": str(r.get("tags", "")),
        })

    filtered.reverse()
    return filtered[:limit]


# --- MerchantMap: learned merchant → category mappings ---
#
# The MerchantMap tab lets the agent persist categorisation decisions across
# sessions. When the user corrects "Shopee → Pet Litter," writing a row here
# means the next Shopee transaction can be auto-categorised without re-asking.
#
# Schema: merchant_pattern | category | created_at
# Lookup is case-insensitive substring match against the incoming merchant
# string, with longest-pattern-wins precedence so more specific patterns
# (e.g. "GRABFOOD") beat shorter ones (e.g. "GRAB").

def read_merchant_mappings() -> list[dict]:
    """Read all rows from the MerchantMap tab. Returns an empty list if the
    tab doesn't exist yet (so callers don't have to special-case the
    pre-migration state)."""
    try:
        ws = get_spreadsheet().worksheet(MERCHANT_MAP_SHEET)
    except gspread.WorksheetNotFound:
        return []

    records = ws.get_all_records()
    mappings = []
    for r in records:
        pattern = str(r.get("merchant_pattern", "")).strip()
        category = str(r.get("category", "")).strip()
        if pattern and category:
            mappings.append({
                "merchant_pattern": pattern,
                "category": category,
                "created_at": str(r.get("created_at", "")),
            })
    return mappings


def lookup_merchant_category(merchant: str) -> dict | None:
    """Return the best MerchantMap match for a merchant string, or None.

    Match rule: case-insensitive substring; longest matching pattern wins.
    A return of None means the agent should fall back to its own judgement
    (and ask the user if uncertain)."""
    if not merchant:
        return None

    merchant_lower = merchant.lower()
    best_match = None
    best_length = 0

    for mapping in read_merchant_mappings():
        pattern_lower = mapping["merchant_pattern"].lower()
        if pattern_lower and pattern_lower in merchant_lower:
            if len(pattern_lower) > best_length:
                best_match = mapping
                best_length = len(pattern_lower)

    return best_match


def add_merchant_mapping(merchant_pattern: str, category: str) -> dict:
    """Append (or update) a row in the MerchantMap tab. If a row with the
    same `merchant_pattern` (case-insensitive) already exists, its category
    is updated in place rather than duplicated.

    Creates the MerchantMap tab on first call if it doesn't exist."""
    ss = get_spreadsheet()
    try:
        ws = ss.worksheet(MERCHANT_MAP_SHEET)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=MERCHANT_MAP_SHEET, rows=100, cols=3)
        ws.append_row(
            ["merchant_pattern", "category", "created_at"],
            value_input_option="USER_ENTERED",
        )

    pattern_clean = merchant_pattern.strip()
    today = datetime.now().strftime("%Y-%m-%d")

    # Check for existing row with same pattern (case-insensitive)
    all_values = ws.get_all_values()
    for idx, row in enumerate(all_values[1:], start=2):
        if row and row[0].strip().lower() == pattern_clean.lower():
            ws.update_cell(idx, 2, category)
            return {
                "status": "updated",
                "merchant_pattern": pattern_clean,
                "category": category,
                "row": idx,
            }

    ws.append_row(
        [pattern_clean, category, today],
        value_input_option="USER_ENTERED",
    )
    return {
        "status": "created",
        "merchant_pattern": pattern_clean,
        "category": category,
        "created_at": today,
    }


# --- WebhookLog sweep: find bank emails that never made it to Transactions ---
#
# The Apps Script writes every parsed bank email to the WebhookLog tab BEFORE
# firing the webhook, so even if the LLM call returns 529 / times out, the
# parsed payload is durable. This function diffs WebhookLog against the
# Transactions ledger by idempotency_key and returns anything that's missing
# so the user can review and log it.
#
# Idempotency key parity (JS side ↔ Python side) is critical here; mismatch
# = false positives in the missed list. The JS implementation is in
# apps-script/Code.gs::computeIdempotencyKey, verified by the testIdempotency
# KeyParity() Apps Script function against the pins in the Python unit test.

def sweep_missed_transactions(days_back: int = 7) -> dict:
    """Compare WebhookLog against Transactions to surface parsed bank emails
    that never made it into the ledger (e.g. because the LLM returned 529).

    Returns a dict with:
      - status: "ok" | "error"
      - total_webhook_logs: count of audit rows within the window
      - missed_count: count of audit rows with no matching Transactions row
      - missed: list of the unmatched audit rows (each with date, merchant,
        amount, currency, payment_method, idempotency_key, webhook_status)

    Rows already marked `matched == "yes"` in WebhookLog are excluded (the
    user has already resolved them in a prior sweep). Rows with `date` older
    than `days_back` are excluded to keep the response small.

    Returns {"status": "error"} if the WebhookLog tab doesn't exist — that's
    the signal the Apps Script audit log hasn't been deployed yet.
    """
    ss = get_spreadsheet()

    try:
        wl = ss.worksheet(WEBHOOK_LOG_SHEET)
    except gspread.WorksheetNotFound:
        return {
            "status": "error",
            "message": (
                "WebhookLog tab not found. Deploy the updated Apps Script "
                "first (apps-script/Code.gs) so bank emails get audited "
                "before the webhook fires."
            ),
        }

    ws = ss.worksheet(TRANSACTIONS_SHEET)

    # Collect idempotency keys already present in Transactions. Header
    # lookup rather than a fixed column index so this works against both
    # the legacy 8-column layout (no idem key → empty set, everything
    # looks missed) and the v4 11-column layout.
    idem_col_idx = _get_column_index(ws, "idempotency_key")
    txn_keys: set[str] = set()
    if idem_col_idx is not None:
        for value in ws.col_values(idem_col_idx)[1:]:  # skip header
            value = (value or "").strip()
            if value:
                txn_keys.add(value)

    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    log_records = wl.get_all_records()

    missed: list[dict] = []
    in_window = 0
    for row in log_records:
        row_date = str(row.get("date", ""))
        if row_date < cutoff:
            continue
        in_window += 1

        # Skip rows the user already reconciled in a previous sweep.
        if str(row.get("matched", "")).strip().lower() == "yes":
            continue

        key = str(row.get("idempotency_key", "")).strip()
        if not key:
            continue
        if key in txn_keys:
            continue

        missed.append({
            "date": row_date,
            "bank": str(row.get("bank", "")),
            "type": str(row.get("type", "")),
            "merchant": str(row.get("merchant", "")),
            "amount": row.get("amount", ""),
            "currency": str(row.get("currency", "")),
            "payment_method": str(row.get("payment_method", "")),
            "idempotency_key": key,
            "webhook_status": str(row.get("webhook_status", "")),
            "timestamp": str(row.get("timestamp", "")),
        })

    return {
        "status": "ok",
        "days_back": days_back,
        "total_webhook_logs": in_window,
        "missed_count": len(missed),
        "missed": missed,
    }
