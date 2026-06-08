<p align="center">
  <img src="docs/images/banner.svg" alt="atlassian-browser-mcp banner" width="900"/>
</p>

# atlassian-browser-mcp

[![License: GPL-3.0](https://img.shields.io/github/license/GeiserX/atlassian-browser-mcp?style=flat-square)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3572A5?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![GitHub stars](https://img.shields.io/github/stars/GeiserX/atlassian-browser-mcp?style=flat-square)](https://github.com/GeiserX/atlassian-browser-mcp/stargazers)
[![mcp-atlassian](https://img.shields.io/badge/wraps-mcp--atlassian%200.x-blue?style=flat-square)](https://github.com/sooperset/mcp-atlassian)
[![GeiserX/atlassian-browser-mcp MCP server](https://glama.ai/mcp/servers/GeiserX/atlassian-browser-mcp/badges/score.svg)](https://glama.ai/mcp/servers/GeiserX/atlassian-browser-mcp)

MCP server that wraps the upstream [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) toolset with browser-cookie authentication via Playwright. Designed for Atlassian Server/Data Center instances behind corporate SSO (Okta, SAML, etc.) where API tokens are not available.

## How it works

1. On first use (or when the session expires), Playwright opens a real Chromium window for manual SSO/MFA
2. After login, cookies are saved to a Playwright storage-state file
3. All subsequent MCP tool calls use those cookies via a custom `requests.Session` subclass
4. If an API response looks like an SSO redirect, the browser reopens automatically

The server monkey-patches `JiraClient` and `ConfluenceClient` constructors in `mcp-atlassian` to inject the browser-backed session, giving full parity with the upstream tool surface (72 tools + 1 `atlassian_login` helper = 73 total).

## Files

| File | Purpose |
|------|---------|
| `atlassian_browser_mcp_full.py` | MCP entrypoint. Patches upstream clients, registers `atlassian_login` tool, runs the MCP server |
| `atlassian_browser_auth.py` | Shared auth core: `BrowserCookieSession`, `interactive_login()`, profile seeding, SSO detection |
| `atlassian_cli.py` + `atlassian-cli` | Command-line front-end over the same auth core (Jira/Confluence get/search, login). Great for scripts and agents — see [`AGENT_USAGE.md`](AGENT_USAGE.md) |
| `run-atlassian-browser-mcp.sh` | MCP launcher: creates venv, installs deps via `uv`, runs compatibility check, starts server |
| `pyproject.toml` | Dependency pins |

## Reusing your real browser session (recommended)

To avoid re-entering your username/password + MFA on every login, **seed the
automation profile once from your real Chrome profile**. The copy carries your
existing SSO cookies (and saved logins / password-manager extension), so the
first login is typically one-click or fully hands-free:

```bash
ATLASSIAN_SEED_FROM_CHROME_PROFILE=Default ./atlassian-cli login jira
```

Chrome 136+ blocks automation from driving the live profile in place, so a
one-time copy into the dedicated profile dir is the supported way to inherit the
session. The profile is **never auto-deleted** on an auth failure, so the
long-lived session persists and re-login stays instant. Jira and Confluence keep
separate cookie jars but share one seeded profile.

## CLI usage

```bash
export JIRA_URL="https://jira.example.com"
export CONFLUENCE_URL="https://confluence.example.com"

./atlassian-cli login jira                       # one-time per service
./atlassian-cli jira get PROJ-123 --comments
./atlassian-cli jira search 'project = PROJ AND status = "In Progress"'
./atlassian-cli confluence get 123456789 --markdown -o page.md
./atlassian-cli confluence search 'release process' --space DEV
```

The CLI defaults to the real `chrome` channel (its seeded cookies are encrypted
with a keychain key only Chrome can read); the MCP server defaults to `chromium`.

## Usage

```bash
./run-atlassian-browser-mcp.sh
```

### MCP server configuration

Add to your Claude Code, Cursor, or other MCP client configuration:

```json
{
  "mcpServers": {
    "atlassian": {
      "command": "/path/to/atlassian-browser-mcp/run-atlassian-browser-mcp.sh",
      "env": {
        "JIRA_URL": "https://jira.example.com",
        "CONFLUENCE_URL": "https://confluence.example.com",
        "ATLASSIAN_USERNAME": "your.email@company.com"
      }
    }
  }
}
```

On first use (or when cookies expire), a Chromium window opens for SSO login. After login completes, the browser closes automatically and all MCP tool calls proceed using the saved session.

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JIRA_URL` | _(required)_ | Jira base URL (e.g. `https://jira.example.com`) |
| `CONFLUENCE_URL` | _(required)_ | Confluence base URL (e.g. `https://confluence.example.com`) |
| `ATLASSIAN_BROWSER_AUTH_ENABLED` | `true` | Enable browser auth (set `false` to fall back to token auth) |
| `ATLASSIAN_BROWSER_PROFILE_DIR` | `./.atlassian-browser-profile` | Persistent browser profile directory (shared across services) |
| `ATLASSIAN_SEED_FROM_CHROME_PROFILE` | _(none)_ | Seed the profile once from a real Chrome profile (name like `Default`/`Profile 1`, or an absolute path). Brings your cookies, saved logins, and existing SSO session |
| `ATLASSIAN_CHROME_USER_DATA_DIR` | _(macOS Chrome dir)_ | Where Chrome profiles live, for resolving the seed profile name |
| `ATLASSIAN_STORAGE_STATE` | `./.atlassian-browser-state-{service}.json` | Cookie-jar file. Per-service by default; an explicit value is still namespaced per service |
| `ATLASSIAN_LOGIN_TIMEOUT_SECONDS` | `300` | Seconds to wait for manual login |
| `ATLASSIAN_USERNAME` | _(none)_ | Optional: prefill username on SSO page |
| `ATLASSIAN_SSO_MARKERS` | _(auto)_ | Comma-separated URL/text markers for SSO redirect detection. Defaults cover Okta, ADFS, Azure AD, PingOne, Google SAML |
| `ATLASSIAN_BROWSER_CHANNEL` | `chromium` | Browser channel (`chromium`, `chrome`, `msedge`) |
| `ATLASSIAN_JIRA_LOGIN_URL` | `{JIRA_URL}/secure/Dashboard.jspa` | Override the Jira login entry point URL |
| `ATLASSIAN_CONFLUENCE_LOGIN_URL` | `{CONFLUENCE_URL}` | Override the Confluence login entry point URL |
| `ATLASSIAN_BROWSER_USER_AGENT` | _(Chrome 136)_ | Custom User-Agent string for API requests |
| `TOOLSETS` | `all` | Which upstream toolsets to enable |

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (for dependency management)
- Chromium (installed automatically by Playwright)
- A graphical display (macOS, X11, or Wayland) — required for interactive SSO login
- Network access to your Atlassian instance

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Browser doesn't open | Headless environment (SSH, Docker) | Forward X11 or run initial login on a machine with a display |
| Login timed out | Didn't land on Jira/Confluence URL within 300s | Check `JIRA_URL`/`CONFLUENCE_URL` match exactly where your IdP redirects after login. Increase `ATLASSIAN_LOGIN_TIMEOUT_SECONDS` if needed |
| Tools return HTML instead of JSON | Session expired, SSO markers not matching your IdP | Set `ATLASSIAN_SSO_MARKERS` with your IdP's URL pattern |
| "Upstream compatibility check failed" | `mcp-atlassian` version changed its internal API | Pin to a compatible version or update the wrapper |
| "Executable doesn't exist" | Playwright Chromium not installed | Run `python -m playwright install chromium` |


