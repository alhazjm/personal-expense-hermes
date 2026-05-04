# Personal Expense Hermes (PEH)

**A production LLM agent for personal finance — built as a study in treating AI systems like real software.**

Telegram bot + Gmail webhook → tool-calling agent → Google Sheets ledger. Runs on a $7/mo Render box, has been categorising my real expenses for months, and exists to learn the patterns that make LLM agents safe to put in production.

→ **For the interesting bits, read [DESIGN.md](DESIGN.md).**
→ Internal deployment & operational notes live in a separate (private) repo.

---

## What it does

I forward bank-alert emails (DBS/UOB) and message my Telegram bot. The agent categorises each transaction with an LLM, learns merchant-to-category mappings over time, writes everything to a Google Sheet (the source of truth), and answers natural-language questions about my budget.

| Message in Telegram | What happens |
|---|---|
| *"How much have I spent on food?"* | Queries the sheet, returns total |
| *"Remind me daily to stop ordering Grab"* | Creates a cron job |
| *"Can I afford a $150 jacket?"* | Checks discretionary budget, gives an assessment |
| *"Reallocate $50 from Dining to Transport"* | Updates budget limits in the sheet |

Behind the scenes there are four skills (`expense-tracker`, `card-optimiser`, `budget-manager`, `weekly-summary`), 24 registered tools, a tier-2 memory layer in the system prompt, and a custom upstream patch that fixes a model-behaviour bug deterministically.

---

## Why this is interesting (the four patterns)

This project exists to demonstrate four production-rigor patterns I think AI engineering teams will care about. Each has a receipt in the code; each has a 1-page writeup in [DESIGN.md](DESIGN.md).

