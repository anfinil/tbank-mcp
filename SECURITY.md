# Security Policy

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting**: the *Report a vulnerability*
button on the [Security tab](https://github.com/icyberdeveloper/tbank-mcp/security)
of this repository. Please do not open a public issue for anything exploitable.
Non-sensitive hardening suggestions are welcome as regular
[issues](https://github.com/icyberdeveloper/tbank-mcp/issues).

There is no bug bounty; reports are handled on a best-effort basis, and fixes
ship as a new PyPI release.

## What counts as a vulnerability here

This project moves real money on a real bank account, so the bar is concrete.
Report anything that breaks one of these guarantees:

- **No payment without a human press.** Every money-moving tool must end in an
  MCP elicitation showing the exact amount; a path that debits an account
  without that confirmation — or with a misleading amount — is critical.
- **Credentials stay out of the model.** The password and PIN must never enter
  the LLM context, tool arguments, logs, or files other than the session store.
- **Pinned TLS.** Certificate verification must rely only on the system store
  plus the SHA-256-pinned roots shipped in the package; anything that makes the
  client trust material learned from the network is critical.
- **Local data stays local.** Session tokens (`session.json`, mode 0600) and
  redacted local traces must not leak into the repo, fixtures, or any network
  destination other than T-Bank's own API hosts.
- **No personal data in the repo.** Fixtures and docs are scrubbed
  (`tests/fixtures/audit_captures.py` enforces this); a real account, card,
  order or identity value that survives scrubbing is a valid report.

The full threat model lives in the README (Security section) and
[PRIVACY.md](PRIVACY.md).

## Supported versions

Only the latest release on [PyPI](https://pypi.org/project/tbank-mcp/) is
supported; there are no backports.
