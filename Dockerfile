# Build for MCP catalogs (Glama) and anyone who wants the server containerized.
# Builds from the repo source so the image always matches the indexed commit.
# The grocery-checkout browser (playwright chromium, ~150 MB) is deliberately
# NOT installed — only grocery_checkout needs it; every other tool works without.
FROM python:3.13-slim

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir /app

# stdio MCP server; login happens out-of-band (tbank-mcp-login) — the session
# file location is governed by TBANK_SESSION if the container needs a mount.
ENTRYPOINT ["tbank-mcp"]
