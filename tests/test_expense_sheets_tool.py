"""Tests for the expense sheets tool handlers."""

import json
import os
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def mock_env():
    with patch.dict(os.environ, {
        "GSPREAD_SPREADSHEET_ID": "test-sheet-id",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "/tmp/fake-sa.json",
    }):
        yield


@pytest.fixture
def mock_sheets_client():
    with patch("tools.expense_sheets_tool.sheets_client") as mock:
        mock.append_transaction.return_value = {
            "status": "ok",
            "row": ["2026-04-14", "Starbucks", 15.50, "SGD", "Food & Dining", "manual", ""],
        }
        mock.update_budget_row.return_value = {
            "status": "ok",
            "category": "Food & Dining",
            "new_limit": 900,
        }
        mock.get_spending_summary.return_value = {
            "Food & Dining": {"limit": 800, "spent": 345.50, "remaining": 454.50, "percent_used": 43.2},
            "Transport": {"limit": 300, "spent": 120.00, "remaining": 180.00, "percent_used": 40.0},
        }
        yield mock


class TestLogExpense:
    def test_logs_with_all_fields(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        result = json.loads(handle_log_expense(
            merchant="Starbucks",
            amount=15.50,
            category="Food & Dining",
            currency="SGD",
            date="2026-04-14",
        ))
        assert result["status"] == "ok"
        mock_sheets_client.append_transaction.assert_called_once_with(
            date="2026-04-14",
            merchant="Starbucks",
            amount=15.50,
            currency="SGD",
            category="Food & Dining",
            source="manual",
            notes="",
        )

    def test_defaults_to_today(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        result = json.loads(handle_log_expense(
            merchant="Grab",
            amount=8.00,
            category="Transport",
        ))
        assert result["status"] == "ok"
        call_kwargs = mock_sheets_client.append_transaction.call_args[1]
        assert len(call_kwargs["date"]) == 10  # YYYY-MM-DD format


class TestUpdateBudget:
    def test_updates_category_limit(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_update_budget

        result = json.loads(handle_update_budget(
            category="Food & Dining",
            monthly_limit=900,
        ))
        assert result["status"] == "ok"
        assert result["new_limit"] == 900


class TestGetRemainingBudget:
    def test_returns_all_categories(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_get_remaining_budget

        result = json.loads(handle_get_remaining_budget())
        assert "Food & Dining" in result
        assert "Transport" in result
        assert result["Food & Dining"]["remaining"] == 454.50

    def test_returns_single_category(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_get_remaining_budget

        result = json.loads(handle_get_remaining_budget(category="Transport"))
        assert "Transport" in result
        assert len(result) == 1

    def test_returns_error_for_unknown_category(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_get_remaining_budget

        result = json.loads(handle_get_remaining_budget(category="Nonexistent"))
        assert "error" in result
