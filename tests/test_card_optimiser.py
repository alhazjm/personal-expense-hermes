"""Tests for the card-optimiser module and its 5 tool handlers.

Also covers the two bug-fix regressions from commit 9926a5a that ride along
on this branch:
  - get_spending_summary(month="") no longer raises IndexError
  - _is_transient_gspread_error recognises 429 / 5xx as transient
"""

import json
import os
from datetime import date
from unittest.mock import MagicMock, patch

import gspread
import pytest


@pytest.fixture(autouse=True)
def mock_env():
    with patch.dict(os.environ, {
        "GSPREAD_SPREADSHEET_ID": "test-sheet-id",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "/tmp/fake-sa.json",
    }):
        yield


SAMPLE_CARDS = [
    {
        "card_id": "dbs-altitude",
        "display_name": "DBS Altitude Visa",
        "payment_method_pattern": "DBS/POSB card ending 1234",
        "cycle_start_day": 15,
        "min_spend_bonus": 0.0,
        "notes": "",
    },
    {
        "card_id": "uob-prvi",
        "display_name": "UOB PRVI Miles",
        "payment_method_pattern": "UOB Card ending 3456",
        "cycle_start_day": 1,
        "min_spend_bonus": 0.0,
        "notes": "",
    },
]


SAMPLE_STRATEGIES = [
    {
        "category": "_default",
        "primary_card_id": "uob-prvi",
        "primary_cap": 0.0,
        "primary_earn_rate": 1.4,
        "fallback_card_id": "",
        "fallback_earn_rate": 0.0,
        "promo_active_until": "",
        "notes": "catch-all",
    },
    {
        "category": "Personal - Food & Drinks",
        "primary_card_id": "dbs-altitude",
        "primary_cap": 1000.0,
        "primary_earn_rate": 4.0,
        "fallback_card_id": "uob-prvi",
        "fallback_earn_rate": 2.4,
        "promo_active_until": "",
        "notes": "DBS 4mpd dining",
    },
]


