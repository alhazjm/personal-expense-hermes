"""Tests for the expense sheets tool handlers."""

import json
import os
import urllib.error
from datetime import datetime, timedelta
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
            "row": [
                "2026-04-14", "Starbucks", 15.50, "SGD", "Food & Dining",
                "manual", "", "", "txn_20260414_001", "",
            ],
            "txn_id": "txn_20260414_001",
        }
        mock.ensure_category_exists.return_value = {"status": "exists", "category": "Food & Dining"}
        mock.update_budget_row.return_value = {
            "status": "ok",
            "category": "Food & Dining",
            "month": 4,
            "new_limit": 900,
        }
        mock.get_spending_summary.return_value = {
            "Food & Dining": {"limit": 800, "spent": 345.50, "remaining": 454.50, "percent_used": 43.2},
            "Transport": {"limit": 300, "spent": 120.00, "remaining": 180.00, "percent_used": 40.0},
        }
        mock.edit_transaction.return_value = {
            "status": "ok",
            "row": 5,
            "txn_id": "txn_20260414_001",
            "changes": {"Category": {"from": "Groceries", "to": "Pet Litter"}},
        }
        mock.delete_transaction.return_value = {
            "status": "ok",
            "deleted_row": 5,
            "transaction": {"Merchant": "SHOPEE SINGAPORE MP", "Amount": 55.90},
        }
        mock.find_transaction_row.return_value = (5, {
            "Date": "2026-04-14",
            "Merchant": "SHOPEE SINGAPORE MP",
            "Amount": 55.90,
            "Currency": "SGD",
            "Category": "Groceries",
        })
        mock.find_transaction_by_id.return_value = (5, {
            "Date": "2026-04-14",
            "Merchant": "SHOPEE SINGAPORE MP",
            "Amount": 55.90,
            "Currency": "SGD",
            "Category": "Groceries",
            "txn_id": "txn_20260414_001",
            "telegram_message_id": "4421",
        })
        mock.find_transaction_by_message_id.return_value = (5, {
            "Date": "2026-04-14",
            "Merchant": "SHOPEE SINGAPORE MP",
            "Amount": 55.90,
            "Currency": "SGD",
            "Category": "Groceries",
            "txn_id": "txn_20260414_001",
            "telegram_message_id": "4421",
        })
        mock.link_telegram_message.return_value = {
            "status": "ok",
            "txn_id": "txn_20260414_001",
            "telegram_message_id": "4421",
            "row": 5,
        }
        mock.lookup_merchant_category.return_value = None
        mock.add_merchant_mapping.return_value = {
            "status": "created",
            "merchant_pattern": "SHOPEE SINGAPORE",
            "category": "Pet Litter",
            "created_at": "2026-04-15",
        }
        yield mock


