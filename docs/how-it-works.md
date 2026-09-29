# How it works

Authentication and serving are **two separate processes**. This is what keeps the MCP server from hanging:

1. **Authenticate with the CLI** (foreground, where a browser can open): `atlassian-cli login <jira|confluence>` runs Playwright, you complete SSO/MFA once, and cookies are saved to a per-service storage-state file.
2. **The MCP server serves data only.** It reads the saved cookies through a custom `requests.Session` subclass and never opens a browser. When the saved cookies are missing or expired it first tries, on macOS, to harvest a live session from your installed Chromium-family browsers (bounded, no window); if that finds nothing it fails fast with an `AuthRequiredError` telling you to run the CLI login. It does **not** block waiting for an interactive login.

Earlier versions launched the login browser from inside the server. Because the server is detached and async, that blocked tool calls for minutes (often forever) and could deadlock Playwright's sync API on the event loop. The CLI/server split (`allow_interactive=False` on server sessions) removes that failure mode.

The server monkey-patches the `JiraClient` and `ConfluenceClient` constructors in `mcp-atlassian` to inject the browser-cookie session, giving full parity with the upstream tool surface.

## Files

| File | Purpose |
|------|---------|
| `atlassian_browser_mcp_full.py` | MCP entrypoint. Patches upstream clients, registers the `atlassian_login` tool, runs the MCP server |
| `atlassian_browser_auth.py` | Shared auth core: `BrowserCookieSession`, `interactive_login()`, profile seeding, SSO detection |
| `cookie_autoauth.py`, `cookie_harvest.py` | Harvest a live Jira/Confluence session from installed browsers without opening a window |
| `atlassian_cli.py` + `atlassian-cli` | Command-line front-end over the same auth core (Jira/Confluence get/search, login) |
| `run-atlassian-browser-mcp.sh` | MCP launcher: creates the venv, installs deps with `uv`, runs the compatibility check, starts the server |
| `pyproject.toml` | Dependency pins |
