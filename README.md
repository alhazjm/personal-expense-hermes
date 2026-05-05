# Personal Expense Hermes (PEH)

**A production LLM agent for personal finance — built as a study in treating AI systems like real software.**

Telegram bot + Gmail webhook → tool-calling agent → Google Sheets ledger. Runs on a $7/mo Render box, has been categorising my real expenses for months, and exists to learn the patterns that make LLM agents safe to put in production.

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

## Why this is interesting

Four production-rigor patterns I think AI engineering teams will care about. Each is a *forward-leaning* stance — not retrospective lessons, but claims about how to build agents that I'd advocate for on day one of an AI engineering role.

Each section is structured the same way: **the problem**, **what naive approaches do**, **what I landed on**, **the receipt**, and **what this would teach a team I joined**.

---

## 1. Debug the model, not the prompt

> When prompt-level instructions stop being reliable, that is a *signal*, not a stubborn LLM you need to coax.

### The problem

`log_expense` is a tool that side-channels its own confirmation message to Telegram before returning. The agent loop's job at that point is to do nothing — the user has already received their receipt. But MiniMax-M2.7 kept generating a duplicate "OK, I logged that for you" reply on top.

I tried the obvious things first: stronger silence instructions in the skill markdown, "do not respond, the tool has already replied" in the system prompt, examples in `MEMORY.md`. They worked maybe 70% of the time. The remaining 30% was bad UX (duplicate messages on real expenses) and unfixable through more prompting.

### What naive approaches do

Three common reactions when prompts stop being reliable:

1. **Keep prompt-engineering harder.** Diminishing returns. You can't unit-test "the model will obey this instruction 99.9% of the time"; eventually you accept reality or move on.
2. **Tolerate the bug.** Ship anyway, document as known issue. Acceptable if the surface is small. Not acceptable if it's the primary user interaction.
3. **Switch models.** Tempting and sometimes correct. But this *trades* one set of model-specific quirks for another, doesn't generalise, and creates re-evaluation cost every time you bump the model.

All three treat the model as the immutable centre of the system. None of them treat *model behaviour* as something you can route around in code.

### What I landed on

A deterministic patch to the upstream agent loop. When a tool result carries `assistant_reply_required: false`, the loop exits cleanly without sending a final assistant message. Tools that own their own egress (like `log_expense`) set that flag.

The interesting story is the **v1 → v2 iteration**:

- **v1** zeroed out `final_response` to an empty string. Logical, minimal change. But hermes-agent's empty-response handling triggered a *retry/nudge cascade* — the framework saw an empty response, assumed the model had glitched, and re-prompted it. That re-prompt sometimes succeeded and produced a duplicate. v1 *moved* the bug; it didn't fix it.
- **v2** short-circuits the loop entirely instead of muting the response. Skips the retry logic, skips the nudge, exits the agent turn. Solves the underlying state-machine issue rather than the surface symptom.

The patch script anchors on a specific upstream line (`final_response = assistant_message.content or ""` in the no-tool-calls branch of the loop). If upstream refactors that line, the anchor mismatch fails the Docker build with a clear pointer to the patch script — not a silent runtime bug at deploy time.

### The receipt

[`deploy/patches/suppress_reply_on_silent_tools.py`](deploy/patches/suppress_reply_on_silent_tools.py) and the [`CLAUDE.md`](CLAUDE.md) "hermes-agent SHA bump checklist" that documents which anchors to verify before bumping.

### What this would teach a team I joined

When prompt-level reliability plateaus on something user-facing, **escalate from prompt-engineering to system-engineering**. Concretely: define a structured signal a tool can return to control the agent loop's behaviour, and make the loop honour it deterministically. The model becomes one component you can route around, not the whole system you have to obey.

This generalises to: tool-call shape contracts, agent-loop guards, deterministic exits, and the broader principle that *prompts are a soft contract; code is a hard one* — and you should know which one you're using for any given guarantee.

---

## 2. Fail loud when upstream drifts

> A third-party agent framework is a dependency, not a fork target. Treat the boundary like a public API you don't control.

### The problem

PEH builds on top of `hermes-agent`, an open-source agent framework. Three options for taking that dependency:

1. **Fork it.** Get full control, accept slow rot as your fork diverges from upstream and merges become painful.
2. **Vendor it.** Copy-paste into your repo. Same problem as fork but worse — you also lose attribution clarity.
3. **Pin it and patch surgically.** Keep upstream pristine, add only the files you own, treat anchor points in upstream as a contract.

The third option is what most people *should* pick for any actively-maintained framework, but very few do — because it requires confidence in your anchors and discipline about what crosses the boundary.

### What naive approaches do

The fork-and-diverge anti-pattern is the dominant one in agent-framework deployments I've seen. It looks fine for two months, then you want a feature upstream shipped and your merge is 400 conflicts deep. The vendor anti-pattern is even worse — you've broken the contributor relationship entirely.

Both fail *quietly*. There's no Docker build error when your fork drifts; you just stop tracking upstream and your codebase ages without you noticing.

### What I landed on

The Dockerfile is a "narrow surface" pattern. It clones upstream at a pinned SHA (`HERMES_AGENT_SHA`), then COPYs *exactly eight specified paths* from this repo into the container — nothing else. New files in this repo are silently absent at deploy time unless they're explicitly named in the Dockerfile.

Two anchor-string contracts with upstream:

1. **Tool-list injection.** A `sed` one-liner in the Dockerfile inserts our 24 custom tool names into hermes-agent's `toolsets.py` after a specific line (`"send_message",`). If upstream renames or moves that line, the sed silently no-ops and our tools don't appear. To prevent this, the same Dockerfile step pipes into `grep -q '"log_expense"'` — turning silent failure into a build error.

