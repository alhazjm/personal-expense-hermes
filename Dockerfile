FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

RUN git clone https://github.com/alhazjm/hermes-agent.git /app/hermes-agent

WORKDIR /app/hermes-agent
RUN uv venv venv --python 3.11 && \
    . venv/bin/activate && \
    uv pip install -e ".[all]" && \
    uv pip install gspread google-auth

COPY tools/sheets_client.py /app/hermes-agent/tools/sheets_client.py
COPY tools/expense_sheets_tool.py /app/hermes-agent/tools/expense_sheets_tool.py

RUN echo "import tools.expense_sheets_tool" >> /app/hermes-agent/model_tools.py

COPY skills/ /root/.hermes/skills/
COPY hermes-config/cli-config.yaml /root/.hermes/config.yaml
COPY deploy/start.sh /app/start.sh
RUN chmod +x /app/start.sh

ENV HERMES_HOME=/root/.hermes
ENV PATH="/app/hermes-agent/venv/bin:$PATH"

EXPOSE 8644

CMD ["/app/start.sh"]
