"""Card optimiser — category-keyed earn-rate strategy + event-driven nudges.

See `skills/card-optimiser/SKILL.md` for the user-facing flow and
`sheets-template/README.md` for the tab schemas. Everything in this module
is gated behind `_check_setup()` — tools return
`{"status": "setup_required", ...}` until the user populates the `Cards` and
`CardStrategy` tabs (with at least one row each and a `_default` sentinel row
in `CardStrategy`).

The one cross-skill touchpoint is `maybe_send_post_cap_nudge`, called from
`handle_log_expense` after a transaction is logged. It MUST NOT raise — the
caller wraps it in try/except as a defensive second layer, but this module
should swallow its own errors and return a reason string.
"""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta

import gspread

from tools import sheets_client
from tools.sheets_client import (
    TRANSACTIONS_SHEET,
    _get_column_index,
    _is_pending,
    get_spreadsheet,
)

CARDS_SHEET = "Cards"
CARD_STRATEGY_SHEET = "CardStrategy"
CARD_NUDGE_LOG_SHEET = "CardNudgeLog"
DEFAULT_STRATEGY_KEY = "_default"

_STRATEGY_HEADERS = [
    "category",
    "primary_card_id",
    "primary_cap",
    "primary_earn_rate",
    "fallback_card_id",
    "fallback_earn_rate",
    "promo_active_until",
    "notes",
]
_NUDGE_LOG_HEADERS = [
    "timestamp",
    "cycle_window",
    "card_id",
    "category",
    "threshold",
    "triggering_txn_id",
    "fallback_card_id",
]


# --- Sheet I/O helpers ------------------------------------------------------


def _setup_error(message: str) -> dict:
    return {"status": "setup_required", "message": message}


def _open_sheet(name: str):
    try:
        return get_spreadsheet().worksheet(name)
    except gspread.WorksheetNotFound:
        return None


def _as_float(val) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def _as_int(val, default: int = 0) -> int:
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def read_cards() -> list[dict]:
    """Read the Cards tab. Returns `[]` if the tab is missing."""
    ws = _open_sheet(CARDS_SHEET)
    if ws is None:
        return []
    records = ws.get_all_records()
    cards = []
    for r in records:
        card_id = str(r.get("card_id", "")).strip()
        if not card_id:
            continue
        cycle_day = _as_int(r.get("cycle_start_day"), default=1)
        cards.append({
            "card_id": card_id,
            "display_name": str(r.get("display_name", "")).strip() or card_id,
            "payment_method_pattern": str(r.get("payment_method_pattern", "")).strip(),
            "cycle_start_day": max(1, min(31, cycle_day)),
            "min_spend_bonus": _as_float(r.get("min_spend_bonus")),
            "notes": str(r.get("notes", "")),
        })
    return cards


def read_card_strategy() -> list[dict]:
    """Read the CardStrategy tab. Returns `[]` if the tab is missing."""
    ws = _open_sheet(CARD_STRATEGY_SHEET)
    if ws is None:
        return []
    records = ws.get_all_records()
    rows = []
    for r in records:
        category = str(r.get("category", "")).strip()
        if not category:
            continue
        rows.append({
            "category": category,
            "primary_card_id": str(r.get("primary_card_id", "")).strip(),
            "primary_cap": _as_float(r.get("primary_cap")),
            "primary_earn_rate": _as_float(r.get("primary_earn_rate")),
            "fallback_card_id": str(r.get("fallback_card_id", "")).strip(),
            "fallback_earn_rate": _as_float(r.get("fallback_earn_rate")),
            "promo_active_until": str(r.get("promo_active_until", "")).strip(),
            "notes": str(r.get("notes", "")),
        })
    return rows


