# atlassian-browser-mcp

[![License: GPL-3.0](https://img.shields.io/github/license/GeiserX/atlassian-browser-mcp?style=flat-square)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3572A5?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![GitHub stars](https://img.shields.io/github/stars/GeiserX/atlassian-browser-mcp?style=flat-square)](https://github.com/GeiserX/atlassian-browser-mcp/stargazers)
[![mcp-atlassian](https://img.shields.io/badge/wraps-mcp--atlassian%200.21.x-blue?style=flat-square)](https://github.com/sooperset/mcp-atlassian)

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
| `atlassian_browser_mcp_full.py` | Entrypoint. Patches upstream clients, registers `atlassian_login` tool, runs the MCP server |
| `atlassian_browser_auth.py` | Shared auth: `BrowserCookieSession`, `interactive_login()`, SSO detection |
| `run-atlassian-browser-mcp.sh` | Launcher: creates venv, installs deps via `uv`, runs compatibility check, starts server |
| `pyproject.toml` | Dependency pins |

## Usage

```bash
./run-atlassian-browser-mcp.sh
```

Or configure as an MCP server in your editor (Cursor, Claude Code, etc.) pointing to the launcher script.

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JIRA_URL` | _(required)_ | Jira base URL (e.g. `https://jira.example.com`) |
| `CONFLUENCE_URL` | _(required)_ | Confluence base URL (e.g. `https://confluence.example.com`) |
| `ATLASSIAN_BROWSER_AUTH_ENABLED` | `true` | Enable browser auth (set `false` to fall back to token auth) |
| `ATLASSIAN_BROWSER_PROFILE_DIR` | `./.atlassian-browser-profile` | Persistent Chromium profile directory |
| `ATLASSIAN_STORAGE_STATE` | `./.atlassian-browser-state.json` | Playwright storage-state file |
| `ATLASSIAN_LOGIN_TIMEOUT_SECONDS` | `300` | Seconds to wait for manual login |
| `ATLASSIAN_USERNAME` | _(none)_ | Optional: prefill username on SSO page |
| `ATLASSIAN_SSO_MARKERS` | _(auto)_ | Comma-separated URL/text markers for SSO redirect detection. Defaults cover Okta, ADFS, Azure AD, PingOne, Google SAML |
| `TOOLSETS` | `all` | Which upstream toolsets to enable |

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (for dependency management)
- Chromium (installed automatically by Playwright)
- Network access to your Atlassian instance