class TestSetupGate:
    def test_none_when_ready(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards", return_value=SAMPLE_CARDS), \
             patch.object(card_optimiser, "read_card_strategy", return_value=SAMPLE_STRATEGIES):
            assert card_optimiser._check_setup() is None

    def test_missing_cards(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards", return_value=[]), \
             patch.object(card_optimiser, "read_card_strategy", return_value=SAMPLE_STRATEGIES):
            gate = card_optimiser._check_setup()
        assert gate is not None
        assert gate["status"] == "setup_required"
        assert "Cards" in gate["message"]

    def test_missing_strategy(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards", return_value=SAMPLE_CARDS), \
             patch.object(card_optimiser, "read_card_strategy", return_value=[]):
            gate = card_optimiser._check_setup()
        assert gate["status"] == "setup_required"
        assert "CardStrategy" in gate["message"]

    def test_missing_default_sentinel(self):
        from tools import card_optimiser
        no_default = [s for s in SAMPLE_STRATEGIES if s["category"] != "_default"]
        with patch.object(card_optimiser, "read_cards", return_value=SAMPLE_CARDS), \
             patch.object(card_optimiser, "read_card_strategy", return_value=no_default):
            gate = card_optimiser._check_setup()
        assert gate["status"] == "setup_required"
        assert "_default" in gate["message"]


class TestCardResolution:
    def test_longest_match_wins(self):
        from tools.card_optimiser import _find_card_by_payment_method
        cards = [
            {"card_id": "a", "payment_method_pattern": "DBS"},
            {"card_id": "b", "payment_method_pattern": "DBS/POSB card ending 1234"},
            {"card_id": "c", "payment_method_pattern": "UOB"},
        ]
        match = _find_card_by_payment_method("DBS/POSB card ending 1234 at Cold Storage", cards)
        assert match["card_id"] == "b"

    def test_no_match_returns_none(self):
        from tools.card_optimiser import _find_card_by_payment_method
        cards = [{"card_id": "a", "payment_method_pattern": "DBS"}]
        assert _find_card_by_payment_method("Citibank MC 1234", cards) is None

    def test_empty_payment_method(self):
        from tools.card_optimiser import _find_card_by_payment_method
        assert _find_card_by_payment_method("", SAMPLE_CARDS) is None

    def test_case_insensitive(self):
        from tools.card_optimiser import _find_card_by_payment_method
        cards = [{"card_id": "a", "payment_method_pattern": "DBS/POSB"}]
        match = _find_card_by_payment_method("dbs/posb card ending 1234", cards)
        assert match["card_id"] == "a"


class TestStrategyResolution:
    def test_exact_match_wins(self):
        from tools.card_optimiser import _find_strategy
        s = _find_strategy("Personal - Food & Drinks", SAMPLE_STRATEGIES)
        assert s["primary_card_id"] == "dbs-altitude"

    def test_default_fallback(self):
        from tools.card_optimiser import _find_strategy
        s = _find_strategy("Groceries", SAMPLE_STRATEGIES)
        assert s["primary_card_id"] == "uob-prvi"
        assert s["category"] == "_default"

    def test_no_default_returns_none(self):
        from tools.card_optimiser import _find_strategy
        no_default = [s for s in SAMPLE_STRATEGIES if s["category"] != "_default"]
        assert _find_strategy("Groceries", no_default) is None


class TestCycleWindow:
    def test_mid_cycle(self):
        from tools.card_optimiser import _cycle_window
        start, end = _cycle_window(15, date(2026, 4, 22))
        assert start == date(2026, 4, 15)
        assert end == date(2026, 5, 14)

    def test_before_cycle_boundary(self):
        from tools.card_optimiser import _cycle_window
        start, end = _cycle_window(15, date(2026, 4, 10))
        assert start == date(2026, 3, 15)
        assert end == date(2026, 4, 14)

    def test_cycle_day_31_feb_clamp(self):
        from tools.card_optimiser import _cycle_window
        start, end = _cycle_window(31, date(2026, 2, 15))
        # Jan 31 cycle runs until Feb 27 (Feb 28 clamps to day 28, so the
        # current cycle for Feb 15 started on Jan 31 and the next cycle
        # starts Feb 28 → current cycle ends Feb 27).
        assert start == date(2026, 1, 31)
        assert end == date(2026, 2, 27)

    def test_cycle_day_1_simple(self):
        from tools.card_optimiser import _cycle_window
        start, end = _cycle_window(1, date(2026, 4, 10))
        assert start == date(2026, 4, 1)
        assert end == date(2026, 4, 30)

    def test_cycle_across_year_boundary(self):
        from tools.card_optimiser import _cycle_window
        start, end = _cycle_window(15, date(2027, 1, 5))
        assert start == date(2026, 12, 15)
        assert end == date(2027, 1, 14)


class TestCapStatus:
    def test_ok_under_80(self):
        from tools.card_optimiser import _cap_status
        assert _cap_status(500, 1000) == "ok"
        assert _cap_status(799, 1000) == "ok"

    def test_warning_at_80(self):
        from tools.card_optimiser import _cap_status
        assert _cap_status(800, 1000) == "warning"
        assert _cap_status(999, 1000) == "warning"

    def test_capped_at_100(self):
        from tools.card_optimiser import _cap_status
        assert _cap_status(1000, 1000) == "capped"
        assert _cap_status(1500, 1000) == "capped"

    def test_zero_cap_is_unlimited(self):
        from tools.card_optimiser import _cap_status
        assert _cap_status(99999, 0) == "ok"


class TestSpendInCycle:
    @staticmethod
    def _mock_sheets(records):
        """Wire a mock get_spreadsheet() that returns a ws with the records."""
        ws = MagicMock()
        ws.get_all_records.return_value = records
        ss = MagicMock()
        ss.worksheet.return_value = ws
        return ss

    def test_filters_by_cycle_window(self):
        from tools import card_optimiser
        card = {
            "cycle_start_day": 15,
            "payment_method_pattern": "DBS/POSB card ending 1234",
        }
        records = [
            # In cycle (2026-04-15 to 2026-05-14)
            {"Date": "2026-04-20", "Category": "Personal - Food & Drinks",
             "Amount": 50, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "email"},
            # Before cycle — excluded
            {"Date": "2026-04-10", "Category": "Personal - Food & Drinks",
             "Amount": 100, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "email"},
            # After cycle — excluded
            {"Date": "2026-05-20", "Category": "Personal - Food & Drinks",
             "Amount": 200, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "email"},
        ]
        ss = self._mock_sheets(records)
        with patch.object(card_optimiser, "get_spreadsheet", return_value=ss):
            spent = card_optimiser._spend_in_cycle(
                card, "Personal - Food & Drinks", date(2026, 4, 22)
            )
        assert spent == 50.0

    def test_excludes_backfill_and_pending(self):
        from tools import card_optimiser
        card = {
            "cycle_start_day": 15,
            "payment_method_pattern": "DBS/POSB card ending 1234",
        }
        records = [
            {"Date": "2026-04-20", "Category": "Personal - Food & Drinks",
             "Amount": 50, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "email"},
            # Backfill — skipped
            {"Date": "2026-04-20", "Category": "Personal - Food & Drinks",
             "Amount": 1000, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "backfill"},
            # Pending — skipped
            {"Date": "2026-04-20", "Category": "UNCATEGORIZED",
             "Amount": 25, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "email"},
        ]
        ss = self._mock_sheets(records)
        with patch.object(card_optimiser, "get_spreadsheet", return_value=ss):
            spent = card_optimiser._spend_in_cycle(
                card, "Personal - Food & Drinks", date(2026, 4, 22)
            )
        assert spent == 50.0

    def test_filters_by_payment_method(self):
        from tools import card_optimiser
        card = {
            "cycle_start_day": 15,
            "payment_method_pattern": "DBS/POSB card ending 1234",
        }
        records = [
            {"Date": "2026-04-20", "Category": "Personal - Food & Drinks",
             "Amount": 50, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "email"},
            # Different card — skipped
            {"Date": "2026-04-20", "Category": "Personal - Food & Drinks",
             "Amount": 500, "Payment Method": "UOB Card ending 3456",
             "Source": "email"},
        ]
        ss = self._mock_sheets(records)
        with patch.object(card_optimiser, "get_spreadsheet", return_value=ss):
            spent = card_optimiser._spend_in_cycle(
                card, "Personal - Food & Drinks", date(2026, 4, 22)
            )
        assert spent == 50.0


class TestMaybeSendPostCapNudge:
    @pytest.fixture
    def patched_helpers(self):
        """Patch out all sheet reads so only the decision logic runs."""
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards", return_value=SAMPLE_CARDS), \
             patch.object(card_optimiser, "read_card_strategy", return_value=SAMPLE_STRATEGIES), \
             patch.object(card_optimiser, "_record_nudge"), \
             patch.object(card_optimiser, "_find_triggering_txn_id", return_value="txn_20260420_001"):
            yield card_optimiser

    def test_setup_required_returns_no_send(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards", return_value=[]), \
             patch.object(card_optimiser, "read_card_strategy", return_value=[]):
            result = card_optimiser.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-20",
            )
        assert result["sent"] is False
        assert result["reason"] == "setup"

    def test_unmapped_payment_method(self, patched_helpers):
        result = patched_helpers.maybe_send_post_cap_nudge(
            payment_method="Random Bank Card 9999",
            category="Personal - Food & Drinks",
            amount=100.0,
            txn_date="2026-04-20",
        )
        assert result["sent"] is False
        assert result["reason"] == "unmapped_payment_method"

    def test_not_primary_for_category(self, patched_helpers):
        result = patched_helpers.maybe_send_post_cap_nudge(
            payment_method="UOB Card ending 3456",
            category="Personal - Food & Drinks",
            amount=100.0,
            txn_date="2026-04-20",
        )
        assert result["sent"] is False
        assert result["reason"] == "not_primary_for_category"

    def test_fires_at_80_percent(self, patched_helpers):
        # spent_after=810 crosses 80% (cap=1000). amount=100 → spent_before=710 (71%).
        with patch.object(patched_helpers, "_spend_in_cycle", return_value=810.0), \
             patch.object(patched_helpers, "_already_nudged", return_value=False), \
             patch("tools.expense_sheets_tool._send_telegram_bubble",
                   return_value={"ok": True, "message_id": "1234"}):
            result = patched_helpers.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-20",
            )
        assert result["sent"] is True
        assert result["threshold"] == 80
        assert result["card_id"] == "dbs-altitude"

    def test_no_threshold_crossed(self, patched_helpers):
        # spent_after=500 (50%), spent_before=400 (40%). No crossing.
        with patch.object(patched_helpers, "_spend_in_cycle", return_value=500.0), \
             patch.object(patched_helpers, "_already_nudged", return_value=False):
            result = patched_helpers.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-20",
            )
        assert result["sent"] is False
        assert result["reason"] == "no_threshold_crossed"

    def test_already_nudged_dedup(self, patched_helpers):
        # 80% threshold crossed, but already nudged this cycle.
        with patch.object(patched_helpers, "_spend_in_cycle", return_value=810.0), \
             patch.object(patched_helpers, "_already_nudged", return_value=True):
            result = patched_helpers.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-20",
            )
        assert result["sent"] is False
        assert result["reason"] == "already_nudged_this_cycle"

    def test_silent_when_already_switched(self, patched_helpers):
        # spent_after=1050 crosses 100%. amount=50 → spent_before=1000 (still
        # 100%, but pct_before=1.0 not < 1.0). Use amount=100 → spent_before=950
        # (95%), spent_after=1050 (105%) → crosses 100%. Good.
        def already(cycle_window, card_id, category, threshold):
            return threshold == 80  # 80% already fired, 100% has not

        with patch.object(patched_helpers, "_spend_in_cycle", return_value=1050.0), \
             patch.object(patched_helpers, "_already_nudged", side_effect=already), \
             patch.object(patched_helpers, "_last_n_in_category_on_card", return_value=True), \
             patch("tools.expense_sheets_tool._send_telegram_bubble") as mock_send:
            result = patched_helpers.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-25",
            )
        assert result["sent"] is False
        assert result["reason"] == "silent_already_switched"
        mock_send.assert_not_called()

    def test_100_fires_when_not_already_switched(self, patched_helpers):
        def already(cycle_window, card_id, category, threshold):
            return threshold == 80

        with patch.object(patched_helpers, "_spend_in_cycle", return_value=1050.0), \
             patch.object(patched_helpers, "_already_nudged", side_effect=already), \
             patch.object(patched_helpers, "_last_n_in_category_on_card", return_value=False), \
             patch("tools.expense_sheets_tool._send_telegram_bubble",
                   return_value={"ok": True, "message_id": "5678"}):
            result = patched_helpers.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-25",
            )
        assert result["sent"] is True
        assert result["threshold"] == 100

    def test_swallows_exceptions(self, patched_helpers):
        with patch.object(patched_helpers, "_spend_in_cycle",
                          side_effect=RuntimeError("boom")):
            result = patched_helpers.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-20",
            )
        assert result["sent"] is False
        assert result["reason"] == "exception"
        assert "boom" in result["error"]