def _check_setup() -> dict | None:
    """None if ready; setup_required dict otherwise."""
    cards = read_cards()
    strategies = read_card_strategy()
    missing = []
    if not cards:
        missing.append("`Cards` tab is empty or missing")
    if not strategies:
        missing.append("`CardStrategy` tab is empty or missing")
    elif not any(s["category"] == DEFAULT_STRATEGY_KEY for s in strategies):
        missing.append(
            f"`CardStrategy` needs a sentinel row with category=`{DEFAULT_STRATEGY_KEY}`"
        )
    if missing:
        return _setup_error(
            "Card optimiser not ready: "
            + "; ".join(missing)
            + ". See sheets-template/README.md (Tab 7: Cards / Tab 8: CardStrategy)."
        )
    return None


# --- Resolution helpers -----------------------------------------------------


def _find_card_by_payment_method(payment_method: str,
                                 cards: list[dict]) -> dict | None:
    """Longest `payment_method_pattern` substring match wins, case-insensitive."""
    if not payment_method:
        return None
    pm_lower = payment_method.lower()
    best = None
    best_len = 0
    for c in cards:
        pat = (c.get("payment_method_pattern") or "").lower().strip()
        if not pat:
            continue
        if pat in pm_lower and len(pat) > best_len:
            best = c
            best_len = len(pat)
    return best


def _find_strategy(category: str, strategies: list[dict]) -> dict | None:
    """Exact category match first, else the `_default` sentinel row, else None."""
    for s in strategies:
        if s["category"] == category:
            return s
    for s in strategies:
        if s["category"] == DEFAULT_STRATEGY_KEY:
            return s
    return None


def _parse_date(value) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# --- Cycle math -------------------------------------------------------------


def _cycle_window(cycle_start_day: int, as_of: date) -> tuple[date, date]:
    """Return the cycle window (start, end inclusive) containing `as_of`.

    `cycle_start_day` is clamped to the month length (e.g. day 31 in Feb
    clamps to 28). End date is (start of next cycle) − 1 day.
    """
    csd = max(1, min(31, int(cycle_start_day)))
    y, m = as_of.year, as_of.month
    cur_day = min(csd, monthrange(y, m)[1])
    cur_month_start = date(y, m, cur_day)

    if as_of >= cur_month_start:
        start = cur_month_start
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        next_day = min(csd, monthrange(ny, nm)[1])
        next_start = date(ny, nm, next_day)
    else:
        py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
        prev_day = min(csd, monthrange(py, pm)[1])
        start = date(py, pm, prev_day)
        next_start = cur_month_start
    return (start, next_start - timedelta(days=1))


# --- Spend aggregation ------------------------------------------------------


def _txn_rows_in_cycle(card: dict, category: str | None,
                       as_of: date) -> list[dict]:
    """Read Transactions for `card` within the cycle window containing
    `as_of`. Optional category filter (exact match). Always excludes
    UNCATEGORIZED and backfill rows."""
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    records = ws.get_all_records()
    start, end = _cycle_window(card["cycle_start_day"], as_of)
    pat = (card.get("payment_method_pattern") or "").lower().strip()
    rows = []
    for r in records:
        if _is_pending(r):
            continue
        if str(r.get("Source", "")).strip().lower() == "backfill":
            continue
        row_date = _parse_date(r.get("Date", ""))
        if row_date is None or not (start <= row_date <= end):
            continue
        row_pm = str(r.get("Payment Method", "")).lower()
        if pat and pat not in row_pm:
            continue
        if category is not None:
            if str(r.get("Category", "")).strip() != category:
                continue
        rows.append(r)
    return rows


def _spend_in_cycle(card: dict, category: str | None, as_of: date) -> float:
    return round(
        sum(_as_float(r.get("Amount")) for r in _txn_rows_in_cycle(card, category, as_of)),
        2,
    )


def _cap_status(spent: float, cap: float) -> str:
    """`ok` <80%, `warning` 80–99%, `capped` ≥100%. Unlimited cap (≤0) is `ok`."""
    if cap <= 0:
        return "ok"
    pct = spent / cap
    if pct < 0.80:
        return "ok"
    if pct < 1.00:
        return "warning"
    return "capped"