class TestLogExpense:
    def test_logs_with_all_fields(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        result = json.loads(handle_log_expense({
            "merchant": "Starbucks",
            "amount": 15.50,
            "category": "Food & Dining",
            "currency": "SGD",
            "date": "2026-04-14",
        }))
        assert result["status"] == "ok"
        assert result["txn_id"] == "txn_20260414_001"
        mock_sheets_client.append_transaction.assert_called_once_with(
            date="2026-04-14",
            merchant="Starbucks",
            amount=15.50,
            currency="SGD",
            category="Food & Dining",
            source="manual",
            payment_method="",
            notes="",
        )

    def test_defaults_to_today(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        result = json.loads(handle_log_expense({
            "merchant": "Grab",
            "amount": 8.00,
            "category": "Transport",
        }))
        assert result["status"] == "ok"
        call_kwargs = mock_sheets_client.append_transaction.call_args[1]
        assert len(call_kwargs["date"]) == 10  # YYYY-MM-DD format

    def test_new_category_created_flag(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        mock_sheets_client.ensure_category_exists.return_value = {
            "status": "created",
            "category": "New Category",
        }
        result = json.loads(handle_log_expense({
            "merchant": "Some Store",
            "amount": 20.00,
            "category": "New Category",
        }))
        assert result["status"] == "ok"
        assert result.get("new_category_created") is True
        assert result.get("new_category") == "New Category"

    def test_no_flag_for_existing_category(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        result = json.loads(handle_log_expense({
            "merchant": "Starbucks",
            "amount": 5.00,
            "category": "Food & Dining",
        }))
        assert "new_category_created" not in result

    def test_source_defaults_to_manual(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        handle_log_expense({
            "merchant": "Starbucks",
            "amount": 5.00,
            "category": "Food & Dining",
        })
        call_kwargs = mock_sheets_client.append_transaction.call_args[1]
        assert call_kwargs["source"] == "manual"

    def test_source_email_for_webhook(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        handle_log_expense({
            "merchant": "GrabFood",
            "amount": 18.50,
            "category": "Personal - Food & Drinks",
            "source": "email",
        })
        call_kwargs = mock_sheets_client.append_transaction.call_args[1]
        assert call_kwargs["source"] == "email"

    def test_source_backfill(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        handle_log_expense({
            "merchant": "Sephora",
            "amount": 65.00,
            "category": "Partner Hair, Skincare, Pads",
            "source": "backfill",
        })
        call_kwargs = mock_sheets_client.append_transaction.call_args[1]
        assert call_kwargs["source"] == "backfill"

    def test_returns_txn_id(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        result = json.loads(handle_log_expense({
            "merchant": "Starbucks",
            "amount": 15.50,
            "category": "Food & Dining",
        }))
        assert "txn_id" in result
        assert result["txn_id"].startswith("txn_")

    def test_auto_sends_bubble_and_links(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        fake_send = {"ok": True, "message_id": "7777"}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fake-token"}):
            with patch("tools.expense_sheets_tool._send_telegram_bubble", return_value=fake_send):
                result = json.loads(handle_log_expense({
                    "merchant": "Cold Storage",
                    "amount": 2.50,
                    "category": "Groceries",
                }))
        assert result["bubble_sent"] is True
        assert result["telegram_message_id"] == "7777"
        assert result["linked"] is True
        mock_sheets_client.link_telegram_message.assert_called_once_with(
            "txn_20260414_001", "7777"
        )

    def test_auto_send_failure_still_logs(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        fake_send = {"ok": False, "error": "Bot token invalid"}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fake-token"}):
            with patch("tools.expense_sheets_tool._send_telegram_bubble", return_value=fake_send):
                result = json.loads(handle_log_expense({
                    "merchant": "Cold Storage",
                    "amount": 2.50,
                    "category": "Groceries",
                }))
        assert result["status"] == "ok"
        assert result["bubble_sent"] is False
        assert "bubble_error" in result
        mock_sheets_client.link_telegram_message.assert_not_called()

    def test_no_token_skips_send(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}, clear=False):
            with patch("tools.expense_sheets_tool._send_telegram_bubble") as mock_send:
                result = json.loads(handle_log_expense({
                    "merchant": "Starbucks",
                    "amount": 5.00,
                    "category": "Food & Dining",
                }))
        mock_send.assert_not_called()
        assert "bubble_sent" not in result


class TestUpdateBudget:
    def test_updates_category_limit(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_update_budget

        result = json.loads(handle_update_budget({
            "category": "Food & Dining",
            "monthly_limit": 900,
        }))
        assert result["status"] == "ok"
        assert result["new_limit"] == 900
        mock_sheets_client.update_budget_row.assert_called_once_with(
            "Food & Dining", 900.0, month_num=None
        )

    def test_updates_specific_month(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_update_budget

        handle_update_budget({
            "category": "Food & Dining",
            "monthly_limit": 500,
            "month": 6,
        })
        mock_sheets_client.update_budget_row.assert_called_once_with(
            "Food & Dining", 500.0, month_num=6
        )


class TestGetRemainingBudget:
    def test_returns_all_categories(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_get_remaining_budget

        result = json.loads(handle_get_remaining_budget({}))
        assert "Food & Dining" in result
        assert "Transport" in result
        assert result["Food & Dining"]["remaining"] == 454.50

    def test_returns_single_category(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_get_remaining_budget

        result = json.loads(handle_get_remaining_budget({"category": "Transport"}))
        assert "Transport" in result
        assert len(result) == 1

    def test_returns_error_for_unknown_category(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_get_remaining_budget

        result = json.loads(handle_get_remaining_budget({"category": "Nonexistent"}))
        assert "error" in result


class TestEditExpense:
    def test_edit_by_txn_id(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_edit_expense

        result = json.loads(handle_edit_expense({
            "txn_id": "txn_20260414_001",
            "new_category": "Pet Litter",
        }))
        assert result["status"] == "ok"
        mock_sheets_client.edit_transaction.assert_called_once_with(
            "", 0.0, None, {"Category": "Pet Litter"}, txn_id="txn_20260414_001"
        )

    def test_edit_category_legacy_lookup(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_edit_expense

        result = json.loads(handle_edit_expense({
            "merchant": "SHOPEE SINGAPORE MP",
            "amount": 55.90,
            "new_category": "Pet Litter",
        }))
        assert result["status"] == "ok"
        mock_sheets_client.edit_transaction.assert_called_once_with(
            "SHOPEE SINGAPORE MP", 55.90, None, {"Category": "Pet Litter"}, txn_id=None
        )

    def test_edit_merchant(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_edit_expense

        handle_edit_expense({
            "merchant": "SHOPEE SINGAPORE MP",
            "amount": 55.90,
            "new_merchant": "Pet Litter - Shopee",
        })
        mock_sheets_client.edit_transaction.assert_called_once_with(
            "SHOPEE SINGAPORE MP", 55.90, None, {"Merchant": "Pet Litter - Shopee"}, txn_id=None
        )

    def test_edit_notes(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_edit_expense

        handle_edit_expense({
            "merchant": "Grab",
            "amount": 8.00,
            "new_notes": "work trip",
        })
        mock_sheets_client.edit_transaction.assert_called_once_with(
            "Grab", 8.0, None, {"Notes": "work trip"}, txn_id=None
        )

    def test_edit_with_date(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_edit_expense

        handle_edit_expense({
            "merchant": "Grab",
            "amount": 8.00,
            "date": "2026-04-10",
            "new_category": "Transport",
        })
        mock_sheets_client.edit_transaction.assert_called_once_with(
            "Grab", 8.0, "2026-04-10", {"Category": "Transport"}, txn_id=None
        )

    def test_error_when_no_lookup_keys(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_edit_expense

        result = json.loads(handle_edit_expense({"new_category": "Transport"}))
        assert result["status"] == "error"
        mock_sheets_client.edit_transaction.assert_not_called()


class TestDeleteExpense:
    def test_delete_by_txn_id(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_delete_expense

        result = json.loads(handle_delete_expense({
            "txn_id": "txn_20260414_001",
        }))
        assert result["status"] == "ok"
        mock_sheets_client.delete_transaction.assert_called_once_with(
            "", 0.0, None, txn_id="txn_20260414_001"
        )

    def test_delete_legacy_lookup(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_delete_expense

        result = json.loads(handle_delete_expense({
            "merchant": "SHOPEE SINGAPORE MP",
            "amount": 55.90,
        }))
        assert result["status"] == "ok"
        assert result["deleted_row"] == 5
        mock_sheets_client.delete_transaction.assert_called_once_with(
            "SHOPEE SINGAPORE MP", 55.90, None, txn_id=None
        )

    def test_delete_with_date(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_delete_expense

        handle_delete_expense({
            "merchant": "Grab",
            "amount": 8.00,
            "date": "2026-04-10",
        })
        mock_sheets_client.delete_transaction.assert_called_once_with(
            "Grab", 8.0, "2026-04-10", txn_id=None
        )

    def test_error_when_no_lookup_keys(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_delete_expense

        result = json.loads(handle_delete_expense({}))
        assert result["status"] == "error"
        mock_sheets_client.delete_transaction.assert_not_called()


class TestLinkTelegramMessage:
    def test_links_message_id(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_link_telegram_message

        result = json.loads(handle_link_telegram_message({
            "txn_id": "txn_20260414_001",
            "telegram_message_id": "4421",
        }))
        assert result["status"] == "ok"
        assert result["txn_id"] == "txn_20260414_001"
        assert result["telegram_message_id"] == "4421"
        mock_sheets_client.link_telegram_message.assert_called_once_with(
            "txn_20260414_001", "4421"
        )

    def test_coerces_message_id_to_string(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_link_telegram_message

        handle_link_telegram_message({
            "txn_id": "txn_20260414_001",
            "telegram_message_id": 4421,  # int from Telegram API
        })
        mock_sheets_client.link_telegram_message.assert_called_once_with(
            "txn_20260414_001", "4421"
        )


class TestGetTransactionByMessageId:
    def test_returns_transaction(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_get_transaction_by_message_id

        result = json.loads(handle_get_transaction_by_message_id({
            "telegram_message_id": "4421",
        }))
        assert result["status"] == "ok"
        assert result["transaction"]["txn_id"] == "txn_20260414_001"
        assert result["row"] == 5
        mock_sheets_client.find_transaction_by_message_id.assert_called_once_with("4421")

    def test_returns_not_found(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_get_transaction_by_message_id

        mock_sheets_client.find_transaction_by_message_id.return_value = None
        result = json.loads(handle_get_transaction_by_message_id({
            "telegram_message_id": "9999",
        }))
        assert result["status"] == "not_found"


class TestLookupMerchantCategory:
    def test_no_match(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_lookup_merchant_category

        mock_sheets_client.lookup_merchant_category.return_value = None
        result = json.loads(handle_lookup_merchant_category({
            "merchant": "SOME NEW PLACE",
        }))
        assert result["status"] == "no_match"

    def test_match(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_lookup_merchant_category

        mock_sheets_client.lookup_merchant_category.return_value = {
            "merchant_pattern": "GRABFOOD",
            "category": "Personal - Food & Drinks",
            "created_at": "2026-04-10",
        }
        result = json.loads(handle_lookup_merchant_category({
            "merchant": "GRABFOOD SG",
        }))
        assert result["status"] == "match"
        assert result["mapping"]["category"] == "Personal - Food & Drinks"


class TestLearnMerchantMapping:
    def test_creates_mapping(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_learn_merchant_mapping

        result = json.loads(handle_learn_merchant_mapping({
            "merchant_pattern": "SHOPEE SINGAPORE",
            "category": "Pet Litter",
        }))
        assert result["status"] == "created"
        mock_sheets_client.add_merchant_mapping.assert_called_once_with(
            "SHOPEE SINGAPORE", "Pet Litter"
        )


class TestRetryLink:
    """Tests for _retry_link exponential backoff logic."""

    def test_succeeds_on_first_attempt(self, mock_sheets_client):
        from tools.expense_sheets_tool import _retry_link

        mock_sheets_client.link_telegram_message.return_value = {
            "status": "ok",
            "txn_id": "txn_20260416_001",
            "telegram_message_id": "5555",
            "row": 3,
        }
        result = _retry_link("txn_20260416_001", "5555")
        assert result["status"] == "ok"
        assert mock_sheets_client.link_telegram_message.call_count == 1

    @patch("tools.expense_sheets_tool.time.sleep")
    def test_retries_on_exception(self, mock_sleep, mock_sheets_client):
        from tools.expense_sheets_tool import _retry_link

        mock_sheets_client.link_telegram_message.side_effect = [
            Exception("429 Rate limit"),
            {"status": "ok", "txn_id": "txn_20260416_001",
             "telegram_message_id": "5555", "row": 3},
        ]
        result = _retry_link("txn_20260416_001", "5555")
        assert result["status"] == "ok"
        assert mock_sheets_client.link_telegram_message.call_count == 2
        mock_sleep.assert_called_once_with(1)  # 2^0 = 1s

    @patch("tools.expense_sheets_tool.time.sleep")
    def test_gives_up_after_max_attempts(self, mock_sleep, mock_sheets_client):
        from tools.expense_sheets_tool import _retry_link

        mock_sheets_client.link_telegram_message.side_effect = Exception("429 Rate limit")
        result = _retry_link("txn_20260416_001", "5555", max_attempts=3)
        assert result["status"] == "error"
        assert "3 attempts" in result["message"]
        assert mock_sheets_client.link_telegram_message.call_count == 3
        assert mock_sleep.call_count == 2  # sleeps between attempts 1-2 and 2-3

    @patch("tools.expense_sheets_tool.time.sleep")
    def test_exponential_backoff_timing(self, mock_sleep, mock_sheets_client):
        from tools.expense_sheets_tool import _retry_link

        mock_sheets_client.link_telegram_message.side_effect = Exception("429")
        _retry_link("txn_20260416_001", "5555", max_attempts=3)
        assert mock_sleep.call_args_list == [
            ((1,),),   # 2^0
            ((2,),),   # 2^1
        ]

    def test_returns_non_ok_status_without_retry(self, mock_sheets_client):
        """Non-exception failures (e.g. missing column) should not be retried."""
        from tools.expense_sheets_tool import _retry_link

        mock_sheets_client.link_telegram_message.return_value = {
            "status": "error",
            "message": "Sheet has no telegram_message_id column",
        }
        result = _retry_link("txn_20260416_001", "5555")
        assert result["status"] == "error"
        assert mock_sheets_client.link_telegram_message.call_count == 1


class TestLogExpenseDuplicate:
    """Tests for duplicate transaction detection in handle_log_expense."""

    def test_duplicate_skips_bubble_and_returns_early(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        mock_sheets_client.append_transaction.return_value = {
            "status": "duplicate",
            "txn_id": "txn_20260416_001",
            "idempotency_key": "abc123",
            "message": "Duplicate transaction — already logged.",
        }
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fake-token"}):
            with patch("tools.expense_sheets_tool._send_telegram_bubble") as mock_send:
                result = json.loads(handle_log_expense({
                    "merchant": "Starbucks",
                    "amount": 15.50,
                    "category": "Food & Dining",
                }))
        assert result["status"] == "duplicate"
        assert "Duplicate transaction" in result["note"]
        mock_send.assert_not_called()
        mock_sheets_client.ensure_category_exists.assert_not_called()

    def test_duplicate_returns_existing_txn_id(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense

        mock_sheets_client.append_transaction.return_value = {
            "status": "duplicate",
            "txn_id": "txn_20260416_002",
            "idempotency_key": "def456",
            "message": "Duplicate transaction — already logged.",
        }
        result = json.loads(handle_log_expense({
            "merchant": "GrabFood",
            "amount": 18.50,
            "category": "Personal - Food & Drinks",
        }))
        assert result["txn_id"] == "txn_20260416_002"


class TestSubstringMatching:
    """Tests for find_transaction_row substring matching logic."""

    def _make_records(self):
        return [
            {"Merchant": "SHOPEE SINGAPORE MP", "Amount": 55.90, "Date": "2026-04-14",
             "Currency": "SGD", "Category": "Groceries"},
            {"Merchant": "GRAB", "Amount": 8.00, "Date": "2026-04-13",
             "Currency": "SGD", "Category": "Transport"},
            {"Merchant": "STARBUCKS", "Amount": 7.50, "Date": "2026-04-12",
             "Currency": "SGD", "Category": "Food & Dining"},
        ]

    def test_exact_match(self):
        from tools.sheets_client import find_transaction_row

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = self._make_records()
            mock_ss.return_value.worksheet.return_value = ws

            result = find_transaction_row("SHOPEE SINGAPORE MP", 55.90)
            assert result is not None
            assert result[1]["Merchant"] == "SHOPEE SINGAPORE MP"

    def test_partial_prefix_match(self):
        from tools.sheets_client import find_transaction_row

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = self._make_records()
            mock_ss.return_value.worksheet.return_value = ws

            result = find_transaction_row("shopee", 55.90)
            assert result is not None
            assert result[1]["Merchant"] == "SHOPEE SINGAPORE MP"

    def test_partial_substring_match(self):
        from tools.sheets_client import find_transaction_row

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = self._make_records()
            mock_ss.return_value.worksheet.return_value = ws

            result = find_transaction_row("SINGAPORE", 55.90)
            assert result is not None
            assert result[1]["Merchant"] == "SHOPEE SINGAPORE MP"

    def test_short_search_no_match(self):
        """Search terms under 3 chars should not match."""
        from tools.sheets_client import find_transaction_row

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = self._make_records()
            mock_ss.return_value.worksheet.return_value = ws

            result = find_transaction_row("sh", 55.90)
            assert result is None

    def test_no_match_wrong_amount(self):
        from tools.sheets_client import find_transaction_row

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = self._make_records()
            mock_ss.return_value.worksheet.return_value = ws

            result = find_transaction_row("shopee", 99.99)
            assert result is None

    def test_date_filter(self):
        from tools.sheets_client import find_transaction_row

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = self._make_records()
            mock_ss.return_value.worksheet.return_value = ws

            result = find_transaction_row("shopee", 55.90, date="2026-04-99")
            assert result is None


class TestTxnIdGeneration:
    """Tests for _generate_txn_id sequence logic."""

    def test_first_id_for_date(self):
        from tools.sheets_client import _generate_txn_id

        ws = MagicMock()
        ws.row_values.return_value = [
            "Date", "Merchant", "Amount", "Currency", "Category",
            "Source", "Payment Method", "Notes", "txn_id", "telegram_message_id",
        ]
        ws.col_values.return_value = ["txn_id"]  # header only, no rows yet

        assert _generate_txn_id(ws, "2026-04-15") == "txn_20260415_001"

    def test_increments_within_same_date(self):
        from tools.sheets_client import _generate_txn_id

        ws = MagicMock()
        ws.row_values.return_value = [
            "Date", "Merchant", "Amount", "Currency", "Category",
            "Source", "Payment Method", "Notes", "txn_id", "telegram_message_id",
        ]
        ws.col_values.return_value = [
            "txn_id",
            "txn_20260415_001",
            "txn_20260415_002",
            "txn_20260414_007",
        ]

        assert _generate_txn_id(ws, "2026-04-15") == "txn_20260415_003"

    def test_independent_per_date(self):
        from tools.sheets_client import _generate_txn_id

        ws = MagicMock()
        ws.row_values.return_value = [
            "Date", "Merchant", "Amount", "Currency", "Category",
            "Source", "Payment Method", "Notes", "txn_id", "telegram_message_id",
        ]
        ws.col_values.return_value = [
            "txn_id",
            "txn_20260414_001",
            "txn_20260414_002",
        ]

        assert _generate_txn_id(ws, "2026-04-15") == "txn_20260415_001"

    def test_ignores_legacy_ids(self):
        from tools.sheets_client import _generate_txn_id

        ws = MagicMock()
        ws.row_values.return_value = [
            "Date", "Merchant", "Amount", "Currency", "Category",
            "Source", "Payment Method", "Notes", "txn_id", "telegram_message_id",
        ]
        ws.col_values.return_value = [
            "txn_id",
            "txn_legacy_001",
            "txn_legacy_002",
        ]

        # Legacy IDs don't match the date pattern, so the new sequence starts at 001
        assert _generate_txn_id(ws, "2026-04-15") == "txn_20260415_001"

    def test_fallback_when_no_txn_id_column(self):
        from tools.sheets_client import _generate_txn_id

        ws = MagicMock()
        ws.row_values.return_value = [
            "Date", "Merchant", "Amount", "Currency", "Category",
            "Source", "Payment Method", "Notes",  # legacy 8-column layout
        ]

        assert _generate_txn_id(ws, "2026-04-15") == "txn_20260415_001"
        ws.col_values.assert_not_called()


class TestFormatBubble:
    """Tests for the _format_bubble helper."""

    def test_basic_format(self):
        from tools.expense_sheets_tool import _format_bubble

        result = _format_bubble("Cold Storage", 2.50, "SGD", "Groceries", "", "txn_20260416_001")
        assert "SGD 2.50" in result
        assert "Cold Storage" in result
        assert "Groceries" in result
        assert "txn_20260416_001" in result

    def test_includes_payment_method(self):
        from tools.expense_sheets_tool import _format_bubble

        result = _format_bubble("NTUC", 45.00, "SGD", "Groceries",
                                "DBS card •1234", "txn_20260416_002")
        assert "DBS card •1234" in result

    def test_omits_empty_payment_method(self):
        from tools.expense_sheets_tool import _format_bubble

        result = _format_bubble("Grab", 8.00, "SGD", "Transport", "", "txn_20260416_003")
        lines = result.strip().split("\n")
        assert len(lines) == 2  # line1 + txn_id only


class TestSendTelegramBubble:
    """Tests for the _send_telegram_bubble helper."""

    def test_sends_message_and_returns_id(self):
        from tools.expense_sheets_tool import _send_telegram_bubble

        fake_response = json.dumps({
            "ok": True,
            "result": {"message_id": 12345},
        }).encode("utf-8")

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "fake-token",
            "TELEGRAM_ALLOWED_USERS": "99999",
        }):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.read.return_value = fake_response
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = mock_resp

                result = _send_telegram_bubble("Hello!")
                assert result["ok"] is True
                assert result["message_id"] == "12345"

    def test_no_token_returns_error(self):
        from tools.expense_sheets_tool import _send_telegram_bubble

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}, clear=False):
            result = _send_telegram_bubble("Hello!")
            assert result["ok"] is False
            assert "TELEGRAM_BOT_TOKEN" in result["error"]

    def test_no_chat_id_returns_error(self):
        from tools.expense_sheets_tool import _send_telegram_bubble

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "fake-token",
            "TELEGRAM_ALLOWED_USERS": "",
        }):
            result = _send_telegram_bubble("Hello!")
            assert result["ok"] is False
            assert "chat_id" in result["error"]

    def test_explicit_chat_id_overrides_env(self):
        from tools.expense_sheets_tool import _send_telegram_bubble

        fake_response = json.dumps({
            "ok": True,
            "result": {"message_id": 999},
        }).encode("utf-8")

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "fake-token",
            "TELEGRAM_ALLOWED_USERS": "11111",
        }):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.read.return_value = fake_response
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = mock_resp

                result = _send_telegram_bubble("Hello!", chat_id="22222")
                assert result["ok"] is True

                req = mock_urlopen.call_args[0][0]
                body = json.loads(req.data.decode("utf-8"))
                assert body["chat_id"] == "22222"

    def test_comma_separated_users_takes_first(self):
        from tools.expense_sheets_tool import _send_telegram_bubble

        fake_response = json.dumps({
            "ok": True,
            "result": {"message_id": 1},
        }).encode("utf-8")

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "fake-token",
            "TELEGRAM_ALLOWED_USERS": "11111,22222,33333",
        }):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.read.return_value = fake_response
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = mock_resp

                _send_telegram_bubble("Hello!")
                req = mock_urlopen.call_args[0][0]
                body = json.loads(req.data.decode("utf-8"))
                assert body["chat_id"] == "11111"

    def test_network_error_returns_error(self):
        from tools.expense_sheets_tool import _send_telegram_bubble

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "fake-token",
            "TELEGRAM_ALLOWED_USERS": "99999",
        }):
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
                result = _send_telegram_bubble("Hello!")
                assert result["ok"] is False
                assert "Connection refused" in result["error"]


class TestLookupMerchantCategoryUnit:
    """Tests for sheets_client.lookup_merchant_category matching logic."""

    def test_longest_match_wins(self):
        from tools import sheets_client

        with patch.object(sheets_client, "read_merchant_mappings") as mock_read:
            mock_read.return_value = [
                {"merchant_pattern": "GRAB", "category": "Personal - Travel", "created_at": "2026-04-01"},
                {"merchant_pattern": "GRABFOOD", "category": "Personal - Food & Drinks", "created_at": "2026-04-02"},
            ]

            result = sheets_client.lookup_merchant_category("GRABFOOD SG PTE LTD")
            assert result is not None
            assert result["category"] == "Personal - Food & Drinks"

    def test_case_insensitive(self):
        from tools import sheets_client

        with patch.object(sheets_client, "read_merchant_mappings") as mock_read:
            mock_read.return_value = [
                {"merchant_pattern": "ntuc", "category": "Groceries", "created_at": "2026-04-01"},
            ]

            result = sheets_client.lookup_merchant_category("NTUC FAIRPRICE")
            assert result is not None
            assert result["category"] == "Groceries"

    def test_no_match_returns_none(self):
        from tools import sheets_client

        with patch.object(sheets_client, "read_merchant_mappings") as mock_read:
            mock_read.return_value = [
                {"merchant_pattern": "GRABFOOD", "category": "Personal - Food & Drinks", "created_at": "2026-04-01"},
            ]

            result = sheets_client.lookup_merchant_category("STARBUCKS")
            assert result is None

    def test_empty_merchant_returns_none(self):
        from tools import sheets_client

        result = sheets_client.lookup_merchant_category("")
        assert result is None


class TestIdempotencyKey:
    """Tests for _compute_idempotency_key and _find_by_idempotency_key."""

    def test_deterministic_hash(self):
        from tools.sheets_client import _compute_idempotency_key

        key1 = _compute_idempotency_key("2026-04-16", "Starbucks", 5.50, "DBS card •1234")
        key2 = _compute_idempotency_key("2026-04-16", "Starbucks", 5.50, "DBS card •1234")
        assert key1 == key2
        assert len(key1) == 16  # truncated sha256

    def test_different_inputs_different_keys(self):
        from tools.sheets_client import _compute_idempotency_key

        key1 = _compute_idempotency_key("2026-04-16", "Starbucks", 5.50, "DBS card •1234")
        key2 = _compute_idempotency_key("2026-04-16", "Starbucks", 6.00, "DBS card •1234")
        assert key1 != key2

    def test_case_insensitive_merchant(self):
        from tools.sheets_client import _compute_idempotency_key

        key1 = _compute_idempotency_key("2026-04-16", "starbucks", 5.50, "")
        key2 = _compute_idempotency_key("2026-04-16", "STARBUCKS", 5.50, "")
        assert key1 == key2

    def test_strips_whitespace(self):
        from tools.sheets_client import _compute_idempotency_key

        key1 = _compute_idempotency_key("2026-04-16", "  Starbucks  ", 5.50, " DBS ")
        key2 = _compute_idempotency_key("2026-04-16", "Starbucks", 5.50, "DBS")
        assert key1 == key2

    def test_find_returns_none_when_column_missing(self):
        from tools.sheets_client import _find_by_idempotency_key

        ws = MagicMock()
        ws.row_values.return_value = [
            "Date", "Merchant", "Amount", "Currency", "Category",
            "Source", "Payment Method", "Notes", "txn_id", "telegram_message_id",
        ]  # no idempotency_key column
        result = _find_by_idempotency_key(ws, "abc123")
        assert result is None

    def test_find_returns_none_when_key_not_found(self):
        from tools.sheets_client import _find_by_idempotency_key

        ws = MagicMock()
        ws.row_values.return_value = [
            "Date", "Merchant", "Amount", "Currency", "Category",
            "Source", "Payment Method", "Notes", "txn_id",
            "telegram_message_id", "idempotency_key",
        ]
        ws.find.return_value = None
        result = _find_by_idempotency_key(ws, "abc123")
        assert result is None

    def test_find_returns_row_when_key_matches(self):
        from tools.sheets_client import _find_by_idempotency_key

        ws = MagicMock()
        header = [
            "Date", "Merchant", "Amount", "Currency", "Category",
            "Source", "Payment Method", "Notes", "txn_id",
            "telegram_message_id", "idempotency_key",
        ]
        ws.row_values.side_effect = [
            header,  # _get_column_index
            header,  # header for dict zip
            [  # the matching row data
                "2026-04-16", "Starbucks", "5.50", "SGD", "Food & Dining",
                "email", "DBS card •1234", "", "txn_20260416_001", "", "abc123",
            ],
        ]
        cell = MagicMock()
        cell.row = 3
        ws.find.return_value = cell

        result = _find_by_idempotency_key(ws, "abc123")
        assert result is not None
        row_num, row_dict = result
        assert row_num == 3
        assert row_dict["txn_id"] == "txn_20260416_001"
        assert row_dict["idempotency_key"] == "abc123"


class TestAppendTransactionDedup:
    """Tests for duplicate detection in append_transaction."""

    def test_duplicate_returns_existing_txn(self):
        from tools.sheets_client import append_transaction

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            mock_ss.return_value.worksheet.return_value = ws
            header = [
                "Date", "Merchant", "Amount", "Currency", "Category",
                "Source", "Payment Method", "Notes", "txn_id",
                "telegram_message_id", "idempotency_key",
            ]
            ws.row_values.side_effect = [
                header,  # _find_by_idempotency_key → _get_column_index
                header,  # _find_by_idempotency_key → header for dict zip
                [  # _find_by_idempotency_key → row data
                    "2026-04-16", "Starbucks", "5.50", "SGD", "Food & Dining",
                    "email", "", "", "txn_20260416_001", "", "whatever",
                ],
            ]
            cell = MagicMock()
            cell.row = 3
            ws.find.return_value = cell

            result = append_transaction(
                "2026-04-16", "Starbucks", 5.50, "SGD", "Food & Dining",
            )
            assert result["status"] == "duplicate"
            assert result["txn_id"] == "txn_20260416_001"
            ws.append_row.assert_not_called()

    def test_new_transaction_appends_with_key(self):
        from tools.sheets_client import append_transaction

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            mock_ss.return_value.worksheet.return_value = ws
            header = [
                "Date", "Merchant", "Amount", "Currency", "Category",
                "Source", "Payment Method", "Notes", "txn_id",
                "telegram_message_id", "idempotency_key",
            ]
            ws.row_values.side_effect = [
                header,  # _find_by_idempotency_key → _get_column_index
                header,  # _generate_txn_id → _get_column_index
            ]
            ws.find.return_value = None  # no duplicate
            ws.col_values.return_value = ["txn_id"]  # header only

            result = append_transaction(
                "2026-04-16", "Starbucks", 5.50, "SGD", "Food & Dining",
            )
            assert result["status"] == "ok"
            assert result["txn_id"] == "txn_20260416_001"
            assert "idempotency_key" in result
            assert len(result["idempotency_key"]) == 16
            ws.append_row.assert_called_once()
            # Verify the row includes the idempotency key as the 11th element
            appended_row = ws.append_row.call_args[0][0]
            assert len(appended_row) == 11
            assert appended_row[10] == result["idempotency_key"]

    def test_no_idem_column_skips_dedup_still_appends(self):
        """When the idempotency_key column doesn't exist (legacy sheet),
        dedup is skipped and the row is appended normally."""
        from tools.sheets_client import append_transaction

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            mock_ss.return_value.worksheet.return_value = ws
            legacy_header = [
                "Date", "Merchant", "Amount", "Currency", "Category",
                "Source", "Payment Method", "Notes", "txn_id",
                "telegram_message_id",
            ]  # no idempotency_key
            ws.row_values.side_effect = [
                legacy_header,  # _find_by_idempotency_key → _get_column_index → None
                legacy_header,  # _generate_txn_id → _get_column_index
            ]
            ws.col_values.return_value = ["txn_id"]

            result = append_transaction(
                "2026-04-16", "Starbucks", 5.50, "SGD", "Food & Dining",
            )
            assert result["status"] == "ok"
            ws.append_row.assert_called_once()


class TestDetectSubscriptionCreep:
    """Tests for sheets_client.detect_subscription_creep."""

    def _make_records(self, entries):
        """Build transaction records from shorthand tuples.
        Each entry: (date, merchant, amount, category, source)
        """
        return [
            {
                "Date": d, "Merchant": m, "Amount": a, "Currency": "SGD",
                "Category": c, "Source": s, "Payment Method": "", "Notes": "",
                "txn_id": f"txn_{d.replace('-', '')}_{i:03d}",
                "telegram_message_id": "", "idempotency_key": "",
            }
            for i, (d, m, a, c, s) in enumerate(entries, start=1)
        ]

    @patch("tools.sheets_client.datetime")
    def test_identifies_recurring_subscription(self, mock_dt):
        from tools.sheets_client import detect_subscription_creep

        mock_dt.now.return_value = datetime(2026, 4, 16)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        records = self._make_records([
            ("2026-02-15", "APPLE.COM/BILL", 3.98, "iCloud", "email"),
            ("2026-03-15", "APPLE.COM/BILL", 3.98, "iCloud", "email"),
            ("2026-04-15", "APPLE.COM/BILL", 3.98, "iCloud", "email"),
            ("2026-02-10", "GRAB", 25.00, "Transport", "email"),
            ("2026-04-11", "GRAB", 8.50, "Transport", "email"),
        ])

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = records
            mock_ss.return_value.worksheet.return_value = ws

            result = detect_subscription_creep(months_back=3)

        assert len(result["subscriptions"]) == 1
        sub = result["subscriptions"][0]
        assert sub["merchant"] == "APPLE.COM/BILL"
        assert sub["avg_amount"] == 3.98
        assert sub["status"] == "active"
        assert len(sub["months_seen"]) == 3

    @patch("tools.sheets_client.datetime")
    def test_detects_price_change(self, mock_dt):
        from tools.sheets_client import detect_subscription_creep

        mock_dt.now.return_value = datetime(2026, 4, 16)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        records = self._make_records([
            ("2026-02-01", "NETFLIX", 15.98, "Entertainment", "email"),
            ("2026-03-01", "NETFLIX", 15.98, "Entertainment", "email"),
            ("2026-04-01", "NETFLIX", 18.98, "Entertainment", "email"),
        ])

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = records
            mock_ss.return_value.worksheet.return_value = ws

            result = detect_subscription_creep(months_back=3)

        subs = result["subscriptions"]
        assert len(subs) == 1
        assert subs[0]["status"] == "price_change"
        assert subs[0]["price_change_pct"] > 0

    @patch("tools.sheets_client.datetime")
    def test_detects_possibly_cancelled(self, mock_dt):
        from tools.sheets_client import detect_subscription_creep

        mock_dt.now.return_value = datetime(2026, 4, 16)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        records = self._make_records([
            ("2026-02-01", "DISNEY PLUS", 11.98, "Entertainment", "email"),
            ("2026-03-01", "DISNEY PLUS", 11.98, "Entertainment", "email"),
            # No April charge
        ])

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = records
            mock_ss.return_value.worksheet.return_value = ws

            result = detect_subscription_creep(months_back=3)

        assert len(result["possibly_cancelled"]) == 1
        assert result["possibly_cancelled"][0]["merchant"] == "DISNEY PLUS"

    @patch("tools.sheets_client.datetime")
    def test_excludes_backfill_rows(self, mock_dt):
        from tools.sheets_client import detect_subscription_creep

        mock_dt.now.return_value = datetime(2026, 4, 16)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        records = self._make_records([
            ("2026-02-01", "SPOTIFY", 9.99, "Entertainment", "backfill"),
            ("2026-03-01", "SPOTIFY", 9.99, "Entertainment", "backfill"),
            ("2026-04-01", "SPOTIFY", 9.99, "Entertainment", "email"),
        ])

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = records
            mock_ss.return_value.worksheet.return_value = ws

            result = detect_subscription_creep(months_back=3)

        # Only 1 non-backfill month → not enough to detect pattern
        assert len(result["subscriptions"]) == 0

    @patch("tools.sheets_client.datetime")
    def test_skips_high_variance_merchants(self, mock_dt):
        from tools.sheets_client import detect_subscription_creep

        mock_dt.now.return_value = datetime(2026, 4, 16)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        records = self._make_records([
            ("2026-02-10", "GRAB", 8.00, "Transport", "email"),
            ("2026-03-10", "GRAB", 25.00, "Transport", "email"),
            ("2026-04-10", "GRAB", 12.00, "Transport", "email"),
        ])

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = records
            mock_ss.return_value.worksheet.return_value = ws

            result = detect_subscription_creep(months_back=3)

        # High variance → not a subscription
        assert len(result["subscriptions"]) == 0

    @patch("tools.sheets_client.datetime")
    def test_total_monthly_excludes_cancelled(self, mock_dt):
        from tools.sheets_client import detect_subscription_creep

        mock_dt.now.return_value = datetime(2026, 4, 16)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        records = self._make_records([
            ("2026-02-01", "APPLE.COM/BILL", 3.98, "iCloud", "email"),
            ("2026-03-01", "APPLE.COM/BILL", 3.98, "iCloud", "email"),
            ("2026-04-01", "APPLE.COM/BILL", 3.98, "iCloud", "email"),
            ("2026-02-01", "DISNEY PLUS", 11.98, "Entertainment", "email"),
            ("2026-03-01", "DISNEY PLUS", 11.98, "Entertainment", "email"),
        ])

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = records
            mock_ss.return_value.worksheet.return_value = ws

            result = detect_subscription_creep(months_back=3)

        # Only Apple is active, Disney is cancelled
        assert result["total_monthly_subscriptions"] == 3.98

    @patch("tools.sheets_client.datetime")
    def test_empty_transactions_returns_empty(self, mock_dt):
        from tools.sheets_client import detect_subscription_creep

        mock_dt.now.return_value = datetime(2026, 4, 16)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = []
            mock_ss.return_value.worksheet.return_value = ws

            result = detect_subscription_creep(months_back=3)

        assert result["subscriptions"] == []
        assert result["total_monthly_subscriptions"] == 0

    @patch("tools.sheets_client.datetime")
    def test_skips_multiple_charges_per_month(self, mock_dt):
        """Merchants with >2 charges per month aren't subscriptions."""
        from tools.sheets_client import detect_subscription_creep

        mock_dt.now.return_value = datetime(2026, 4, 16)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        records = self._make_records([
            ("2026-03-01", "GRABFOOD", 15.00, "Food", "email"),
            ("2026-03-05", "GRABFOOD", 18.00, "Food", "email"),
            ("2026-03-15", "GRABFOOD", 12.00, "Food", "email"),
            ("2026-04-01", "GRABFOOD", 14.00, "Food", "email"),
            ("2026-04-10", "GRABFOOD", 16.00, "Food", "email"),
            ("2026-04-20", "GRABFOOD", 13.00, "Food", "email"),
        ])

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = records
            mock_ss.return_value.worksheet.return_value = ws

            result = detect_subscription_creep(months_back=3)

        assert len(result["subscriptions"]) == 0


class TestDetectSubscriptionCreepHandler:
    """Tests for the tool handler."""

    def test_calls_with_default_months(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_detect_subscription_creep

        mock_sheets_client.detect_subscription_creep.return_value = {
            "subscriptions": [],
            "total_monthly_subscriptions": 0,
            "new_this_month": [],
            "price_changes": [],
            "possibly_cancelled": [],
            "months_analyzed": ["2026-02", "2026-03", "2026-04"],
        }

        result = json.loads(handle_detect_subscription_creep({}))
        assert result["total_monthly_subscriptions"] == 0
        mock_sheets_client.detect_subscription_creep.assert_called_once_with(3)

    def test_passes_custom_months(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_detect_subscription_creep

        mock_sheets_client.detect_subscription_creep.return_value = {
            "subscriptions": [], "total_monthly_subscriptions": 0,
            "new_this_month": [], "price_changes": [],
            "possibly_cancelled": [], "months_analyzed": [],
        }

        handle_detect_subscription_creep({"months_back": 6})
        mock_sheets_client.detect_subscription_creep.assert_called_once_with(6)


class TestGetLastTransaction:
    """Tests for sheets_client.get_last_transaction."""

    def test_returns_last_row(self):
        from tools.sheets_client import get_last_transaction

        records = [
            {"Date": "2026-04-14", "Merchant": "Starbucks", "Amount": 5.50,
             "txn_id": "txn_20260414_001"},
            {"Date": "2026-04-15", "Merchant": "GRAB", "Amount": 8.00,
             "txn_id": "txn_20260415_001"},
        ]

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = records
            mock_ss.return_value.worksheet.return_value = ws

            result = get_last_transaction()
            assert result is not None
            row_num, txn = result
            assert row_num == 3  # header + 2 records
            assert txn["Merchant"] == "GRAB"
            assert txn["txn_id"] == "txn_20260415_001"

    def test_returns_none_when_empty(self):
        from tools.sheets_client import get_last_transaction

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            ws.get_all_records.return_value = []
            mock_ss.return_value.worksheet.return_value = ws

            result = get_last_transaction()
            assert result is None


class TestUndoLastExpense:
    """Tests for the undo_last_expense tool handler."""

    def test_preview_returns_last_transaction(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_undo_last_expense

        mock_sheets_client.get_last_transaction.return_value = (5, {
            "Date": "2026-04-15", "Merchant": "GRAB", "Amount": 8.00,
            "Currency": "SGD", "Category": "Transport",
            "txn_id": "txn_20260415_001",
        })

        result = json.loads(handle_undo_last_expense({}))
        assert result["status"] == "preview"
        assert result["transaction"]["Merchant"] == "GRAB"
        assert result["row"] == 5
        mock_sheets_client.delete_transaction.assert_not_called()

    def test_preview_explicit_false(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_undo_last_expense

        mock_sheets_client.get_last_transaction.return_value = (5, {
            "Date": "2026-04-15", "Merchant": "GRAB", "Amount": 8.00,
            "txn_id": "txn_20260415_001",
        })

        result = json.loads(handle_undo_last_expense({"confirm": False}))
        assert result["status"] == "preview"
        mock_sheets_client.delete_transaction.assert_not_called()

    def test_confirm_deletes_by_txn_id(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_undo_last_expense

        mock_sheets_client.get_last_transaction.return_value = (5, {
            "Date": "2026-04-15", "Merchant": "GRAB", "Amount": 8.00,
            "txn_id": "txn_20260415_001",
        })
        mock_sheets_client.delete_transaction.return_value = {
            "status": "ok", "deleted_row": 5,
            "transaction": {"Merchant": "GRAB", "Amount": 8.00},
        }

        result = json.loads(handle_undo_last_expense({"confirm": True}))
        assert result["status"] == "ok"
        mock_sheets_client.delete_transaction.assert_called_once_with(
            "", 0.0, txn_id="txn_20260415_001"
        )

    def test_confirm_fallback_to_merchant_amount(self, mock_sheets_client):
        """Legacy rows without txn_id fall back to merchant+amount delete."""
        from tools.expense_sheets_tool import handle_undo_last_expense

        mock_sheets_client.get_last_transaction.return_value = (5, {
            "Date": "2026-04-15", "Merchant": "GRAB", "Amount": 8.00,
            "txn_id": "",
        })
        mock_sheets_client.delete_transaction.return_value = {
            "status": "ok", "deleted_row": 5,
            "transaction": {"Merchant": "GRAB", "Amount": 8.00},
        }

        result = json.loads(handle_undo_last_expense({"confirm": True}))
        assert result["status"] == "ok"
        mock_sheets_client.delete_transaction.assert_called_once_with(
            "GRAB", 8.0
        )

    def test_empty_sheet_returns_error(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_undo_last_expense

        mock_sheets_client.get_last_transaction.return_value = None

        result = json.loads(handle_undo_last_expense({"confirm": True}))
        assert result["status"] == "error"
        assert "No transactions" in result["message"]


class TestGenerateSpendingReport:
    """Tests for sheets_client.generate_spending_report."""

    def _make_transactions(self, month="2026-04"):
        return [
            {"Date": f"{month}-01", "Merchant": "GRABFOOD", "Amount": 15.00,
             "Currency": "SGD", "Category": "Personal - Food & Drinks",
             "Source": "email", "txn_id": "txn_20260401_001"},
            {"Date": f"{month}-02", "Merchant": "GRABFOOD", "Amount": 18.00,
             "Currency": "SGD", "Category": "Personal - Food & Drinks",
             "Source": "email", "txn_id": "txn_20260402_001"},
            {"Date": f"{month}-03", "Merchant": "BUS/MRT", "Amount": 2.30,
             "Currency": "SGD", "Category": "Personal - Travel",
             "Source": "email", "txn_id": "txn_20260403_001"},
            {"Date": f"{month}-05", "Merchant": "COLD STORAGE", "Amount": 55.30,
             "Currency": "SGD", "Category": "Groceries",
             "Source": "email", "txn_id": "txn_20260405_001"},
        ]

    def _make_budgets(self):
        return [
            {"Category": "Personal - Food & Drinks", "Monthly Limit": 800},
            {"Category": "Personal - Travel", "Monthly Limit": 300},
            {"Category": "Groceries", "Monthly Limit": 400},
        ]

    def test_basic_report_structure(self):
        from tools.sheets_client import generate_spending_report

        with patch("tools.sheets_client.read_transactions") as mock_txn, \
             patch("tools.sheets_client.read_budgets") as mock_budgets:
            mock_txn.side_effect = [
                self._make_transactions(),  # current month
                [],  # prev month
            ]
            mock_budgets.return_value = self._make_budgets()

            result = generate_spending_report("2026-04")

        assert result["month"] == "2026-04"
        assert result["total_spent"] == 90.60
        assert result["transaction_count"] == 4
        assert len(result["categories"]) == 3
        assert len(result["top_merchants"]) == 3
        assert len(result["daily_spending"]) == 4  # 4 unique days

    def test_categories_include_txn_ids(self):
        from tools.sheets_client import generate_spending_report

        with patch("tools.sheets_client.read_transactions") as mock_txn, \
             patch("tools.sheets_client.read_budgets") as mock_budgets:
            mock_txn.side_effect = [self._make_transactions(), []]
            mock_budgets.return_value = self._make_budgets()

            result = generate_spending_report("2026-04")

        food_cat = next(c for c in result["categories"]
                        if c["category"] == "Personal - Food & Drinks")
        assert len(food_cat["txn_ids"]) == 2
        assert "txn_20260401_001" in food_cat["txn_ids"]

    def test_month_over_month_comparison(self):
        from tools.sheets_client import generate_spending_report

        prev_txns = [
            {"Date": "2026-03-01", "Merchant": "GRABFOOD", "Amount": 10.00,
             "Currency": "SGD", "Category": "Personal - Food & Drinks",
             "Source": "email", "txn_id": "txn_20260301_001"},
        ]

        with patch("tools.sheets_client.read_transactions") as mock_txn, \
             patch("tools.sheets_client.read_budgets") as mock_budgets:
            mock_txn.side_effect = [self._make_transactions(), prev_txns]
            mock_budgets.return_value = self._make_budgets()

            result = generate_spending_report("2026-04")

        mom = result["month_over_month"]
        assert mom["previous_month"] == "2026-03"
        assert mom["previous_total"] == 10.00
        assert mom["current_total"] == 90.60
        assert mom["change_pct"] > 0
        assert len(mom["category_changes"]) > 0

    def test_top_merchants_sorted_by_total(self):
        from tools.sheets_client import generate_spending_report

        with patch("tools.sheets_client.read_transactions") as mock_txn, \
             patch("tools.sheets_client.read_budgets") as mock_budgets:
            mock_txn.side_effect = [self._make_transactions(), []]
            mock_budgets.return_value = self._make_budgets()

            result = generate_spending_report("2026-04")

        merchants = result["top_merchants"]
        assert merchants[0]["merchant"] == "COLD STORAGE"  # $55.30
        assert merchants[0]["total"] == 55.30
        assert merchants[1]["merchant"] == "GRABFOOD"  # $33.00
        assert merchants[1]["count"] == 2

    def test_empty_month_returns_zeros(self):
        from tools.sheets_client import generate_spending_report

        with patch("tools.sheets_client.read_transactions") as mock_txn, \
             patch("tools.sheets_client.read_budgets") as mock_budgets:
            mock_txn.side_effect = [[], []]
            mock_budgets.return_value = self._make_budgets()

            result = generate_spending_report("2026-04")

        assert result["total_spent"] == 0
        assert result["transaction_count"] == 0
        # Categories still show from budget with 0 spent
        assert all(c["spent"] == 0 for c in result["categories"])

    def test_unbudgeted_category_included(self):
        from tools.sheets_client import generate_spending_report

        txns = [
            {"Date": "2026-04-01", "Merchant": "RANDOM", "Amount": 20.00,
             "Currency": "SGD", "Category": "Mystery Category",
             "Source": "email", "txn_id": "txn_20260401_001"},
        ]

        with patch("tools.sheets_client.read_transactions") as mock_txn, \
             patch("tools.sheets_client.read_budgets") as mock_budgets:
            mock_txn.side_effect = [txns, []]
            mock_budgets.return_value = self._make_budgets()

            result = generate_spending_report("2026-04")

        mystery = next(c for c in result["categories"]
                       if c["category"] == "Mystery Category")
        assert mystery["spent"] == 20.00
        assert mystery["limit"] == 0


class TestGenerateSpendingReportHandler:
    """Tests for the generate_spending_report tool handler."""

    def test_passes_month(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_generate_spending_report

        mock_sheets_client.generate_spending_report.return_value = {
            "month": "2026-03", "total_spent": 500.00,
        }

        result = json.loads(handle_generate_spending_report({"month": "2026-03"}))
        assert result["month"] == "2026-03"
        mock_sheets_client.generate_spending_report.assert_called_once_with("2026-03")

    def test_defaults_month_to_none(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_generate_spending_report

        mock_sheets_client.generate_spending_report.return_value = {
            "month": "2026-04", "total_spent": 0,
        }

        handle_generate_spending_report({})
        mock_sheets_client.generate_spending_report.assert_called_once_with(None)


class TestWriteInsight:
    """Tests for sheets_client.write_insight."""

    def test_creates_tab_if_missing(self):
        from tools.sheets_client import write_insight
        import gspread

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ss = MagicMock()
            mock_ss.return_value = ss
            ss.worksheet.side_effect = gspread.WorksheetNotFound("Insights")
            new_ws = MagicMock()
            ss.add_worksheet.return_value = new_ws

            result = write_insight("Overspent Food by 22%", "spending", "2026-04")

        assert result["status"] == "ok"
        assert result["insight"] == "Overspent Food by 22%"
        ss.add_worksheet.assert_called_once_with(
            title="Insights", rows=200, cols=4
        )
        # Header row + data row
        assert new_ws.append_row.call_count == 2

    def test_appends_to_existing_tab(self):
        from tools.sheets_client import write_insight

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            mock_ss.return_value.worksheet.return_value = ws

            result = write_insight("Transport trending up", "trend", "2026-04")

        assert result["status"] == "ok"
        assert result["category"] == "trend"
        ws.append_row.assert_called_once()
        row = ws.append_row.call_args[0][0]
        assert row[1] == "trend"
        assert row[2] == "2026-04"
        assert row[3] == "Transport trending up"

    def test_defaults_month_to_current(self):
        from tools.sheets_client import write_insight

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            mock_ss.return_value.worksheet.return_value = ws

            result = write_insight("General note")

        assert result["month"] == datetime.now().strftime("%Y-%m")


class TestGetInsights:
    """Tests for sheets_client.get_insights."""

    def test_returns_all_when_no_filter(self):
        from tools.sheets_client import get_insights

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            mock_ss.return_value.worksheet.return_value = ws
            ws.get_all_records.return_value = [
                {"date": "2026-04-10", "category": "spending",
                 "month": "2026-04", "insight": "Food over budget"},
                {"date": "2026-04-16", "category": "trend",
                 "month": "2026-04", "insight": "Transport up 35%"},
            ]

            result = get_insights()

        assert len(result) == 2
        # Most recent first
        assert result[0]["insight"] == "Transport up 35%"

    def test_filters_by_month(self):
        from tools.sheets_client import get_insights

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            mock_ss.return_value.worksheet.return_value = ws
            ws.get_all_records.return_value = [
                {"date": "2026-03-31", "category": "spending",
                 "month": "2026-03", "insight": "March note"},
                {"date": "2026-04-16", "category": "spending",
                 "month": "2026-04", "insight": "April note"},
            ]

            result = get_insights(month="2026-04")

        assert len(result) == 1
        assert result[0]["insight"] == "April note"

    def test_filters_by_category(self):
        from tools.sheets_client import get_insights

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            mock_ss.return_value.worksheet.return_value = ws
            ws.get_all_records.return_value = [
                {"date": "2026-04-10", "category": "spending",
                 "month": "2026-04", "insight": "Spending note"},
                {"date": "2026-04-16", "category": "trend",
                 "month": "2026-04", "insight": "Trend note"},
            ]

            result = get_insights(category="trend")

        assert len(result) == 1
        assert result[0]["insight"] == "Trend note"

    def test_respects_limit(self):
        from tools.sheets_client import get_insights

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            ws = MagicMock()
            mock_ss.return_value.worksheet.return_value = ws
            ws.get_all_records.return_value = [
                {"date": f"2026-04-{i:02d}", "category": "general",
                 "month": "2026-04", "insight": f"Note {i}"}
                for i in range(1, 11)
            ]

            result = get_insights(limit=3)

        assert len(result) == 3

    def test_returns_empty_when_tab_missing(self):
        from tools.sheets_client import get_insights
        import gspread

        with patch("tools.sheets_client.get_spreadsheet") as mock_ss:
            mock_ss.return_value.worksheet.side_effect = gspread.WorksheetNotFound("Insights")

            result = get_insights()

        assert result == []


class TestWriteInsightHandler:
    """Tests for the write_insight tool handler."""

    def test_passes_all_args(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_write_insight

        mock_sheets_client.write_insight.return_value = {
            "status": "ok", "insight": "test", "category": "spending",
            "month": "2026-04",
        }

        result = json.loads(handle_write_insight({
            "insight": "test",
            "category": "spending",
            "month": "2026-04",
        }))
        assert result["status"] == "ok"
        mock_sheets_client.write_insight.assert_called_once_with(
            "test", "spending", "2026-04"
        )

    def test_defaults(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_write_insight

        mock_sheets_client.write_insight.return_value = {
            "status": "ok", "insight": "note", "category": "general",
            "month": "2026-04",
        }

        handle_write_insight({"insight": "note"})
        mock_sheets_client.write_insight.assert_called_once_with(
            "note", "general", None
        )


class TestGetInsightsHandler:
    """Tests for the get_insights tool handler."""

    def test_passes_filters(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_get_insights

        mock_sheets_client.get_insights.return_value = [
            {"date": "2026-04-16", "category": "trend",
             "month": "2026-04", "insight": "Up"},
        ]

        result = json.loads(handle_get_insights({
            "month": "2026-04", "category": "trend", "limit": 5,
        }))
        assert len(result) == 1
        mock_sheets_client.get_insights.assert_called_once_with(
            "2026-04", "trend", 5
        )

    def test_defaults(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_get_insights

        mock_sheets_client.get_insights.return_value = []

        handle_get_insights({})
        mock_sheets_client.get_insights.assert_called_once_with(
            None, None, 20
        )


class TestLogExpensePending:
    """Tests for log_expense_pending — logs with UNCATEGORIZED and sends
    an ask-prompt bubble (instead of the normal confirmation)."""

    def test_logs_as_uncategorized_and_sends_ask_prompt(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense_pending

        fake_send = {"ok": True, "message_id": "9001"}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fake-token"}):
            with patch(
                "tools.expense_sheets_tool._send_telegram_bubble",
                return_value=fake_send,
            ) as mock_send:
                result = json.loads(handle_log_expense_pending({
                    "merchant": "MUHAMMAD MUBEEN",
                    "amount": 1.50,
                    "currency": "SGD",
                    "payment_method": "PayLah! Wallet",
                    "options": [
                        "🙋 Personal - Others",
                        "💸 Transfer (outside tracker)",
                    ],
                }))

        assert result["status"] == "ok"
        assert result["pending"] is True
        assert result["bubble_sent"] is True
        assert result["telegram_message_id"] == "9001"
        assert result["linked"] is True
        assert result["assistant_reply_required"] is False

        # Transaction was appended with UNCATEGORIZED (force-overrides any
        # category the LLM might have passed).
        call_kwargs = mock_sheets_client.append_transaction.call_args[1]
        assert call_kwargs["category"] == "UNCATEGORIZED"
        assert call_kwargs["merchant"] == "MUHAMMAD MUBEEN"
        assert call_kwargs["amount"] == 1.50

        # Ask-prompt bubble includes the txn_id for reply-to-fallback
        bubble_text = mock_send.call_args[0][0]
        assert "txn_20260414_001" in bubble_text
        assert "MUHAMMAD MUBEEN" in bubble_text
        assert "🤔" in bubble_text
        assert "Personal - Others" in bubble_text

        # link_telegram_message fired so reply-to-message resolves
        mock_sheets_client.link_telegram_message.assert_called_once_with(
            "txn_20260414_001", "9001"
        )

    def test_requires_options(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense_pending

        result = json.loads(handle_log_expense_pending({
            "merchant": "Unknown",
            "amount": 5.00,
            "options": [],
        }))
        assert result["status"] == "error"
        assert "options" in result["message"].lower()
        mock_sheets_client.append_transaction.assert_not_called()

    def test_duplicate_transaction_skips_bubble(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense_pending

        mock_sheets_client.append_transaction.return_value = {
            "status": "duplicate",
            "txn_id": "txn_20260414_001",
            "idempotency_key": "abc123",
        }

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fake-token"}):
            with patch("tools.expense_sheets_tool._send_telegram_bubble") as mock_send:
                result = json.loads(handle_log_expense_pending({
                    "merchant": "Starbucks",
                    "amount": 5.00,
                    "options": ["Food & Dining"],
                }))

        assert result["status"] == "duplicate"
        mock_send.assert_not_called()

    def test_ensures_uncategorized_category_exists(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_log_expense_pending

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}, clear=False):
            handle_log_expense_pending({
                "merchant": "Starbucks",
                "amount": 5.00,
                "options": ["Food & Dining"],
            })

        # Should create the UNCATEGORIZED budget row once on first use
        mock_sheets_client.ensure_category_exists.assert_called_with("UNCATEGORIZED")


class TestJournalTools:
    """Tests for append_journal_entry / get_journal_entries — the
    reply=journal experiment surface."""

    def test_append_journal_entry_passes_through(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_append_journal_entry

        mock_sheets_client.write_journal_entry.return_value = {
            "status": "ok",
            "date": "2026-04-22",
            "reply_text": "Stressful day, impulse-bought chocolate",
            "txn_ids_referenced": "txn_20260422_005",
            "tags": "regret,stress",
        }

        result = json.loads(handle_append_journal_entry({
            "reply_text": "Stressful day, impulse-bought chocolate",
            "date": "2026-04-22",
            "txn_ids_referenced": "txn_20260422_005",
            "tags": "regret,stress",
        }))

        assert result["status"] == "ok"
        mock_sheets_client.write_journal_entry.assert_called_once_with(
            reply_text="Stressful day, impulse-bought chocolate",
            date="2026-04-22",
            txn_ids_referenced="txn_20260422_005",
            tags="regret,stress",
        )

    def test_append_journal_entry_defaults_date_to_today(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_append_journal_entry

        mock_sheets_client.write_journal_entry.return_value = {
            "status": "ok", "date": "today", "reply_text": "x",
            "txn_ids_referenced": "", "tags": "",
        }

        handle_append_journal_entry({"reply_text": "x"})
        call_kwargs = mock_sheets_client.write_journal_entry.call_args[1]
        assert len(call_kwargs["date"]) == 10  # YYYY-MM-DD format
        assert call_kwargs["txn_ids_referenced"] == ""
        assert call_kwargs["tags"] == ""

    def test_append_journal_entry_rejects_empty_reply(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_append_journal_entry

        result = json.loads(handle_append_journal_entry({"reply_text": "   "}))
        assert result["status"] == "error"
        mock_sheets_client.write_journal_entry.assert_not_called()

    def test_get_journal_entries_passes_filters(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_get_journal_entries

        mock_sheets_client.read_journal_entries.return_value = [
            {
                "date": "2026-04-22",
                "reply_text": "regret",
                "txn_ids_referenced": "",
                "tags": "regret",
            },
        ]

        result = json.loads(handle_get_journal_entries({
            "month": "2026-04",
            "limit": 5,
        }))
        assert len(result) == 1
        mock_sheets_client.read_journal_entries.assert_called_once_with(
            date=None, month="2026-04", limit=5,
        )

    def test_get_journal_entries_defaults(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_get_journal_entries

        mock_sheets_client.read_journal_entries.return_value = []

        handle_get_journal_entries({})
        mock_sheets_client.read_journal_entries.assert_called_once_with(
            date=None, month=None, limit=20,
        )


class TestIdempotencyKeyParity:
    """Pinned idempotency key values. The JS implementation in
    apps-script/Code.gs::computeIdempotencyKey MUST produce the same 16-char
    hex for these exact inputs. The testIdempotencyKeyParity() function in
    Code.gs asserts the same table manually — run it once from the Apps
    Script editor after redeploying to confirm parity.

    If any of these values change, the sweep tool starts flagging false
    positives (JS logs a different key than Python reconciles against).
    """

    CASES = [
        ("2026-04-16", "Starbucks",    5.50,  "DBS card ending 1234",      "cb89d3a1d11dd274"),
        ("2026-04-14", "Cold Storage", 45.30, "DBS/POSB card ending 1234", "a2a3de653d223e63"),
        ("2026-04-12", "GRABFOOD",     12.50, "UOB Card ending 3456",      "4077c41963aa0a86"),
        ("2026-04-10", "Sheng Siong",  23.80, "PayLah! Wallet",            "25f16c670462d6c8"),
    ]

    def test_python_pins_match_expected(self):
        from tools.sheets_client import _compute_idempotency_key

        for date, merchant, amount, payment_method, expected in self.CASES:
            got = _compute_idempotency_key(date, merchant, amount, payment_method)
            assert got == expected, (
                f"Parity drift for {merchant} ${amount}: "
                f"got={got} expected={expected}. "
                f"If you changed _compute_idempotency_key, update the Apps "
                f"Script testIdempotencyKeyParity() pins too."
            )


class TestSweepMissedTransactions:
    """Tests for sheets_client.sweep_missed_transactions."""

    def _make_spreadsheet(self, webhook_log_records, transactions_keys,
                         has_webhook_log=True, has_idem_col=True):
        """Build a MagicMock spreadsheet with two worksheets (WebhookLog and
        Transactions) wired up for sweep_missed_transactions."""
        import gspread

        ss = MagicMock()
        wl = MagicMock()
        wl.get_all_records.return_value = webhook_log_records

        tx = MagicMock()
        header = [
            "Date", "Merchant", "Amount", "Currency", "Category",
            "Source", "Payment Method", "Notes", "txn_id",
            "telegram_message_id", "idempotency_key",
        ]
        if not has_idem_col:
            header = header[:-1]
        tx.row_values.return_value = header
        tx.col_values.return_value = ["idempotency_key"] + list(transactions_keys)

        def worksheet(name):
            if name == "WebhookLog":
                if not has_webhook_log:
                    raise gspread.WorksheetNotFound("WebhookLog")
                return wl
            if name == "Transactions":
                return tx
            raise gspread.WorksheetNotFound(name)

        ss.worksheet.side_effect = worksheet
        return ss

    def test_returns_error_when_webhook_log_missing(self):
        from tools.sheets_client import sweep_missed_transactions

        ss = self._make_spreadsheet([], [], has_webhook_log=False)
        with patch("tools.sheets_client.get_spreadsheet", return_value=ss):
            result = sweep_missed_transactions(days_back=7)

        assert result["status"] == "error"
        assert "WebhookLog" in result["message"]

    def test_empty_log_returns_zero_missed(self):
        from tools.sheets_client import sweep_missed_transactions

        ss = self._make_spreadsheet([], ["abc123"])
        with patch("tools.sheets_client.get_spreadsheet", return_value=ss):
            result = sweep_missed_transactions(days_back=7)

        assert result["status"] == "ok"
        assert result["missed_count"] == 0
        assert result["total_webhook_logs"] == 0
        assert result["missed"] == []

    def test_all_matched_returns_zero_missed(self):
        from tools.sheets_client import sweep_missed_transactions

        today = datetime.now().strftime("%Y-%m-%d")
        records = [
            {
                "timestamp": "2026-04-20T10:00:00Z",
                "bank": "DBS", "type": "card",
                "amount": 12.50, "currency": "SGD",
                "merchant": "GRABFOOD", "date": today,
                "payment_method": "UOB Card ending 3456",
                "idempotency_key": "key_grab_1",
                "webhook_status": "sent", "matched": "",
            },
        ]
        ss = self._make_spreadsheet(records, ["key_grab_1"])
        with patch("tools.sheets_client.get_spreadsheet", return_value=ss):
            result = sweep_missed_transactions(days_back=7)

        assert result["missed_count"] == 0
        assert result["total_webhook_logs"] == 1

    def test_finds_unmatched_entries(self):
        from tools.sheets_client import sweep_missed_transactions

        today = datetime.now().strftime("%Y-%m-%d")
        records = [
            {
                "timestamp": "2026-04-20T10:00:00Z",
                "bank": "DBS", "type": "card",
                "amount": 12.50, "currency": "SGD",
                "merchant": "GRABFOOD", "date": today,
                "payment_method": "UOB Card ending 3456",
                "idempotency_key": "key_grab_1",
                "webhook_status": "sent", "matched": "",
            },
            {
                "timestamp": "2026-04-20T11:00:00Z",
                "bank": "DBS", "type": "card",
                "amount": 45.30, "currency": "SGD",
                "merchant": "COLD STORAGE", "date": today,
                "payment_method": "DBS/POSB card ending 1234",
                "idempotency_key": "key_cold_1",
                "webhook_status": "error:timeout", "matched": "",
            },
        ]
        # Only the GRABFOOD row made it into Transactions; COLD STORAGE
        # is missing (the webhook errored out).
        ss = self._make_spreadsheet(records, ["key_grab_1"])
        with patch("tools.sheets_client.get_spreadsheet", return_value=ss):
            result = sweep_missed_transactions(days_back=7)

        assert result["missed_count"] == 1
        assert result["missed"][0]["merchant"] == "COLD STORAGE"
        assert result["missed"][0]["idempotency_key"] == "key_cold_1"
        assert result["missed"][0]["webhook_status"] == "error:timeout"

    def test_excludes_rows_already_marked_matched(self):
        from tools.sheets_client import sweep_missed_transactions

        today = datetime.now().strftime("%Y-%m-%d")
        records = [
            {
                "timestamp": "2026-04-20T10:00:00Z",
                "bank": "DBS", "type": "card",
                "amount": 12.50, "currency": "SGD",
                "merchant": "GRABFOOD", "date": today,
                "payment_method": "UOB Card ending 3456",
                "idempotency_key": "key_grab_1",
                "webhook_status": "error:529", "matched": "yes",
            },
        ]
        # The transaction isn't in Transactions, but "matched=yes" means
        # a prior sweep already reconciled it — don't re-surface.
        ss = self._make_spreadsheet(records, [])
        with patch("tools.sheets_client.get_spreadsheet", return_value=ss):
            result = sweep_missed_transactions(days_back=7)

        assert result["missed_count"] == 0

    def test_excludes_rows_older_than_cutoff(self):
        from tools.sheets_client import sweep_missed_transactions

        today = datetime.now().strftime("%Y-%m-%d")
        long_ago = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        records = [
            {
                "timestamp": "2025-04-20T10:00:00Z",
                "bank": "DBS", "type": "card",
                "amount": 12.50, "currency": "SGD",
                "merchant": "OLD MERCHANT", "date": long_ago,
                "payment_method": "UOB Card",
                "idempotency_key": "key_old",
                "webhook_status": "sent", "matched": "",
            },
            {
                "timestamp": "2026-04-20T10:00:00Z",
                "bank": "DBS", "type": "card",
                "amount": 12.50, "currency": "SGD",
                "merchant": "GRABFOOD", "date": today,
                "payment_method": "UOB Card ending 3456",
                "idempotency_key": "key_grab_1",
                "webhook_status": "sent", "matched": "",
            },
        ]
        ss = self._make_spreadsheet(records, [])
        with patch("tools.sheets_client.get_spreadsheet", return_value=ss):
            result = sweep_missed_transactions(days_back=7)

        # Only the in-window row is even counted; the old one is filtered first.
        assert result["total_webhook_logs"] == 1
        assert result["missed_count"] == 1
        assert result["missed"][0]["merchant"] == "GRABFOOD"

    def test_skips_rows_with_no_idempotency_key(self):
        from tools.sheets_client import sweep_missed_transactions

        today = datetime.now().strftime("%Y-%m-%d")
        records = [
            {
                "timestamp": "2026-04-20T10:00:00Z",
                "bank": "DBS", "type": "card",
                "amount": 12.50, "currency": "SGD",
                "merchant": "UNKEYED", "date": today,
                "payment_method": "UOB Card",
                "idempotency_key": "",  # Apps Script hiccup
                "webhook_status": "sent", "matched": "",
            },
        ]
        ss = self._make_spreadsheet(records, [])
        with patch("tools.sheets_client.get_spreadsheet", return_value=ss):
            result = sweep_missed_transactions(days_back=7)

        # Counted in the window total but not actionable (no key to diff).
        assert result["total_webhook_logs"] == 1
        assert result["missed_count"] == 0

    def test_legacy_transactions_without_idem_column(self):
        from tools.sheets_client import sweep_missed_transactions

        today = datetime.now().strftime("%Y-%m-%d")
        records = [
            {
                "timestamp": "2026-04-20T10:00:00Z",
                "bank": "DBS", "type": "card",
                "amount": 12.50, "currency": "SGD",
                "merchant": "GRABFOOD", "date": today,
                "payment_method": "UOB Card ending 3456",
                "idempotency_key": "key_grab_1",
                "webhook_status": "sent", "matched": "",
            },
        ]
        # Legacy 10-column sheet with no idempotency_key column — can't
        # match, so everything in WebhookLog reads as missed. That's the
        # right conservative behaviour: prompts the user to apply the
        # schema migration.
        ss = self._make_spreadsheet(records, [], has_idem_col=False)
        with patch("tools.sheets_client.get_spreadsheet", return_value=ss):
            result = sweep_missed_transactions(days_back=7)

        assert result["missed_count"] == 1


class TestSweepMissedTransactionsHandler:
    """Tests for the expense_sheets_tool handler wrapper."""

    def test_handler_passes_days_back(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_sweep_missed_transactions

        mock_sheets_client.sweep_missed_transactions.return_value = {
            "status": "ok", "days_back": 14, "total_webhook_logs": 0,
            "missed_count": 0, "missed": [],
        }

        result = json.loads(handle_sweep_missed_transactions({"days_back": 14}))
        assert result["status"] == "ok"
        mock_sheets_client.sweep_missed_transactions.assert_called_once_with(days_back=14)

    def test_handler_defaults_days_back_to_7(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_sweep_missed_transactions

        mock_sheets_client.sweep_missed_transactions.return_value = {
            "status": "ok", "days_back": 7, "total_webhook_logs": 0,
            "missed_count": 0, "missed": [],
        }

        handle_sweep_missed_transactions({})
        mock_sheets_client.sweep_missed_transactions.assert_called_once_with(days_back=7)

    def test_handler_propagates_error_status(self, mock_sheets_client):
        from tools.expense_sheets_tool import handle_sweep_missed_transactions

        mock_sheets_client.sweep_missed_transactions.return_value = {
            "status": "error", "message": "WebhookLog tab not found...",
        }

        result = json.loads(handle_sweep_missed_transactions({}))
        assert result["status"] == "error"
        assert "WebhookLog" in result["message"]
