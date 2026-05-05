# ROADMAP

Long-term direction for this Hermes deployment. Keep this honest — if a
milestone slips or a principle turns out wrong, rewrite rather than paper over.

## Current state (May 2026)

- **Skill markdown files**: 4 — `expense-tracker` v4.11.0 (primary),
  `card-optimiser` v1.0.0 (its own tool module), and `budget-manager` v3.0.0
  + `weekly-summary` v3.0.0 (both cron-invoked, sharing the expense-tracker
  tool surface; no separate tool files)
- **Ingress**: Telegram bot + Gmail Apps Script HMAC webhook (bank alerts)
- **Egress**: Telegram (plus four scheduled cron deliveries)
- **Storage**: Google Sheets (append-only ledger, MerchantMap, Budget,
  Insights, Cards, CardStrategy, CardNudgeLog)
- **LLM**: MiniMax-M2.7
- **Memory**: tier-2 files (`USER.md`, `MEMORY.md`, `SOUL.md`) + persistent
  disk for sessions
- **Cost**: ~$8–9/mo (Render Singapore + MiniMax + Google free tier)
- **Tool count**: 24 registered, all exposed to the Telegram gateway

> Note: the card-optimiser skill validates the multi-skill routing envisioned
> in near-term step #2 — it has its own tool module gated behind sheet-setup
> readiness. Per-skill toolset scoping in `cli-config.yaml` becomes mandatory
> once a non-finance skill ships.

## Vision

Multi-skill personal assistant with **cross-domain memory**. One agent brain,
multiple life domains, one conversation surface per domain (Slack channels or
equivalent). Narrative layer in Obsidian; structured data in Sheets (today)
or Postgres (if productised). Eventually packageable for 5–50 users without
architectural rewrites.

This is **not** trying to be the "24/7 assistant across every channel" TikTok
framing. That conflates reach with leverage. The leverage is cross-domain
memory; reach is a gateway plugin.

## Principles

1. **Skills are the product.** Storage, LLM provider, and gateway are
   swappable infrastructure.
2. **Obsidian daily notes are the join key** across domains. Structured data
   answers "what"; daily notes answer "why and how it relates".
3. **Tier-2 stays small, tier-3 scales.** Never bloat `MEMORY.md` to store
   history — index it in FTS5 instead.
4. **No premature abstraction.** Ship a skill first, abstract when the
   second skill makes the duplication hurt.
5. **Single-tenant until 3+ non-me users exist daily.** Don't build auth,
   billing, or multi-tenancy on speculation.
6. **Event-driven or user-initiated.** No polling loops. Webhook in, cron
   out, user-initiated in between. This is what keeps API cost flat.

## Near-term (next 1–3 months)

Order matters — each step validates an architectural assumption the next
step depends on.

1. **Start Obsidian daily notes manually.** No agent integration yet. Just
   build the habit and accumulate narrative data that FTS5 will eventually
   index. Structure:
   ```
   Daily/ Merchants/ People/ Cards/ Principles/ Reviews/
   ```
   Template in `hermes-config/` once it stabilises.

2. **Miles/rewards skill.** ✅ Shipped as `card-optimiser` v1.0.0.
   - `Cards` and `CardStrategy` tabs hold per-card earn rates and category
     primaries; `CardNudgeLog` enforces "at most one nudge per (cycle, card,
     category, threshold)"
   - Tools: `recommend_card_for(category, amount?)`, `get_card_cap_status`,
     `plan_month`, `review_card_efficiency`, `set_category_primary`
   - Validated: multi-skill prompt-file structure, setup-gate pattern,
     event-driven post-cap nudge from inside `log_expense`. Per-skill
     toolset scoping in `cli-config.yaml` is still optional today since
     both skills share the finance tool surface — becomes mandatory at
     step #5 when a non-finance skill ships.

3. **Migrate Telegram → Slack.** Telegram supergroups + topics are awkward
   solo. Slack free tier gives channel-per-domain cleanly. Channels:
   `#expenses`, `#cards`, `#journal`, `#agent`. Same container, new gateway.
   Validates: gateway abstraction (if it doesn't exist yet, build it here).

4. **FTS5 indexer.** Cron job on `/data/` that rebuilds
   `/data/fts5.db` from the Obsidian vault + historical sheet rows nightly.
   New tool: `recall(query, scope?)` callable from any skill. Validates:
   cross-skill memory.

## Mid-term (3–9 months)

5. **Fitness/sleep ingestion.** Apple Shortcuts automation POSTs HealthKit
   data to the same HMAC webhook pattern. Skill: `body-log`. Starts
   correlating sleep↓ → spending↑ type insights.

6. **Journal skill.** Voice note → transcription → daily note append.
   Telegram/Slack voice message in, markdown append to today's daily note
   out.

7. **Subscription auditor as a standalone skill.** Extract
   `detect_subscription_creep` + `write_insight` into their own skill with
   monthly cron report. Lower-value than 1-6 but cheap.

8. **Cross-skill `recall` in production.** Once 3+ skills exist, recall
   becomes the main UX: "when did I last …" questions span domains.

## Insurance-today: what to change now for future-sale compatibility

**Nothing structural.** Specifically resist:

- Writing the storage adapter speculatively. Both shipped skills share the
  Sheets backing; abstract when a non-finance skill (`body-log`, `journal`)
  forces the duplication to actually hurt
- Adding tenant_id columns before a second tenant exists
- Building a web UI (Slack/Telegram is the UI)
- Replacing gspread with Postgres (sheets works, quotas aren't close)
- Adding auth middleware (no users yet)

The architecture is already close enough. The CLAUDE.md discipline of
`_get_column_index` (header-name lookups instead of hard-coded indices) is
exactly the kind of cheap future-proofing that pays off. Keep applying that
pattern as new sheet columns/tabs get added.

## Things NOT to do

- **Don't chase local LLM (Qwen on Pi).** Cloud inference cost is not your
  bottleneck. The enthusiast-YouTuber variant is cosplay, not engineering.
- **Don't build a web dashboard.** Slack + Sheets + Obsidian is the UI. A
  dashboard is 2 weeks of work for zero unique leverage.
- **Don't poll anything.** If it's not webhook-triggerable or cron-sensible,
  it doesn't belong here.
- **Don't let tier-2 files grow.** `MEMORY.md` creeping past ~200 lines is a
  signal to move things into FTS5.
- **Don't add a tool without updating the Dockerfile sed injection.**
  (See CLAUDE.md §Dockerfile gotchas.)

## Open questions

- **Obsidian sync mechanism** — Obsidian Sync ($4/mo, trivial) vs Syncthing
  (free, fiddly) vs iCloud (free, unreliable on Android). For
  productisation: probably vault-on-/data with a view API.
- **LLM provider** — MiniMax is cheap and working. No pressure to migrate,
  but re-evaluate if routing quality degrades as tool count grows past 30.
- **Slack free tier** — 90-day message retention. Fine for us since
  everything of value is already indexed elsewhere. Revisit if team use.
- **Daily-note template location** — in `hermes-config/` so the container
  can offer a `new_day` tool that scaffolds today's note, or local-only?

## Milestones that mean "stop and re-evaluate"

- Tool count > 30 → route scoping becomes mandatory, not optional
- A second person using it daily → storage adapter becomes urgent
- Sheet row count > 50k → consider Postgres migration for the transactions
  tab (MerchantMap + Budget can stay in Sheets)
- Monthly Render cost > $30 → you've over-built; revisit the principles