def _percent(spent: float, cap: float) -> float:
    return round(spent / cap * 100, 1) if cap > 0 else 0.0


def _has_cap_room(spent_before: float, cap: float) -> bool:
    return cap <= 0 or spent_before < cap


# --- Promo expiry reversion -------------------------------------------------


def _lazy_revert_expired_promos() -> list[dict]:
    """Scan CardStrategy for rows where `promo_active_until` is in the past,
    and restore the prior config from the `PREV:` snapshot embedded in the
    `notes` column. Returns one dict per reverted row."""
    ws = _open_sheet(CARD_STRATEGY_SHEET)
    if ws is None:
        return []
    all_values = ws.get_all_values()
    if not all_values:
        return []
    header = all_values[0]
    col = {name: i + 1 for i, name in enumerate(header)}
    if "promo_active_until" not in col or "notes" not in col:
        return []

    today = date.today()
    reverted = []
    for idx, row in enumerate(all_values[1:], start=2):
        row_padded = list(row) + [""] * (len(header) - len(row))
        until_raw = row_padded[col["promo_active_until"] - 1].strip()
        if not until_raw:
            continue
        expiry = _parse_date(until_raw)
        if expiry is None or expiry >= today:
            continue

        notes = row_padded[col["notes"] - 1] or ""
        if " ||PREV:" in notes:
            clean_notes, prev_blob = notes.split(" ||PREV:", 1)
            restored = {}
            for pair in prev_blob.strip().split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    restored[k.strip()] = v.strip()
            for h, val in restored.items():
                if h in col:
                    ws.update_cell(idx, col[h], val)
            ws.update_cell(idx, col["notes"], clean_notes.strip())
            reverted.append({
                "category": row_padded[col["category"] - 1],
                "restored_from": restored,
            })
        else:
            reverted.append({
                "category": row_padded[col["category"] - 1],
                "restored_from": "expiry_only",
            })
        ws.update_cell(idx, col["promo_active_until"], "")
    return reverted


# --- CardNudgeLog -----------------------------------------------------------


def _ensure_nudge_log():
    ss = get_spreadsheet()
    try:
        return ss.worksheet(CARD_NUDGE_LOG_SHEET)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(
            title=CARD_NUDGE_LOG_SHEET, rows=300, cols=len(_NUDGE_LOG_HEADERS)
        )
        ws.append_row(_NUDGE_LOG_HEADERS, value_input_option="USER_ENTERED")
        return ws


def _read_nudge_log() -> list[dict]:
    ws = _open_sheet(CARD_NUDGE_LOG_SHEET)
    if ws is None:
        return []
    return ws.get_all_records()


def _already_nudged(cycle_window: str, card_id: str, category: str,
                    threshold: int) -> bool:
    for r in _read_nudge_log():
        if (str(r.get("cycle_window", "")) == cycle_window
                and str(r.get("card_id", "")) == card_id
                and str(r.get("category", "")) == category
                and str(r.get("threshold", "")) == str(threshold)):
            return True
    return False


def _record_nudge(cycle_window: str, card_id: str, category: str,
                  threshold: int, txn_id: str, fallback_card_id: str) -> None:
    ws = _ensure_nudge_log()
    ws.append_row(
        [
            datetime.now().isoformat(timespec="seconds"),
            cycle_window,
            card_id,
            category,
            threshold,
            txn_id,
            fallback_card_id or "",
        ],
        value_input_option="USER_ENTERED",
    )