### 1. Treat model behaviour as a debuggable system, not a prompt to wrestle
When MiniMax-M2.7 wouldn't honour prompt-level "stay silent" instructions, I patched the upstream agent loop to exit cleanly when a tool result signals silence. The v1 → v2 iteration is the interesting part — v1 broke an empty-response retry cascade I hadn't anticipated.
→ [`deploy/patches/suppress_reply_on_silent_tools.py`](deploy/patches/suppress_reply_on_silent_tools.py) · [DESIGN.md §1](DESIGN.md#1-debug-the-model-not-the-prompt)

### 2. Fail loud when upstream drifts
The container clones a third-party agent framework and surgically COPYs in only the eight files I own. New tools require updating a hard-coded sed injection; the agent-loop patch anchors on a narrow upstream line. Both abort the Docker build loud if the anchor moves — no silent breakage on deploy.
→ [`Dockerfile`](Dockerfile) · [DESIGN.md §2](DESIGN.md#2-fail-loud-when-upstream-drifts)

### 3. Restraint-shaped roadmap
Most engineering roadmaps are growth-shaped. Mine has a "Things NOT to do" list with reasoning, milestones that mean *stop and re-evaluate*, and an explicit rule against premature abstraction. Each near-term step is annotated with the architectural assumption it validates.
→ [`ROADMAP.md`](ROADMAP.md) · [DESIGN.md §3](DESIGN.md#3-restraint-shaped-roadmap)

### 4. Memory architecture with stated rules, not just naming
Tier-2 (in the system prompt every turn) vs tier-3 (FTS5 episodic, queried on demand). Each tier-2 file carries a *"what does NOT belong here"* section to fight prompt bloat. Skill markdown files are versioned (`expense-tracker v4.9.0`) — almost nobody versions prompts.
→ [`hermes-config/`](hermes-config/) · [`skills/`](skills/) · [DESIGN.md §4](DESIGN.md#4-memory-architecture-with-stated-rules)

---

## Architecture

```
                  Gmail (DBS/UOB alerts)
                            │
                            ▼
                  Google Apps Script
                            │ HMAC-signed webhook
                            ▼
              ┌──────────────────────────────┐
              │       Hermes Agent           │
              │   (tool-calling LLM loop)    │
              │                              │
              │  Skills:                     │
              │   • expense-tracker v4.11    │
              │   • card-optimiser v1.0      │
              │   • budget-manager v3.0      │
              │   • weekly-summary v3.0      │
              │                              │
              │  24 registered tools         │
              │  Tier-2 memory (USER/MEM/    │
              │   SOUL .md, ~50-200 lines)   │
              └──────────────────────────────┘
                  │           │            │
                  ▼           ▼            ▼
            Google Sheets  Telegram    Cron jobs
            (ledger,       (user UI,   (daily/weekly/
             budget,        confirms)   monthly)
             merchant
             map)
```

**LLM:** MiniMax-M2.7 (chosen for cost + Singapore region). Architecture is provider-agnostic — swapping to Claude or GPT is a config change, not a refactor.
**Storage:** Google Sheets is the database. The append-only `Transactions` tab is the source of truth. No Postgres until a second user appears.
**Deploy:** Docker container on Render (Singapore), $7/mo.

---

## Tech stack

| Layer | Choice |
|---|---|
| Agent framework | [hermes-agent](https://github.com/alhazjm/hermes-agent) (custom, pinned by SHA) |
| LLM | MiniMax-M2.7 |
| Storage | Google Sheets via `gspread` |
| Ingress | Telegram bot + Google Apps Script (HMAC webhook) |
| Egress | Telegram + scheduled cron jobs |
| Deploy | Docker on Render, Singapore region |
| Tests | pytest, ~200 tests with a stubbed registry for fast unit runs |

---

## Repository layout

```
.
├── README.md                  # You are here
├── DESIGN.md                  # The four patterns, in depth
├── ROADMAP.md                 # What's next, and what's deliberately not next
├── CLAUDE.md                  # Repo guide for agents (gotchas, SHA bump checklist)
├── Dockerfile                 # Build shell — surgical COPY pattern, fail-loud anchors
├── deploy/
│   ├── start.sh               # Container entrypoint (stub here; real one is private)
│   └── patches/
│       └── suppress_reply_on_silent_tools.py   # The agent-loop patch (Pattern #1)
├── hermes-config/             # Tier-2 memory templates (sanitised)
│   ├── USER.md.example
│   ├── MEMORY.md.example
│   ├── SOUL.md.example
│   └── cli-config.yaml        # Hermes gateway routes + per-platform toolsets
├── skills/                    # Versioned skill markdown
│   ├── expense-tracker/SKILL.md   # v4.11.0
│   ├── card-optimiser/SKILL.md    # v1.0.0
│   ├── budget-manager/SKILL.md    # v3.0.0
│   └── weekly-summary/SKILL.md    # v3.0.0
├── tools/                     # Custom tool implementations
│   ├── sheets_client.py       # Header-name column lookups (Pattern #2)
│   ├── expense_sheets_tool.py # All 24 @register_tool calls live here (handlers
│   │                          # delegate to card_optimiser for the 5 card tools)
│   └── card_optimiser.py      # Card-optimiser logic, setup-gated
├── apps-script/Code.gs        # Gmail webhook source — DBS/UOB email parsers + HMAC
├── cron/setup-cron-jobs.sh    # Multi-skill cron registration (daily/Friday/monthly/Sunday)
├── sheets-template/README.md  # Full sheet schema + migration notes
├── tests/                     # ~200 tests with a fake registry stub
└── conftest.py                # Stubs tools.registry so tests run without hermes-agent
```

---

## Status

- Deployed and in daily personal use since late 2025.
- Four skills shipped (`expense-tracker`, `card-optimiser`, `budget-manager`, `weekly-summary`).
- 24 tools registered.
- Code in this repo is the **portfolio-curated subset** of the deployed system. Operational config, sensitive memory files, and deployment scripts live in a separate private repo.

---

## Cost

| Item | Monthly cost |
|---|---|
| Render Starter (Singapore) | $7 |
| MiniMax API | ~$1–2 |
| Google Sheets / Apps Script / Telegram | Free |
| **Total** | **~$8–9** |

---

## Local development

This repo is structured as a showcase rather than a one-click deploy. The deployment story (Render, env vars, persistent disk, the surgical Dockerfile pattern) lives in the private operational repo.

To run the tests locally:

```bash
pip install pytest gspread google-auth cffi
pytest tests/
```

Note: `cffi` is a hidden requirement — `gspread` imports `cryptography`, which panics without `_cffi_backend`. Cost me an evening; documented here so it doesn't cost you one.

---

## What's next

See [ROADMAP.md](ROADMAP.md). Briefly: Obsidian daily notes as a join key across domains, then a third skill (journal/voice notes), then an FTS5 cross-skill recall layer. Storage adapter and multi-tenancy are deliberately deferred until they're needed.

### Future-state ideas (prototyped but not in this repo)

- **Travel mode.** When a non-SGD transaction arrives during a trip, route it through a `TravelMode` tab (date range + per-bucket allocations: food / transport / lodging / activities / misc) instead of the regular Budget. The bucket tag goes into Notes as `[bucket:X]`; per-bucket overspend triggers an automatic Telegram nudge at 80% / 100%, deduped per (trip, bucket, threshold). Validates the same event-driven nudge contract as `card-optimiser` against a different domain. Lives in the deployed instance; not ported here to keep the public surface narrow at 24 tools.

---

## Contact

Built by [Hadi](https://github.com/alhazjm). If you're hiring for an AI engineering / enablement role and want the deeper version of any of these patterns, [DESIGN.md](DESIGN.md) is the place to start, or reach out directly.