class TestHandlers:
    """The 5 handlers in expense_sheets_tool.py delegate to card_optimiser.
    These tests verify arg pass-through and JSON encoding."""

    def test_get_card_cap_status_handler(self):
        from tools.expense_sheets_tool import handle_get_card_cap_status
        from tools import card_optimiser
        with patch.object(card_optimiser, "get_card_cap_status",
                          return_value={"status": "ok", "cards": []}) as mock_fn:
            result = json.loads(handle_get_card_cap_status({
                "card_id": "dbs-altitude",
                "category": "Personal - Food & Drinks",
            }))
        mock_fn.assert_called_once_with(
            card_id="dbs-altitude",
            category="Personal - Food & Drinks",
            as_of=None,
        )
        assert result["status"] == "ok"

    def test_recommend_card_for_handler(self):
        from tools.expense_sheets_tool import handle_recommend_card_for
        from tools import card_optimiser
        with patch.object(card_optimiser, "recommend_card_for",
                          return_value={"status": "ok"}) as mock_fn:
            handle_recommend_card_for({
                "category": "Personal - Food & Drinks",
                "amount": 42.50,
            })
        mock_fn.assert_called_once_with(
            category="Personal - Food & Drinks",
            amount=42.5,
            as_of=None,
        )

    def test_plan_month_handler(self):
        from tools.expense_sheets_tool import handle_plan_month
        from tools import card_optimiser
        with patch.object(card_optimiser, "plan_month",
                          return_value={"status": "ok", "plan": []}) as mock_fn:
            result = json.loads(handle_plan_month({"month": "2026-04"}))
        mock_fn.assert_called_once_with(month="2026-04")
        assert result["status"] == "ok"

    def test_review_card_efficiency_handler(self):
        from tools.expense_sheets_tool import handle_review_card_efficiency
        from tools import card_optimiser
        with patch.object(card_optimiser, "review_card_efficiency",
                          return_value={"status": "ok"}) as mock_fn:
            handle_review_card_efficiency({"month": "2026-03"})
        mock_fn.assert_called_once_with(month="2026-03")

    def test_set_category_primary_handler_passes_overrides(self):
        from tools.expense_sheets_tool import handle_set_category_primary
        from tools import card_optimiser
        with patch.object(card_optimiser, "set_category_primary",
                          return_value={"status": "ok"}) as mock_fn:
            handle_set_category_primary({
                "category": "Personal - Food & Drinks",
                "card_id": "dbs-altitude",
                "until_date": "2026-04-30",
                "earn_rate": 10,
            })
        mock_fn.assert_called_once_with(
            category="Personal - Food & Drinks",
            card_id="dbs-altitude",
            until_date="2026-04-30",
            earn_rate=10.0,
            cap=None,
            fallback_card_id=None,
            fallback_earn_rate=None,
        )

    def test_set_category_primary_handler_omits_empties(self):
        from tools.expense_sheets_tool import handle_set_category_primary
        from tools import card_optimiser
        with patch.object(card_optimiser, "set_category_primary",
                          return_value={"status": "ok"}) as mock_fn:
            handle_set_category_primary({
                "category": "Groceries",
                "card_id": "uob-prvi",
                "until_date": "",
            })
        call = mock_fn.call_args
        assert call.kwargs["until_date"] is None
        assert call.kwargs["earn_rate"] is None


