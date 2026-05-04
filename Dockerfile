FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    git \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Run the container in Singapore time so crontab expressions ("0 21 * * *")
# resolve to SGT wall-clock instead of UTC. Render containers default to UTC,
# which means "9 PM daily" fires at 5 AM SGT without this line.
ENV TZ=Asia/Singapore

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Pinned hermes-agent commit. Bump this value to pull upstream updates.
# Changing the ARG invalidates the clone layer so Docker re-fetches.
# Before bumping, verify that upstream toolsets.py still contains the string
# '"send_message",' — the sed below depends on it. See CLAUDE.md
# "hermes-agent SHA bump checklist".
ARG HERMES_AGENT_SHA=73d0b083510367adec42746e90c41ace16c0afb2
RUN git clone https://github.com/alhazjm/hermes-agent.git /app/hermes-agent \
    && cd /app/hermes-agent \
    && git checkout ${HERMES_AGENT_SHA}

WORKDIR /app/hermes-agent
RUN uv venv venv --python 3.11 && \
    . venv/bin/activate && \
    uv pip install -e ".[all]" && \
    uv pip install gspread google-auth

# --- Surgical COPY: only the files this repo owns land in the container ---
# New files added to this repo are silently absent on deploy unless they're
# explicitly named here. That's the point: the contract with upstream is
# narrow and named, so drift is loud, not silent.

COPY tools/sheets_client.py /app/hermes-agent/tools/sheets_client.py
COPY tools/expense_sheets_tool.py /app/hermes-agent/tools/expense_sheets_tool.py
COPY tools/card_optimiser.py /app/hermes-agent/tools/card_optimiser.py

# Register custom tool module so its @register_tool calls run at startup.
RUN printf '\nimport tools.expense_sheets_tool\n' >> /app/hermes-agent/model_tools.py

# Inject custom tool names into hermes-telegram toolset so the gateway
# always includes them. Anchored after `"send_message",` — if upstream
# renames or moves that line, the sed silently no-ops, so we follow it
# with a grep that turns the silent failure into a build error.
RUN sed -i '/"send_message",/a\    # Expense tracker (custom)\n    "log_expense", "log_expense_pending", "update_budget", "get_remaining_budget", "edit_expense", "delete_expense", "link_telegram_message", "get_transaction_by_message_id", "lookup_merchant_category", "learn_merchant_mapping", "detect_subscription_creep", "undo_last_expense", "generate_spending_report", "write_insight", "get_insights", "render_budget_chart", "append_journal_entry", "get_journal_entries", "sweep_missed_transactions", "get_card_cap_status", "recommend_card_for", "plan_month", "review_card_efficiency", "set_category_primary",' /app/hermes-agent/toolsets.py \
    && grep -q '"log_expense"' /app/hermes-agent/toolsets.py

# Patch hermes-agent run_agent.py to exit the agent loop cleanly when a tool
# result carries `"assistant_reply_required": false`. Our log_expense tool
# side-channels its own Telegram confirmation bubble, and MiniMax doesn't
# reliably honour prompt-level silence — this is the deterministic fix.
# v2 replaces the v1 zero-out, which still triggered the empty-response
# nudge/retry cascade. The patch script exits non-zero if the upstream
# anchor shifts (loud build failure). See CLAUDE.md "hermes-agent SHA
# bump checklist".
COPY deploy/patches/suppress_reply_on_silent_tools.py /app/patches/suppress_reply_on_silent_tools.py
RUN python3 /app/patches/suppress_reply_on_silent_tools.py \
    && grep -q "PEH patch v2: exit agent loop when tool signalled silence" /app/hermes-agent/run_agent.py

# Prune built-in skill catalogs — this bot only uses the skills installed
# via COPY skills/ below. Keeps /skills output focused and avoids leaking
# irrelevant categories (pixel-art, github, gaming, etc.) into the agent's
# skill discovery surface.
RUN find /app/hermes-agent/skills -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +

COPY skills/ /root/.hermes/skills/
COPY hermes-config/cli-config.yaml /root/.hermes/config.yaml
COPY deploy/start.sh /app/start.sh
COPY cron/setup-cron-jobs.sh /app/cron/setup-cron-jobs.sh
RUN chmod +x /app/start.sh /app/cron/setup-cron-jobs.sh

# NOTE: USER.md / MEMORY.md / SOUL.md are NOT copied here. The deployed
# instance ships its own private versions; this repo only carries the
# `.example` templates under hermes-config/. Forks should add their own
# COPY lines here for their tier-2 memory files.

ENV HERMES_HOME=/root/.hermes
ENV PATH="/app/hermes-agent/venv/bin:$PATH"

EXPOSE 8644

CMD ["/app/start.sh"]
