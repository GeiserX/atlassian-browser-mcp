# How it works

Authentication and serving are **two separate processes**. This is what keeps the MCP server from hanging:

1. **Authenticate with the CLI** (foreground, where a browser can open): `atlassian-cli login <jira|confluence>` runs Playwright, you complete SSO/MFA once, and cookies are saved to a per-service storage-state file.
2. **The MCP server serves data only.** It reads the saved cookies and never opens a browser. When the saved cookies are missing or expired it fails fast with an `AuthRequiredError` telling you to run the CLI login, and on macOS it starts, in a background thread, a search of your installed Chromium-family browsers for a live session (bounded, no window, at most once a minute). If that search finds one, the next call works. It does **not** block waiting for a login or a Keychain prompt, so the asyncio event loop that serves every MCP request never stalls on authentication.
3. **Writes are never sent twice.** After an expired session, only GET, HEAD and OPTIONS requests are retried automatically. A POST, PUT or DELETE gets an error asking you to run it again, because the first attempt may already have changed data.

Earlier versions launched the login browser from inside the server. Because the server is detached and async, that blocked tool calls for minutes (often forever) and could deadlock Playwright's sync API on the event loop. The CLI/server split (`allow_interactive=False` on server sessions) removes that failure mode.

The server patches the `JiraClient` and `ConfluenceClient` constructors in `mcp-atlassian`: the patched constructor runs upstream's own constructor (in its credential-free "external" mode) and then installs the browser cookies on the session upstream built. Everything upstream applies to that session stays in force: the SSRF redirect check, DNS-rebinding pinning, retries, concurrency and rate limits, the circuit breaker, proxy, TLS and custom-header settings. Tests compare the patched session with one built by upstream alone, so a new upstream hardening step that the patch would drop fails CI.

Cookie jars hold only the cookies a browser would send to that service's host (the host itself and its parent domains), and are written 0600 from the moment the file exists. Jars written by older versions, which held every cookie of the browser profile, are pruned the next time they are loaded.

## Files

| File | Purpose |
|------|---------|
| `atlassian_browser_mcp_full.py` | MCP entrypoint. Patches upstream clients, completes tool annotations, registers `atlassian_auth_status` and `atlassian_login`, runs the MCP server on stdio |
| `atlassian_browser_auth.py` | Shared auth core: `CookieAuth` (cookie loading and re-auth on any session), `BrowserCookieSession`, `interactive_login()`, profile seeding, SSO detection, background harvest |
| `cookie_autoauth.py`, `cookie_harvest.py` | Harvest a live Jira/Confluence session from installed browsers without opening a window |
| `atlassian_cli.py` + `atlassian-cli` | Command-line front-end over the same auth core (Jira/Confluence get/search, login) |
| `run-atlassian-browser-mcp.sh` | MCP launcher: installs the locked dependencies with `uv sync --frozen` when missing, starts the server |
| `preflight.sh` | Bounded GREEN/RED check of the saved sessions from a shell, without the MCP server |
| `pyproject.toml`, `uv.lock` | Dependency ranges and the exact locked versions |