def _last_n_in_category_on_card(category: str, card_id: str,
                                cards: list[dict], n: int = 2) -> bool:
    """True if the last `n` non-pending, non-backfill transactions in
    `category` (across all time) landed on `card_id` by payment-method
    substring match. Used for silent-when-already-switched."""
    cards_by_id = {c["card_id"]: c for c in cards}
    card = cards_by_id.get(card_id)
    if card is None:
        return False
    pat = (card.get("payment_method_pattern") or "").lower().strip()
    if not pat:
        return False

    try:
        ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
        records = ws.get_all_records()
    except Exception:
        return False

    matched = []
    for r in reversed(records):
        if _is_pending(r):
            continue
        if str(r.get("Source", "")).strip().lower() == "backfill":
            continue
        if str(r.get("Category", "")).strip() != category:
            continue
        matched.append(r)
        if len(matched) >= n:
            break
    if len(matched) < n:
        return False
    return all(pat in str(r.get("Payment Method", "")).lower() for r in matched)


def _find_triggering_txn_id(card: dict, category: str, amount: float,
                            txn_date: date) -> str:
    """Best-effort lookup of the txn_id that just triggered the nudge."""
    try:
        ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
        records = ws.get_all_records()
    except Exception:
        return ""
    pat = (card.get("payment_method_pattern") or "").lower().strip()
    target = txn_date.isoformat()
    for r in reversed(records):
        if str(r.get("Date", ""))[:10] != target:
            continue
        if str(r.get("Category", "")).strip() != category:
            continue
        if abs(_as_float(r.get("Amount")) - amount) > 0.01:
            continue
        if pat and pat not in str(r.get("Payment Method", "")).lower():
            continue
        return str(r.get("txn_id", ""))
    return ""


# --- Post-cap nudge (called from handle_log_expense) ------------------------


def maybe_send_post_cap_nudge(payment_method: str, category: str,
                              amount: float, txn_date: str) -> dict:
    """Fire a Telegram nudge when a transaction pushes a card past 80% or
    100% of its monthly category cap — at most once per (cycle, card,
    category, threshold). Suppresses the 100% nudge if the user already
    switched to the fallback card after the 80% nudge.

    MUST NOT raise — callers wrap in try/except too, but this function is
    designed to always return a dict with `sent: bool` + a `reason`.
    """
    try:
        gate = _check_setup()
        if gate is not None:
            return {"sent": False, "reason": "setup"}

        cards = read_cards()
        strategies = read_card_strategy()
        card = _find_card_by_payment_method(payment_method, cards)
        if card is None:
            return {"sent": False, "reason": "unmapped_payment_method"}

        strat = _find_strategy(category, strategies)
        if strat is None:
            return {"sent": False, "reason": "no_strategy"}
        if strat["primary_card_id"] != card["card_id"]:
            return {"sent": False, "reason": "not_primary_for_category"}

        cap = strat["primary_cap"]
        if cap <= 0:
            return {"sent": False, "reason": "no_cap"}

        as_of = _parse_date(txn_date) or date.today()
        spent_after = _spend_in_cycle(card, category, as_of)
        spent_before = max(0.0, spent_after - amount)
        pct_before = spent_before / cap
        pct_after = spent_after / cap

        threshold = None
        if pct_before < 1.00 <= pct_after:
            threshold = 100
        elif pct_before < 0.80 <= pct_after:
            threshold = 80

        if threshold is None:
            return {"sent": False, "reason": "no_threshold_crossed"}

        start, end = _cycle_window(card["cycle_start_day"], as_of)
        cycle_key = f"{start.isoformat()}_{end.isoformat()}"

        if _already_nudged(cycle_key, card["card_id"], category, threshold):
            return {"sent": False, "reason": "already_nudged_this_cycle"}

        fallback_id = strat.get("fallback_card_id", "")
        fallback_rate = strat.get("fallback_earn_rate", 0.0)
        cards_by_id = {c["card_id"]: c for c in cards}
        fallback = cards_by_id.get(fallback_id)

        # Silent-when-already-switched: suppress the 100% nudge if the 80%
        # already fired AND the user has been using the fallback on the
        # last N transactions in this category.
        if (threshold == 100
                and fallback_id
                and _already_nudged(cycle_key, card["card_id"], category, 80)
                and _last_n_in_category_on_card(category, fallback_id, cards, n=2)):
            return {"sent": False, "reason": "silent_already_switched"}

        if threshold == 100:
            lead = f"⚠️ {card['display_name']} just hit its {category} cap."
        else:
            pct_display = int(pct_after * 100)
            lead = (
                f"⚠️ {card['display_name']} is at {pct_display}% of "
                f"its {category} cap."
            )
        if fallback is not None and fallback_rate > 0:
            body = (
                f"Switch to {fallback['display_name']} — {fallback_rate} mpd "
                f"instead of post-cap rate."
            )
        else:
            body = f"Consider another card for {category} spend until the cycle resets."
        text = f"{lead}\n{body}"

        # Lazy import to avoid a circular import at module load: the
        # expense-tracker tool module imports this module's post-cap hook.
        from tools.expense_sheets_tool import _send_telegram_bubble

        send_result = _send_telegram_bubble(text)
        if not send_result.get("ok"):
            return {
                "sent": False,
                "reason": "telegram_send_failed",
                "error": send_result.get("error", ""),
            }

        triggering_txn_id = _find_triggering_txn_id(card, category, amount, as_of)
        _record_nudge(
            cycle_key, card["card_id"], category, threshold,
            triggering_txn_id, fallback_id,
        )
        return {
            "sent": True,
            "threshold": threshold,
            "card_id": card["card_id"],
            "category": category,
            "fallback_card_id": fallback_id,
            "telegram_message_id": send_result.get("message_id", ""),
        }
    except Exception as exc:
        return {"sent": False, "reason": "exception", "error": str(exc)}