# --- Bug-fix regressions from commit 9926a5a (already on this branch) -------


class TestEmptyMonthRegression:
    """get_spending_summary / generate_spending_report / read_transactions /
    write_insight must not crash when called with month=\"\"."""

    def test_get_spending_summary_empty_string(self):
        from tools import sheets_client
        with patch.object(sheets_client, "read_transactions", return_value=[]), \
             patch.object(sheets_client, "read_budgets", return_value=[]):
            result = sheets_client.get_spending_summary("")
        assert isinstance(result, dict)

    def test_generate_spending_report_empty_string(self):
        from tools import sheets_client
        with patch.object(sheets_client, "read_transactions", return_value=[]), \
             patch.object(sheets_client, "read_budgets", return_value=[]):
            result = sheets_client.generate_spending_report("")
        assert isinstance(result, dict)
        assert "month" in result


class TestTransientGspreadDetection:
    """_is_transient_gspread_error gates the retry wrapper in
    get_spreadsheet(). It should return True for 429/5xx, False otherwise."""

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_transient_status_codes(self, status):
        from tools.sheets_client import _is_transient_gspread_error
        exc = MagicMock()
        exc.response.status_code = status
        assert _is_transient_gspread_error(exc) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_non_transient_status_codes(self, status):
        from tools.sheets_client import _is_transient_gspread_error
        exc = MagicMock()
        exc.response.status_code = status
        assert _is_transient_gspread_error(exc) is False

    def test_no_response_attribute(self):
        from tools.sheets_client import _is_transient_gspread_error
        assert _is_transient_gspread_error(RuntimeError("unrelated")) is False


