import os
from datetime import datetime
from functools import lru_cache

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

TRANSACTIONS_SHEET = "Transactions"
BUDGET_SHEET = "Budget"

MONTH_COLUMNS = {
    1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7,
    7: 8, 8: 9, 9: 10, 10: 11, 11: 12, 12: 13,
}


@lru_cache(maxsize=1)
def get_client():
    creds_path = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return gspread.authorize(creds)


def get_spreadsheet():
    sheet_id = os.environ["GSPREAD_SPREADSHEET_ID"]
    return get_client().open_by_key(sheet_id)


def append_transaction(date: str, merchant: str, amount: float, currency: str,
                       category: str, source: str = "email",
                       payment_method: str = "", notes: str = ""):
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    row = [date, merchant, amount, currency, category, source, payment_method, notes]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return {"status": "ok", "row": row}


def read_transactions(month: str | None = None) -> list[dict]:
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    records = ws.get_all_records()

    if month is None:
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


def get_spending_summary(month: str | None = None) -> dict:
    if month is None:
        month = datetime.now().strftime("%Y-%m")

    month_num = int(month.split("-")[1])
    transactions = read_transactions(month)
    budgets = {b["Category"]: b["Monthly Limit"] for b in read_budgets(month_num)}

    spending = {}
    for t in transactions:
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

    return summary