# --- Tool-facing public API -------------------------------------------------


def get_card_cap_status(card_id: str | None = None,
                        category: str | None = None,
                        as_of: str | None = None) -> dict:
    """Return per-(card, category) cycle spend + cap status.

    If `card_id` passed, only that card. If `category` passed, only that
    category within the reported cards. Otherwise reports every card and
    every strategy row whose `primary_card_id` matches the card.
    """
    gate = _check_setup()
    if gate:
        return gate

    cards = read_cards()
    strategies = read_card_strategy()
    as_of_date = _parse_date(as_of) or date.today()
    strategy_by_category = {s["category"]: s for s in strategies}
    cards_by_id = {c["card_id"]: c for c in cards}

    if card_id:
        cards_to_report = [cards_by_id[card_id]] if card_id in cards_by_id else []
    else:
        cards_to_report = cards

    result_cards = []
    for c in cards_to_report:
        start, end = _cycle_window(c["cycle_start_day"], as_of_date)
        if category:
            cats = [category]
        else:
            cats = [
                s["category"]
                for s in strategies
                if s["primary_card_id"] == c["card_id"]
                and s["category"] != DEFAULT_STRATEGY_KEY
            ]

        cat_rows = []
        for cat in cats:
            strat = (
                strategy_by_category.get(cat)
                or strategy_by_category.get(DEFAULT_STRATEGY_KEY)
            )
            if strat is None:
                continue
            spent = _spend_in_cycle(c, cat, as_of_date)
            cap = strat["primary_cap"]
            status = _cap_status(spent, cap)
            earn_current = (
                strat["primary_earn_rate"]
                if status != "capped"
                else strat.get("fallback_earn_rate", 0.0)
            )
            cat_rows.append({
                "category": cat,
                "spent_in_cycle": spent,
                "cap": cap,
                "percent_used": _percent(spent, cap),
                "status": status,
                "earn_rate_current": earn_current,
                "fallback_card_id": strat.get("fallback_card_id", ""),
                "fallback_earn_rate": strat.get("fallback_earn_rate", 0.0),
            })

        result_cards.append({
            "card_id": c["card_id"],
            "display_name": c["display_name"],
            "cycle_start_day": c["cycle_start_day"],
            "cycle_window": [start.isoformat(), end.isoformat()],
            "categories": cat_rows,
        })

    return {
        "status": "ok",
        "as_of": as_of_date.isoformat(),
        "cards": result_cards,
    }


