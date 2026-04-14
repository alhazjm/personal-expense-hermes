"""
Custom Hermes tools for personal expense tracking via Google Sheets.

Registers three tools with the Hermes tool registry:
  - log_expense: Record a new transaction
  - update_budget: Change a category's monthly limit
  - get_remaining_budget: Check spending vs budget
"""

import os
import json
from datetime import datetime

from tools.registry import registry
from tools import sheets_client


TOOLSET = "expense_tracker"


def _sheets_configured() -> bool:
    return bool(
        os.environ.get("GSPREAD_SPREADSHEET_ID")
        and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    )


# --- log_expense ---

LOG_EXPENSE_SCHEMA = {
    "name": "log_expense",
    "description": (
        "Log a new expense transaction to the Google Sheet. "
        "Use this when a bank transaction is received or the user reports a purchase. "
        "The category should match one of the user's budget categories in the Budget sheet."
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
                "description": "Payment source (e.g. 'DBS/POSB card ending 5305', 'PayLah! Wallet')",
                "default": "",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes about the transaction",
                "default": "",
            },
        },
        "required": ["merchant", "amount", "category"],
    },
}


def handle_log_expense(merchant: str, amount: float, category: str,
                       currency: str = "SGD", date: str | None = None,
                       payment_method: str = "", notes: str = "",
                       **kwargs) -> str:
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    result = sheets_client.append_transaction(
        date=date,
        merchant=merchant,
        amount=amount,
        currency=currency,
        category=category,
        source="manual",
        payment_method=payment_method,
        notes=notes,
    )
    return json.dumps(result)


registry.register(
    name="log_expense",
    toolset=TOOLSET,
    schema=LOG_EXPENSE_SCHEMA,
    handler=handle_log_expense,
    check_fn=_sheets_configured,
)


# --- update_budget ---

UPDATE_BUDGET_SCHEMA = {
    "name": "update_budget",
    "description": (
        "Update the monthly spending limit for a budget category. "
        "Use when the user wants to change how much they allocate to a category."
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
        },
        "required": ["category", "monthly_limit"],
    },
}


def handle_update_budget(category: str, monthly_limit: float, **kwargs) -> str:
    result = sheets_client.update_budget_row(category, monthly_limit)
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


def handle_get_remaining_budget(category: str | None = None,
                                month: str | None = None, **kwargs) -> str:
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
