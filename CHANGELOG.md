# Changelog

## 1.4.1 - 2026-10-27

### Added

- Published on PyPI as `atlassian-browser-mcp`: `uvx atlassian-browser-mcp` runs the server and `uvx --from atlassian-browser-mcp atlassian-cli login jira` saves a session, so no checkout is needed (#33).
- Listed in the official MCP Registry as `io.github.GeiserX/atlassian-browser-mcp` (`server.json`) (#33).
- Release workflow: a `v*` tag builds the package, publishes it to PyPI through a trusted publisher (OIDC, no stored token) and then publishes `server.json` to the registry (#33).

## 1.4.0 - 2026-10-27

### Security

- The patched Jira and Confluence clients now run upstream mcp-atlassian's own constructor and add the browser cookies afterwards, so upstream's SSRF redirect check, DNS pinning, retries, rate limits, proxy and TLS settings stay in force on browser-cookie sessions (#31).
- Cookie jars keep only the cookies of their own service host and are written with mode 0600 (#31).
- After an expired session only GET, HEAD and OPTIONS requests are sent again; a write gets an `AuthRequiredError` instead, so it never runs twice (#31).
- The server refuses to start on any transport other than stdio (#31).
- Requires `cryptography>=44.0.1`, which carries the fixes for CVE-2024-26130 and CVE-2024-12797 (#31).

### Added

- `atlassian_auth_status` tool: reports which service has a live session, so an agent can ask for a login before it fails mid-task (#31).
- Every tool carries a title and all four MCP tool hints (#31).
- Jira-only and Confluence-only setups work (#31).
- Client setup instructions for Claude Code, Claude Desktop, Cursor, VS Code and Codex in the README (#31).
- Exact dependency versions locked in `uv.lock`; install with `uv sync --frozen` (#31).
- CodeQL analysis workflow (#31).

### Changed

- Seeding the automation profile from your real Chrome profile is opt-in (`ATLASSIAN_SEED_FROM_CHROME_PROFILE`) (#31).
- On macOS the background session reuse from installed Chromium-family browsers says on stderr that it reads the browser's Keychain key, and can be turned off (#31).
- Requires mcp-atlassian 0.23.1 or newer, so Confluence page operations keep working (#18, #21).
- License metadata reads GPL-3.0-or-later (#27).

### CI

- The test suite runs on every pull request on Python 3.11, 3.12 and 3.13 (#22).
- The upstream tracker issue reports Dependabot status correctly (#23), and the stale job keeps its state between runs (#26).
