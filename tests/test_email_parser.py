"""
Tests for DBS/UOB email parsing logic.
Python port of the Apps Script regex patterns for local testability.
"""

import re
import pytest


def parse_dbs(body: str) -> dict | None:
    """Parse DBS PayLah! and Card transaction emails."""
    amount_match = re.search(r"Amount:\s*SGD\s?([\d,]+\.?\d*)", body, re.IGNORECASE)
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
        "amount": float(amount_match.group(1).replace(",", "")),
        "currency": "SGD",
        "merchant": merchant_match.group(1).strip(),
        "date": date_match.group(1).strip() if date_match else None,
        "card_last_four": card_match.group(1) if card_match else None,
        "payment_method": from_text,
    }


def parse_uob(body: str) -> dict | None:
    """Parse UOB credit card transaction emails."""
    amount_match = re.search(r"SGD\s+([\d,]+\.?\d*)\s+was\s+made", body, re.IGNORECASE)
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
        "amount": float(amount_match.group(1).replace(",", "")),
        "currency": "SGD",
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
        "From: PayLah! Wallet (Mobile ending 5167)\n"
        "To: LEMBAGA PENTADBIR MASJID AR RAUDHAH\n"
    )

    def test_parses_amount(self):
        result = parse_dbs(self.BODY)
        assert result["amount"] == 25.00

    def test_parses_merchant(self):
        result = parse_dbs(self.BODY)
        assert result["merchant"] == "LEMBAGA PENTADBIR MASJID AR RAUDHAH"

    def test_identifies_paylah(self):
        result = parse_dbs(self.BODY)
        assert result["type"] == "paylah"

    def test_parses_date(self):
        result = parse_dbs(self.BODY)
        assert result["date"] == "10 Apr"

    def test_parses_mobile_ending(self):
        result = parse_dbs(self.BODY)
        assert result["card_last_four"] == "5167"


class TestDBSCard:
    BODY = (
        "Transaction Ref: SP130091336000000064400\n"
        "Dear Sir / Madam,\n"
        "We refer to your card transaction request dated 14/04/26.\n"
        "Date & Time: 14 APR 06:44 (SGT)\n"
        "Amount: SGD3.08\n"
        "From: DBS/POSB card ending 5305\n"
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
        assert result["card_last_four"] == "5305"

    def test_parses_payment_method(self):
        result = parse_dbs(self.BODY)
        assert "DBS/POSB card ending 5305" in result["payment_method"]


class TestUOB:
    BODY = (
        "A transaction of SGD 55.00 was made with your UOB Card ending 0886 "
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
        assert result["card_last_four"] == "0886"

    def test_no_match_returns_none(self):
        body = "Your UOB statement is ready for viewing"
        assert parse_uob(body) is None