class TestGetSpreadsheetRetry:
    """get_spreadsheet() retries on transient gspread errors."""

    def _make_apierror(self, status_code):
        """Build a gspread APIError without calling its init (which needs a
        real Response). The retry code only reads `.response.status_code`."""
        err = gspread.exceptions.APIError.__new__(gspread.exceptions.APIError)
        resp = MagicMock()
        resp.status_code = status_code
        err.response = resp
        return err

    def test_succeeds_after_retry(self):
        from tools import sheets_client
        client = MagicMock()
        client.open_by_key.side_effect = [
            self._make_apierror(503),
            self._make_apierror(503),
            "ss-ok",
        ]
        with patch.object(sheets_client, "get_client", return_value=client), \
             patch.object(sheets_client.time, "sleep"):
            result = sheets_client.get_spreadsheet()
        assert result == "ss-ok"
        assert client.open_by_key.call_count == 3

    def test_fails_fast_on_403(self):
        from tools import sheets_client
        client = MagicMock()
        client.open_by_key.side_effect = self._make_apierror(403)
        with patch.object(sheets_client, "get_client", return_value=client), \
             patch.object(sheets_client.time, "sleep"):
            with pytest.raises(gspread.exceptions.APIError):
                sheets_client.get_spreadsheet()
        assert client.open_by_key.call_count == 1

    def test_gives_up_after_3_attempts(self):
        from tools import sheets_client
        client = MagicMock()
        client.open_by_key.side_effect = [
            self._make_apierror(503),
            self._make_apierror(503),
            self._make_apierror(503),
        ]
        with patch.object(sheets_client, "get_client", return_value=client), \
             patch.object(sheets_client.time, "sleep"):
            with pytest.raises(gspread.exceptions.APIError):
                sheets_client.get_spreadsheet()
        assert client.open_by_key.call_count == 3