2. **Agent-loop patch anchor.** The patch script (Pattern #1's receipt) targets a specific line in `run_agent.py`. If that line moves or changes shape, the script exits non-zero and the build fails with a pointer to the script. The Dockerfile also greps for the patch's stable marker after the patch runs, so an "applied but corrupted" outcome is also caught. There's a checklist in [`CLAUDE.md`](CLAUDE.md) that says: before bumping `HERMES_AGENT_SHA`, eyeball upstream for these anchors.

Both patterns share a principle: **failure modes that would otherwise be silent runtime bugs become loud build errors instead.** The cost of the build error is five minutes of investigation; the cost of the silent runtime bug is a user typing into a broken Telegram bot.

### The receipt

The [`Dockerfile`](Dockerfile) (the surgical COPY block + the sed/grep verification + the patch invocation + marker-grep) and [`CLAUDE.md`](CLAUDE.md)'s "hermes-agent SHA bump checklist". The pattern is also visible in [`tools/sheets_client.py`](tools/sheets_client.py) at a smaller scale — `_get_column_index(ws, header)` does header-name lookup so a new sheet column doesn't silently break a hard-coded index.

### What this would teach a team I joined

For any dependency you don't fully own — third-party agent framework, vendor SDK, an internal-but-other-team's library — define your contract with it as a *narrow, named surface*. List the paths you depend on. List the line-shapes you've patched against. Make any drift in those surfaces fail your build, not your runtime.

The general principle is: **the cost of a silent bug scales with the time-to-detect**. Push detection upstream of deploys.

---

## 3. Restraint-shaped roadmap

> Most engineering roadmaps are growth-shaped. Restraint with reasoning is a principal-level signal — and it's a *contract* with future-you that prevents over-building.

### The problem

Personal projects rot in two ways. Either they stop being maintained (the obvious failure mode), or they over-grow into a codebase you can't reason about anymore (the subtler one). For an LLM-agent project specifically, over-growth is the more dangerous failure — every new tool dilutes the LLM's routing accuracy, every new memory file inflates context, every new skill multiplies the surface area you have to test.

Without an explicit anti-growth force, the gradient is always toward "add another thing." Especially when AI lets you ship fast.

### What naive approaches do

The default roadmap is a feature list with rough dates. It tells you what to build but not what to *not* build. It doesn't surface the architectural decisions you're locking in by building thing X before thing Y. And it has no exit ramp — no point at which you stop adding things and re-evaluate the foundation.

The result: codebases that drift toward "everything we could imagine wanting" instead of "the smallest thing that does the job."

### What I landed on

`ROADMAP.md` has four properties uncommon enough to be worth naming:

1. **A "Things NOT to do" section with reasoning.** Every entry is a temptation I had to actively resist. Examples:
   - "Don't chase local LLM (Qwen on Pi). Cloud inference cost is not your bottleneck. The enthusiast-YouTuber variant is cosplay, not engineering."
   - "Don't build a web dashboard. Slack + Sheets + Obsidian is the UI. A dashboard is 2 weeks of work for zero unique leverage."

2. **Each near-term step annotated with the architectural assumption it validates.** Step #2 (the card-optimiser skill) is annotated with: *"Validates: multi-skill routing in `cli-config.yaml`, shared MerchantMap across skills, per-skill toolset scoping."* If that step ships and the assumption holds, step #4 (FTS5 cross-skill recall) becomes safer to attempt. If it fails, the recall step is on hold. Hypothesis-driven, not feature-driven.

3. **"Stop and re-evaluate" milestones.** Tool count > 30 means routing scope must become mandatory, not optional. A second daily user means the storage adapter becomes urgent. Sheet rows > 50k means consider Postgres for transactions only. Monthly Render cost > $30 means I've over-built. Each is a number that, when crossed, *forces* a re-architecture conversation rather than letting drift continue.

4. **An "insurance-today" section that says "Nothing structural."** Explicitly resisting writing the storage adapter before the second skill exists, adding tenant_id columns before a second tenant exists, building auth middleware before users exist. The architecture is "already close enough." This is the YAGNI principle made concrete.

### The receipt

[`ROADMAP.md`](ROADMAP.md). The structure itself is the artifact.

### What this would teach a team I joined

Two practices to advocate for:

1. **"Things we are deliberately not doing" should be in every roadmap doc**, not just implicit in absence. Naming what you've ruled out, with reasoning, is what separates discipline from drift.

2. **Tie each near-term step to the architectural assumption it validates.** Make the roadmap a sequence of experiments, not a sequence of features. If an assumption is wrong, you find out before you've built three more things on top of it.

The general principle: *good engineering is mostly about what you don't build.* A roadmap that doesn't surface the things-not-built is missing half the decisions.

---

## 4. Memory architecture with stated rules

> Prompt files behave like databases. They need a schema, capacity limits, and explicit rules for what doesn't go in them.

### The problem

Once you have an agent with persistent identity, you accumulate prompt material fast. Who is the user? What are their preferences? What's the system schema? What's the agent's tone? What was the conversation last week?

The naive answer is "put it all in the system prompt." That works for the first 200 lines. By line 800 you've blown your context budget, the model's routing accuracy has degraded, and you can't tell which of the 17 USER-PREFERENCE bullets is responsible for the latest weird behaviour.

### What naive approaches do

Two failure modes:

1. **Append-only memory file.** Every new fact gets stuffed into one `memory.md`. Six months in, it's 3000 lines, half of it stale, and the model is paying attention-tax on every irrelevant bullet every turn.

2. **No tier separation.** Identity, system facts, persona, conversation history all share the same prompt budget. When you have to cut something, you have no principled basis for what to cut.

Both treat memory as an unstructured pile rather than a structured cache.

### What I landed on

A two-tier explicit split — there's also a tier-3 (FTS5 episodic) but PEH only validates the first two:

**Tier-2: in the system prompt every turn.** Three named files, each with a stated purpose:

- **`USER.md`** — identity and preferences. People in my life, payment methods, communication style, tool-use rules. Things that don't change week to week.
- **`MEMORY.md`** — system/domain facts. Sheet schema, webhook payload shape, enum values, edge-case quirks. *Machine-ish* — the agent looks here for "how does this system work" answers.
- **`SOUL.md`** — agent persona. Voice, principles, failure modes. Capped around ~50 lines.

**Tier-3: queried on demand.** FTS5-indexed historical sheet rows + Obsidian daily notes (planned). The agent calls a `recall(query)` tool when it needs episodic context. Not in the system prompt.

Two disciplines that make this work:

1. **"What does NOT belong here" sections.** Each tier-2 file has an explicit list of things that don't go in it. `MEMORY.md`'s says: no transaction data, no derived insights, no session history, no budget numbers, no category lists. Those live in the sheet (the actual database) or get indexed into tier-3. Without these negative constraints, tier-2 bloats by default.

2. **Skill versioning.** Each skill markdown file has a version in the frontmatter — `expense-tracker v4.11.0`, `budget-manager v3.0.0`. Bumped on breaking changes to tool contracts or flows. Almost nobody versions prompts — but they should, because prompt changes are *behaviour changes* with the same backwards-compatibility implications as code.

### The receipt

[`hermes-config/USER.md.example`](hermes-config/USER.md.example), [`hermes-config/MEMORY.md.example`](hermes-config/MEMORY.md.example), [`hermes-config/SOUL.md.example`](hermes-config/SOUL.md.example) — sanitised templates showing the structure and the "what does NOT belong here" rules. The [`skills/`](skills/) directory shows the versioning convention (`expense-tracker` v4.11.0, `card-optimiser` v1.0.0, `budget-manager` v3.0.0, `weekly-summary` v3.0.0).

### What this would teach a team I joined

Three practices:

1. **Tier your prompt material explicitly.** What goes in every turn? What gets queried on demand? The boundary should be deliberate, not accidental.

2. **Write negative constraints into your memory files.** *"What does NOT belong here"* is a more useful section than *"What belongs here"* because the failure mode is over-inclusion, not under-inclusion.

3. **Version your skill prompts like code.** A skill is a contract; behaviour changes break the contract. Treat them with the same release discipline you'd use for an API.

The general principle: **prompt files are state, not text.** Apply the rigor you'd apply to any other piece of mutable system state — schemas, capacity limits, versioning, negative constraints.

---

## What ties these four together

If I had to compress the whole document into one claim, it would be this:

> **LLM agents are software systems. Build them with the same rigor — and invent the missing rigor where it doesn't exist yet.**

Pattern 1 (debug the model) is rigor about *behaviour boundaries*: when do you stop trusting prompts and start writing code?
Pattern 2 (fail loud on drift) is rigor about *dependency boundaries*: how do you depend on something you don't own?
Pattern 3 (restraint-shaped roadmap) is rigor about *scope boundaries*: what do you decide not to build, and why?
Pattern 4 (memory architecture) is rigor about *state boundaries*: how do you structure prompt material so it doesn't rot into one undifferentiated pile?

Each one is a place where existing software-engineering rigor doesn't quite map onto LLM agents — and where I had to write down the pattern that worked. The patterns themselves are the artifact. PEH is the working sample.

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

## What's next

See [ROADMAP.md](ROADMAP.md). Briefly: Obsidian daily notes as a join key across domains, then a third skill (journal/voice notes), then an FTS5 cross-skill recall layer. Storage adapter and multi-tenancy are deliberately deferred until they're needed.

### Future-state ideas (prototyped but not in this repo)

- **Travel mode.** When a non-SGD transaction arrives during a trip, route it through a `TravelMode` tab (date range + per-bucket allocations: food / transport / lodging / activities / misc) instead of the regular Budget. The bucket tag goes into Notes as `[bucket:X]`; per-bucket overspend triggers an automatic Telegram nudge at 80% / 100%, deduped per (trip, bucket, threshold). Validates the same event-driven nudge contract as `card-optimiser` against a different domain. Lives in the deployed instance; not ported here to keep the public surface narrow at 24 tools.

---

## What's *not* in this document

- **No comparative benchmarking against other agent frameworks.** I haven't run AutoGen vs LangGraph vs CrewAI head-to-head. The patterns above don't depend on the framework choice; they'd apply to any of them.
- **No claim about model accuracy or evaluation methodology.** PEH has unit tests for tool handlers but no eval harness for end-to-end agent quality. That's a real gap and an interesting next project — not something to overclaim here.
- **No multi-tenancy story.** PEH is single-tenant by design (see Pattern 3). Claims about how it would scale are speculation until a second user exists.

These omissions are deliberate. The four patterns above are the strongest claims I can defend with the code that exists today. Adding speculative ones would dilute the signal.
