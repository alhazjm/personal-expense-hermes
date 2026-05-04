# CLAUDE.md

Guidance for Claude Code (and future agents) working in this repo. Read this
file before touching anything — it captures the non-obvious bits that cost real
debugging time to rediscover.

## What this repo is

This repo (`personal-expense-hermes`) is the **portfolio-curated subset** of a
personal expense-tracking agent built on top of the
[hermes-agent](https://github.com/alhazjm/hermes-agent) framework. The deployed
instance runs on Render (Singapore, ~$8–9/mo) as a Docker container. The agent:

- Ingests bank-alert emails via a Gmail Apps Script → HMAC-signed webhook
- Accepts manual `/log` messages via Telegram
- Categorises via MiniMax LLM (`MiniMax-M2.7`)
- Writes to a Google Sheet (append-only ledger — source of truth)
- Talks back via Telegram

**The Google Sheet is the database.** There is no Postgres/SQLite/BQ. All
transaction state lives in the sheet, accessed via `tools/sheets_client.py`
using `gspread`.

This repo carries the surface that's interesting to read: the patterns, the
upstream patch, the skills, and the sanitised memory templates. Operational
config (env vars, real `USER.md`/`MEMORY.md`/`SOUL.md`, deploy scripts with
secrets) lives in a private operational repo and never lands here.

## Repo layout

```
Dockerfile                     # Build shell — READ THE GOTCHAS SECTION BELOW
deploy/
  start.sh                     # Container entrypoint (stub in this repo)
  patches/
    suppress_reply_on_silent_tools.py  # Anchored patch to upstream run_agent.py
hermes-config/
  cli-config.yaml              # Hermes gateway config (routes, prompts, toolsets)
  USER.md.example              # Tier-2 identity template (sanitised)
  MEMORY.md.example            # Tier-2 system facts template (sanitised)
  SOUL.md.example              # Tier-2 agent persona template (sanitised)
  install.sh                   # LOCAL DEV ONLY — not used in container
skills/expense-tracker/
  SKILL.md                     # v4 skill: MerchantMap-first, reply-to-edit flow
skills/card-optimiser/
  SKILL.md                     # v1 skill: cap tracking, nudges, monthly scorecard
skills/budget-manager/
  SKILL.md                     # v3 cron skill: daily/weekly/monthly budget warnings
skills/weekly-summary/
  SKILL.md                     # v3 cron skill: Friday + 1st-of-month summary
tools/
  sheets_client.py             # gspread wrapper — all sheet I/O
  expense_sheets_tool.py       # Hermes tool registrations (log_expense, edit_expense, etc.)
  card_optimiser.py            # Card optimiser logic + 5 tools, gated behind sheet setup
apps-script/Code.gs            # Gmail webhook source — bank email parsers + HMAC + FX
cron/setup-cron-jobs.sh        # Multi-skill cron registration (daily/Friday/monthly/Sunday)
sheets-template/README.md      # Sheet schema reference + migration steps
tests/test_expense_sheets_tool.py
tests/test_card_optimiser.py
tests/test_email_parser.py
conftest.py                    # Stubs tools.registry for pytest
```

## Dockerfile gotchas (CRITICAL — read before adding tools or files)

The container is **not** a checkout of this repo. It clones
`alhazjm/hermes-agent` and then `COPY`s only specific files into it. Anything
you add to this repo that isn't explicitly copied in the Dockerfile will be
silently absent at deploy time. Things to know:

1. **Only the paths named explicitly in the Dockerfile are copied into the
   container.** If you add a new file anywhere else, **add a COPY line** or it
   won't ship. Worked example from PEH history: `card_optimiser.py` needed its
   own COPY line when the card-optimiser module was added — without it the
   module was silently absent on deploy even though imports looked right
   locally.

2. **New tools must be added to the sed injection in the Dockerfile.**
   `toolsets.py` in hermes-agent hard-codes which tools the telegram gateway
   exposes. The sed one-liner appends our custom tool names after
   `"send_message",`. When you register a new `@register_tool` in
   `expense_sheets_tool.py`, add its name to that sed list too. Missing it
   means the tool loads in Python but the LLM never sees it.

   The sed line is followed by a `grep -q '"log_expense"'` step that turns
   silent sed-failure (caused by upstream renaming the anchor line) into a
   loud build error.

   Current injected tools (24): `log_expense`, `log_expense_pending`,
   `update_budget`, `get_remaining_budget`, `edit_expense`,
   `delete_expense`, `link_telegram_message`,
   `get_transaction_by_message_id`, `lookup_merchant_category`,
   `learn_merchant_mapping`, `detect_subscription_creep`,
   `undo_last_expense`, `generate_spending_report`, `write_insight`,
   `get_insights`, `render_budget_chart`, `append_journal_entry`,
   `get_journal_entries`, `sweep_missed_transactions`,
   `get_card_cap_status`, `recommend_card_for`, `plan_month`,
   `review_card_efficiency`, `set_category_primary`.

3. **Patch: `deploy/patches/suppress_reply_on_silent_tools.py`** rewrites
   `run_agent.py` to exit the agent loop cleanly when the most-recent tool
   result carries `"assistant_reply_required": false` (patch v2). v1 zeroed
   out `final_response`, but an empty response triggered hermes-agent's
   empty-response nudge/retry cascade — v2 short-circuits the loop instead.
   This is the deterministic fix for the duplicate-Telegram-message bug —
   our `log_expense` tool side-channels its own confirmation bubble, and
   MiniMax-M2.7 doesn't reliably honour prompt-level silence instructions.
   The script anchors on a narrow one-line target in the agent loop and
   exits non-zero on mismatch so the Docker build fails loud. The Dockerfile
   verifies success by grepping for
   `"PEH patch v2: exit agent loop when tool signalled silence"`.

4. **`hermes-config/install.sh` is a local dev helper, NOT a container
   script.** The container's install path is the Dockerfile itself.

## Sheet schema

See `sheets-template/README.md` for the full reference and migration steps.

**`Transactions`** (append-only ledger):
```
Date | Merchant | Amount | Currency | Category | Source | Payment Method | Notes | txn_id | telegram_message_id | idempotency_key
```

- `txn_id` format: `txn_<YYYYMMDD>_<NNN>` (canonical key; generated by
  `log_expense`). Legacy pre-migration rows use `txn_legacy_NNN`.
- `telegram_message_id` lets the skill resolve reply-to-message edits.
- `Source` enum: `email` (webhook), `manual` (user typed), `backfill`
  (imported from statement). Backfill rows MUST be excluded from creep
  detection / anomaly analysis.
- `idempotency_key` is a SHA-256 of `date|merchant|amount|payment_method`,
  checked on every `log_expense` call to prevent webhook double-inserts.

**`Budget`**: per-month layout (13 cols: `Category | Jan..Dec`) OR simple
(`Category | Monthly Limit`). `sheets_client.py` auto-detects by counting
header columns.

**`MerchantMap`**: learned merchant → category mappings.
Lookup rule: **longest matching substring wins.**

**Card-optimiser tabs** (`Cards`, `CardStrategy`, `CardNudgeLog`): see
`skills/card-optimiser/SKILL.md` and `tools/card_optimiser.py` docstring.
All card-optimiser tools return `{"status": "setup_required", ...}` until
these tabs are populated, so the skill is dormant until the user opts in.

### Backwards compatibility

`tools/sheets_client.py` uses `_get_column_index(ws, header)` to look up
columns by header name at runtime. Do NOT hard-code column indices. This is
what lets the same code run against both 8-column legacy sheets and
11-column v4 sheets — and is the cheap future-proofing pattern that makes
sheet-schema changes additive instead of breaking.

## Memory file layering (tier-2)

`USER.md`, `MEMORY.md`, `SOUL.md` are **tier-2 persistent memory** loaded into
the system prompt every turn. Keep them small. This repo carries `.example`
templates only — real values live in the private operational repo.

- **USER.md** — identity & preferences. People, payment methods,
  communication style, tool-use rules. Things that don't change week to week.
- **MEMORY.md** — system/domain facts. Sheet schema, webhook payload shape,
  enums, edge-case quirks, FTS5 episodic search instructions. Machine-ish.
- **SOUL.md** — agent persona. Voice, principles, failure modes. Capped
  around ~50 lines.

**What does NOT go in these files:** transaction data, derived insights,
session history, budget numbers, category lists (those live in the sheet).
`MEMORY.md` has an explicit "what does NOT belong here" section — respect it.

## Testing

```bash
pip install pytest gspread google-auth cffi
pytest tests/
```

- `cffi` is a hidden requirement — `gspread` imports `cryptography` which
  panics without `_cffi_backend`. If you see
  `pyo3_runtime.PanicException: Python API call failed`, you're missing cffi.
- `conftest.py` stubs `tools.registry` with a no-op `_FakeRegistry` so tests
  don't need the full hermes-agent runtime. When you add new
  `@register_tool`-decorated functions, the fake registry silently accepts
  them — tests cover the handler logic directly.

## Skill versioning

Four skill markdown files ship into `/root/.hermes/skills/`:

- `expense-tracker` — **v4.11.0**. The main skill; holds all tool-use rules
  and the categorisation flow.
- `budget-manager` — **v3.0.0**. Invoked by cron jobs for daily/weekly/
  monthly budget warnings. Shares the expense-tracker tool surface.
- `weekly-summary` — **v3.0.0**. Invoked by the Friday and 1st-of-month
  cron jobs. Shares the expense-tracker tool surface.
- `card-optimiser` — **v1.0.0**. Covers cap tracking, card recommendations,
  promo overrides, and the post-cap nudge contract. Added to the Friday
  and 1st-of-month crons. All its tools are gated behind sheet-setup
  readiness (see `tools/card_optimiser.py::_check_setup`).

Per-cron `--skill` flag selects which skill markdown is preloaded. There
are no separate tool files for `budget-manager` / `weekly-summary` —
they are prompt/instruction variants over the same toolset. `card-optimiser`
has its own tool module (`tools/card_optimiser.py`) for 5 tools + the
post-cap nudge helper.

Bump the version in the skill's frontmatter when you make breaking changes
to the tool contracts or the flow. Skills are pure markdown — the LLM reads
them at runtime.

## hermes-agent SHA bump checklist

Upstream is pinned by `HERMES_AGENT_SHA` in the Dockerfile. Two patches
depend on anchor strings in upstream source — both are designed to fail
the Docker build loudly (not silently) if an anchor shifts.

Before bumping `HERMES_AGENT_SHA`, eyeball upstream for these anchors:

1. **`toolsets.py` must still contain `"send_message",`** (the line the
   tool-list sed inserts after). If renamed, update the sed in the
   Dockerfile and the `grep -q '"log_expense"'` follow-up step.

2. **`run_agent.py` must still contain the line
   `                    final_response = assistant_message.content or ""`**
   (20-space indent, in the no-tool-calls branch of the agent loop). If
   refactored, update `ANCHOR` + `INJECTION` in
   `deploy/patches/suppress_reply_on_silent_tools.py` to match the new
   shape.

If you bump the SHA without checking and a patch fails, the `docker build`
aborts with a clear error pointing to the offending step — no silent
breakage at deploy time.

## When adding a new tool — checklist

1. Add handler + `@register_tool` in `tools/expense_sheets_tool.py` (or a
   new file COPY'd in the Dockerfile).
2. Add its name to the sed injection in the Dockerfile.
3. Reference it from `skills/expense-tracker/SKILL.md` (or another skill)
   so the LLM knows when to call it.
4. Add tests in the corresponding `tests/test_*.py`.
5. If it needs a new sheet column, update `_get_column_index` callers and
   the schema doc in `sheets-template/README.md`.

## When adding a new config file — checklist

1. Put it in `hermes-config/` or another existing subdir that's already
   COPY'd.
2. If it's a new directory, add a COPY line to the Dockerfile.
3. If it needs env-var interpolation, add the substitution to
   `deploy/start.sh` — hermes config YAML does not expand env vars.
