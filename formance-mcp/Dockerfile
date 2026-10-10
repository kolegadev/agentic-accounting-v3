FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

ENV FORNANCE_LEDGER_URL=http://host.docker.internal:3068 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0

# MCP streamable-http endpoint: http://<host>:8000/mcp
EXPOSE 8000

CMD ["formance-mcp"]