def recommend_card_for(category: str, amount: float | None = None,
                       as_of: str | None = None) -> dict:
    """Recommend the best card for a one-off purchase: primary if the cap
    still has room, fallback otherwise."""
    gate = _check_setup()
    if gate:
        return gate

    cards = read_cards()
    strategies = read_card_strategy()
    as_of_date = _parse_date(as_of) or date.today()

    strat = _find_strategy(category, strategies)
    if strat is None:
        return {
            "status": "error",
            "message": (
                f"No CardStrategy row for '{category}' and no `{DEFAULT_STRATEGY_KEY}` "
                f"row either. Add one to the CardStrategy tab."
            ),
        }

    cards_by_id = {c["card_id"]: c for c in cards}
    primary = cards_by_id.get(strat["primary_card_id"])
    fallback = cards_by_id.get(strat.get("fallback_card_id", ""))

    if primary is None:
        return {
            "status": "error",
            "message": (
                f"CardStrategy points at unknown card_id "
                f"'{strat['primary_card_id']}' — add it to the Cards tab."
            ),
        }

    primary_spent = _spend_in_cycle(primary, category, as_of_date)
    primary_status = _cap_status(primary_spent, strat["primary_cap"])

    if primary_status == "capped" and fallback is not None and strat.get("fallback_earn_rate", 0) > 0:
        rec_card = fallback
        earn_rate = strat["fallback_earn_rate"]
        reason = (
            f"{primary['display_name']} {category} cap already hit this cycle "
            f"— {fallback['display_name']} earns {earn_rate} mpd instead of "
            f"post-cap rate."
        )
    else:
        rec_card = primary
        earn_rate = strat["primary_earn_rate"]
        if primary_status == "warning":
            reason = (
                f"{primary['display_name']} is at "
                f"{_percent(primary_spent, strat['primary_cap'])}% of the "
                f"{category} cap — still the right card until you cross it."
            )
        else:
            reason = (
                f"{primary['display_name']} at {earn_rate} mpd on {category}."
            )

    return {
        "status": "ok",
        "category": category,
        "amount": amount,
        "recommendation": {
            "card_id": rec_card["card_id"],
            "display_name": rec_card["display_name"],
            "earn_rate": earn_rate,
            "reason": reason,
        },
        "primary_status": primary_status,
        "primary_spent_in_cycle": primary_spent,
        "primary_cap": strat["primary_cap"],
    }


def plan_month(month: str | None = None) -> dict:
    """Return the current CardStrategy plan plus MTD cycle status. Also
    lazily reverts any expired promo overrides on first call after expiry."""
    gate = _check_setup()
    if gate:
        return gate

    reverted = _lazy_revert_expired_promos()
    strategies = read_card_strategy()
    cards = read_cards()
    cards_by_id = {c["card_id"]: c for c in cards}
    as_of = date.today()

    plan = []
    for s in strategies:
        primary = cards_by_id.get(s["primary_card_id"])
        if primary is None:
            plan.append({
                "category": s["category"],
                "error": f"unknown primary_card_id '{s['primary_card_id']}'",
            })
            continue
        cat_filter = None if s["category"] == DEFAULT_STRATEGY_KEY else s["category"]
        spent = _spend_in_cycle(primary, cat_filter, as_of)
        plan.append({
            "category": s["category"],
            "primary_card": {
                "card_id": primary["card_id"],
                "display_name": primary["display_name"],
                "earn_rate": s["primary_earn_rate"],
                "cap": s["primary_cap"],
            },
            "fallback_card_id": s.get("fallback_card_id", ""),
            "fallback_earn_rate": s.get("fallback_earn_rate", 0.0),
            "spent_in_cycle": spent,
            "percent_used": _percent(spent, s["primary_cap"]),
            "status": _cap_status(spent, s["primary_cap"]),
            "promo_active_until": s.get("promo_active_until", ""),
        })

    return {
        "status": "ok",
        "month": month or as_of.strftime("%Y-%m"),
        "plan": plan,
        "reverted_promos": reverted,
    }


