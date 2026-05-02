"""
Tests for DBS/UOB email parsing logic.
Python port of the Apps Script regex patterns for local testability.
"""

import re
import pytest


def parse_dbs(body: str) -> dict | None:
    """Parse DBS PayLah! and Card transaction emails.

    Currency code is captured dynamically (any 3-letter ISO code), so
    foreign-currency cards (`Amount: IDR2334082.00`, `Amount: USD45.00`)
    parse cleanly. Conversion to SGD happens in the JS production code
    (`convertToSGD`); this Python mirror just exposes the original
    currency for regex parity testing.
    """
    amount_match = re.search(r"Amount:\s*([A-Z]{3})\s?([\d,]+\.?\d*)", body, re.IGNORECASE)
    merchant_match = re.search(r"To:\s*(.+)", body, re.IGNORECASE | re.MULTILINE)
    date_match = re.search(r"Date\s*&\s*Time:\s*(\d{1,2}\s+\w+)\s+", body, re.IGNORECASE)
    from_match = re.search(r"From:\s*(.+)", body, re.IGNORECASE | re.MULTILINE)

    if not amount_match or not merchant_match:
        return None

    from_text = from_match.group(1).strip() if from_match else ""
    is_paylah = "paylah" in from_text.lower()
    card_match = re.search(r"ending\s+(\d{4})", from_text, re.IGNORECASE)

    return {
        "bank": "DBS",
        "type": "paylah" if is_paylah else "card",
        "amount": float(amount_match.group(2).replace(",", "")),
        "currency": amount_match.group(1).upper(),
        "merchant": merchant_match.group(1).strip(),
        "date": date_match.group(1).strip() if date_match else None,
        "card_last_four": card_match.group(1) if card_match else None,
        "payment_method": from_text,
    }


def parse_uob(body: str) -> dict | None:
    """Parse UOB credit card transaction emails.

    Same dynamic-currency-code approach as parse_dbs.
    """
    amount_match = re.search(
        r"transaction\s+of\s+([A-Z]{3})\s+([\d,]+\.?\d*)\s+was\s+made",
        body,
        re.IGNORECASE,
    )
    card_match = re.search(r"Card\s+ending\s+(\d{4})", body, re.IGNORECASE)
    date_match = re.search(r"on\s+(\d{2}/\d{2}/\d{2,4})", body, re.IGNORECASE)
    merchant_match = re.search(r"at\s+(.+?)(?:\.\s*If|\s*$)", body, re.IGNORECASE | re.MULTILINE)

    if not amount_match:
        return None

    merchant = merchant_match.group(1).strip() if merchant_match else "Unknown"
    merchant = re.sub(r"[.\s]+$", "", merchant)

    return {
        "bank": "UOB",
        "type": "card",
        "amount": float(amount_match.group(2).replace(",", "")),
        "currency": amount_match.group(1).upper(),
        "merchant": merchant,
        "date": date_match.group(1) if date_match else None,
        "card_last_four": card_match.group(1) if card_match else None,
    }


class TestDBSPayLah:
    BODY = (
        "Transaction Ref: IPS77579784493433610\n"
        "Dear Sir / Madam,\n"
        "We refer to your PayLah! Scan & Pay Transfer dated 10 Apr.\n"
        "Date & Time: 10 Apr 13:10 (SGT)\n"
        "Amount: SGD25.00\n"
        "From: PayLah! Wallet (Mobile ending 2345)\n"
        "To: EXAMPLE MERCHANT NAME\n"
    )

    def test_parses_amount(self):
        result = parse_dbs(self.BODY)
        assert result["amount"] == 25.00

    def test_parses_merchant(self):
        result = parse_dbs(self.BODY)
        assert result["merchant"] == "EXAMPLE MERCHANT NAME"

    def test_identifies_paylah(self):
        result = parse_dbs(self.BODY)
        assert result["type"] == "paylah"

    def test_parses_date(self):
        result = parse_dbs(self.BODY)
        assert result["date"] == "10 Apr"

    def test_parses_mobile_ending(self):
        result = parse_dbs(self.BODY)
        assert result["card_last_four"] == "2345"


class TestDBSCard:
    BODY = (
        "Transaction Ref: SP130091336000000064400\n"
        "Dear Sir / Madam,\n"
        "We refer to your card transaction request dated 14/04/26.\n"
        "Date & Time: 14 APR 06:44 (SGT)\n"
        "Amount: SGD3.08\n"
        "From: DBS/POSB card ending 1234\n"
        "To: BUS/MRT\n"
    )

    def test_parses_amount(self):
        result = parse_dbs(self.BODY)
        assert result["amount"] == 3.08

    def test_parses_merchant(self):
        result = parse_dbs(self.BODY)
        assert result["merchant"] == "BUS/MRT"

    def test_identifies_card(self):
        result = parse_dbs(self.BODY)
        assert result["type"] == "card"

    def test_parses_card_ending(self):
        result = parse_dbs(self.BODY)
        assert result["card_last_four"] == "1234"

    def test_parses_payment_method(self):
        result = parse_dbs(self.BODY)
        assert "DBS/POSB card ending 1234" in result["payment_method"]


