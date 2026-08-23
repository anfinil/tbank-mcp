# Privacy Policy — T-Bank MCP

_Last updated: 2026-08-24_

T-Bank MCP is an **unofficial**, open-source MCP server and Claude Code plugin for
T-Bank (Т-Банк) mobile banking. It is not affiliated with, endorsed by, or
supported by T-Bank. All code is public: https://github.com/icyberdeveloper/tbank-mcp

## The short version

The developer collects **nothing**. There is no telemetry, no analytics, no
developer-operated server. The software runs entirely on your machine and talks
only to T-Bank's own API hosts, over TLS pinned to certificates shipped in the
package.

## What data goes where

- **Your credentials.** The account password and PIN are entered in a local
  terminal CLI (`tbank-mcp-login`) and sent directly to T-Bank's authentication
  API. They are never written to disk, never logged, and never enter the language
  model's context.
- **Session tokens** are stored locally in `~/.local/share/tbank-mcp/session.json`
  (file mode 0600, owner-only) and sent only to T-Bank API hosts.
- **Banking data** (accounts, operations, documents, carts, tickets) is fetched
  from T-Bank's API at your request and returned to the MCP client you run
  (Claude Code, Cursor, etc.). What that client and its language model provider do
  with conversation content is governed by **their** privacy policies, not this one.
- **Local logs.** A call trace and event journal are written locally
  (`~/.local/share/tbank-mcp/`) for debugging; sensitive tool arguments are
  redacted or stored as length only. `TBANK_TRACE=0` disables the trace. Nothing
  is uploaded anywhere.

## Network endpoints

The only network traffic is to T-Bank's production API hosts (`*.tbank.ru`,
`*.t-bank-app.ru`). There are no third-party services, CDNs, or update checks.

## Money movement

Payment tools require explicit per-payment confirmation through MCP elicitation —
a button showing the exact amount that only you can press. Clients that cannot
render that confirmation are refused before anything is sent.

## Licensing

MIT License — see [LICENSE](LICENSE). T-Bank trademarks belong to their owners.

## Contact

Questions and issues: https://github.com/icyberdeveloper/tbank-mcp/issues