def review_card_efficiency(month: str | None = None) -> dict:
    """Walk every transaction in `month` and compare the card actually used
    vs. the card that would have been optimal given the cap state at the
    moment of the transaction. Returns miles earned, miles optimal, and a
    list of suboptimal transactions."""
    gate = _check_setup()
    if gate:
        return gate

    if not month:
        month = datetime.now().strftime("%Y-%m")

    cards = read_cards()
    strategies = read_card_strategy()
    cards_by_id = {c["card_id"]: c for c in cards}

    txns = sheets_client.read_transactions(month)
    txns = sorted(txns, key=lambda r: str(r.get("Date", "")))

    miles_actual = 0.0
    miles_optimal = 0.0
    suboptimal = []
    unmapped_count = 0
    spent_running: dict[tuple[str, str], float] = defaultdict(float)

    for r in txns:
        if _is_pending(r):
            continue
        if str(r.get("Source", "")).strip().lower() == "backfill":
            continue
        category = str(r.get("Category", "")).strip()
        amount = _as_float(r.get("Amount"))
        payment_method = str(r.get("Payment Method", ""))
        txn_id = str(r.get("txn_id", ""))
        date_str = str(r.get("Date", ""))

        strat = _find_strategy(category, strategies)
        if strat is None:
            continue

        actual_card = _find_card_by_payment_method(payment_method, cards)
        primary_id = strat["primary_card_id"]
        cap = strat["primary_cap"]
        key_primary = (primary_id, category)
        primary_spent_before = spent_running[key_primary]

        if _has_cap_room(primary_spent_before, cap):
            optimal_id = primary_id
            optimal_rate = strat["primary_earn_rate"]
        else:
            optimal_id = strat.get("fallback_card_id") or primary_id
            optimal_rate = strat.get("fallback_earn_rate", 0.0)

        if actual_card is None:
            unmapped_count += 1
            actual_rate = 0.0
            actual_card_id = "unknown"
        else:
            actual_card_id = actual_card["card_id"]
            if actual_card_id == primary_id:
                spent_running[key_primary] += amount
                actual_rate = (
                    strat["primary_earn_rate"]
                    if _has_cap_room(primary_spent_before, cap)
                    else 0.0
                )
            elif actual_card_id == strat.get("fallback_card_id"):
                actual_rate = strat.get("fallback_earn_rate", 0.0)
            else:
                actual_rate = 0.0

        miles_for_txn_actual = amount * actual_rate
        miles_for_txn_optimal = amount * optimal_rate
        miles_actual += miles_for_txn_actual
        miles_optimal += miles_for_txn_optimal

        if miles_for_txn_optimal - miles_for_txn_actual > 0.5:
            suboptimal.append({
                "txn_id": txn_id,
                "date": date_str,
                "merchant": str(r.get("Merchant", "")),
                "amount": amount,
                "category": category,
                "actual_card": actual_card_id,
                "actual_earn_rate": actual_rate,
                "optimal_card": optimal_id,
                "optimal_earn_rate": optimal_rate,
                "miles_lost": round(miles_for_txn_optimal - miles_for_txn_actual, 1),
            })

    return {
        "status": "ok",
        "month": month,
        "miles_earned_actual": round(miles_actual, 1),
        "miles_earned_optimal": round(miles_optimal, 1),
        "miles_left_on_table": round(miles_optimal - miles_actual, 1),
        "transactions_suboptimal": suboptimal,
        "unmapped_payment_methods": unmapped_count,
    }