class TestUOB:
    BODY = (
        "A transaction of SGD 55.00 was made with your UOB Card ending 3456 "
        "on 12/04/26 at UrbanCompany. If unauthorised, call 24/7 Fraud Hotline now"
    )

    def test_parses_amount(self):
        result = parse_uob(self.BODY)
        assert result["amount"] == 55.00

    def test_parses_merchant(self):
        result = parse_uob(self.BODY)
        assert result["merchant"] == "UrbanCompany"

    def test_parses_date_two_digit_year(self):
        result = parse_uob(self.BODY)
        assert result["date"] == "12/04/26"

    def test_parses_card_ending(self):
        result = parse_uob(self.BODY)
        assert result["card_last_four"] == "3456"

    def test_no_match_returns_none(self):
        body = "Your UOB statement is ready for viewing"
        assert parse_uob(body) is None


class TestForeignCurrency:
    """Regression tests: pre-fix, these would all return None because the
    regexes hardcoded `SGD`. Post-fix, the currency code is captured and
    exposed as `currency` for the JS layer's `convertToSGD` to normalise."""

    def test_dbs_card_idr(self):
        # Real failure case from 2026-04-26: DBS card txn at Flyscoot.com
        # in IDR was silently dropped because the SGD-only regex didn't match.
        body = (
            "Transaction Ref: SP1300678880000000235123\n"
            "Dear Sir / Madam,\n"
            "We refer to your card transaction request dated 26/04/26.\n"
            "Date & Time: 26 APR 23:51 (SGT)\n"
            "Amount: IDR2334082.00\n"
            "From: DBS/POSB card ending 7395\n"
            "To: Flyscoot.com IDR\n"
        )
        result = parse_dbs(body)
        assert result is not None
        assert result["currency"] == "IDR"
        assert result["amount"] == 2334082.00
        assert result["merchant"] == "Flyscoot.com IDR"
        assert result["card_last_four"] == "7395"

    def test_dbs_card_usd(self):
        body = (
            "Date & Time: 14 APR 06:44 (SGT)\n"
            "Amount: USD45.00\n"
            "From: DBS/POSB card ending 1234\n"
            "To: AMAZON.COM\n"
        )
        result = parse_dbs(body)
        assert result["currency"] == "USD"
        assert result["amount"] == 45.00

    def test_dbs_card_eur_with_space(self):
        # Some bank emails put a space between the code and the amount.
        body = (
            "Date & Time: 14 APR 06:44 (SGT)\n"
            "Amount: EUR 18.50\n"
            "From: DBS/POSB card ending 1234\n"
            "To: SOMEPLACE PARIS\n"
        )
        result = parse_dbs(body)
        assert result["currency"] == "EUR"
        assert result["amount"] == 18.50

    def test_dbs_sgd_still_parses(self):
        # Regression guard for the common case.
        body = (
            "Date & Time: 14 APR 06:44 (SGT)\n"
            "Amount: SGD3.08\n"
            "From: DBS/POSB card ending 1234\n"
            "To: BUS/MRT\n"
        )
        result = parse_dbs(body)
        assert result["currency"] == "SGD"
        assert result["amount"] == 3.08

    def test_uob_eur(self):
        body = (
            "A transaction of EUR 18.50 was made with your UOB Card "
            "ending 3456 on 12/04/26 at PARIS HOTEL. If unauthorised, "
            "call 24/7 Fraud Hotline now"
        )
        result = parse_uob(body)
        assert result is not None
        assert result["currency"] == "EUR"
        assert result["amount"] == 18.50
        assert result["merchant"] == "PARIS HOTEL"

    def test_uob_idr(self):
        body = (
            "A transaction of IDR 2334082.00 was made with your UOB Card "
            "ending 3456 on 26/04/26 at JAKARTA HOTEL. If unauthorised, "
            "call 24/7 Fraud Hotline now"
        )
        result = parse_uob(body)
        assert result["currency"] == "IDR"
        assert result["amount"] == 2334082.00

    def test_uob_sgd_still_parses(self):
        body = (
            "A transaction of SGD 55.00 was made with your UOB Card "
            "ending 3456 on 12/04/26 at UrbanCompany. If unauthorised, "
            "call 24/7 Fraud Hotline now"
        )
        result = parse_uob(body)
        assert result["currency"] == "SGD"
        assert result["amount"] == 55.00