def set_category_primary(category: str, card_id: str,
                         until_date: str | None = None,
                         earn_rate: float | None = None,
                         cap: float | None = None,
                         fallback_card_id: str | None = None,
                         fallback_earn_rate: float | None = None) -> dict:
    """Manual promo override: write or update a CardStrategy row. When
    `until_date` is set, the row is tagged with `promo_active_until` and a
    snapshot of the prior values is written into `notes` (prefixed `||PREV:`)
    so `_lazy_revert_expired_promos` can restore it later."""
    gate = _check_setup()
    if gate:
        return gate

    cards = read_cards()
    cards_by_id = {c["card_id"]: c for c in cards}
    if card_id not in cards_by_id:
        return {
            "status": "error",
            "message": (
                f"Unknown card_id '{card_id}' — add it to the Cards tab first."
            ),
        }
    if fallback_card_id and fallback_card_id not in cards_by_id:
        return {
            "status": "error",
            "message": (
                f"Unknown fallback_card_id '{fallback_card_id}' — add it to "
                f"the Cards tab first."
            ),
        }

    ws = _open_sheet(CARD_STRATEGY_SHEET)
    if ws is None:
        return _setup_error("`CardStrategy` tab missing.")

    all_values = ws.get_all_values()
    if not all_values:
        return _setup_error("`CardStrategy` tab has no header row.")
    header = all_values[0]
    col = {name: i + 1 for i, name in enumerate(header)}
    missing_cols = [h for h in _STRATEGY_HEADERS if h not in col]
    if missing_cols:
        return _setup_error(
            f"`CardStrategy` header missing columns: {', '.join(missing_cols)}."
        )

    row_num = None
    prev_row: dict[str, str] = {}
    for idx, row in enumerate(all_values[1:], start=2):
        row_padded = list(row) + [""] * (len(header) - len(row))
        if row_padded[col["category"] - 1].strip() == category:
            row_num = idx
            prev_row = dict(zip(header, row_padded))
            break

    # Build snapshot before overwriting so the reversion path has the data.
    snapshot_notes = ""
    prev_notes_raw = prev_row.get("notes", "") if prev_row else ""
    prev_notes_clean = prev_notes_raw.split(" ||PREV:", 1)[0].strip()
    if until_date and prev_row:
        snapshot_parts = []
        for h in _STRATEGY_HEADERS:
            if h in ("notes", "promo_active_until"):
                continue
            snapshot_parts.append(f"{h}={prev_row.get(h, '')}")
        snapshot_notes = (prev_notes_clean + " ||PREV: " + ",".join(snapshot_parts)).strip()

    def _pick(new_val, old_val, default):
        if new_val is not None:
            return new_val
        if old_val not in (None, ""):
            return old_val
        return default

    new_primary_cap = _pick(cap, prev_row.get("primary_cap"), 0)
    new_primary_earn = _pick(earn_rate, prev_row.get("primary_earn_rate"), 0)
    new_fallback_id = _pick(fallback_card_id, prev_row.get("fallback_card_id"), "")
    new_fallback_earn = _pick(fallback_earn_rate, prev_row.get("fallback_earn_rate"), 0)

    new_row_values = {
        "category": category,
        "primary_card_id": card_id,
        "primary_cap": new_primary_cap,
        "primary_earn_rate": new_primary_earn,
        "fallback_card_id": new_fallback_id,
        "fallback_earn_rate": new_fallback_earn,
        "promo_active_until": until_date or "",
        "notes": snapshot_notes if until_date else prev_notes_clean,
    }

    if row_num:
        for h in _STRATEGY_HEADERS:
            ws.update_cell(row_num, col[h], new_row_values[h])
        action = "updated"
    else:
        ordered = [new_row_values[h] for h in _STRATEGY_HEADERS]
        ws.append_row(ordered, value_input_option="USER_ENTERED")
        action = "created"

    return {
        "status": "ok",
        "action": action,
        "category": category,
        "primary_card_id": card_id,
        "until_date": until_date or "",
    }
