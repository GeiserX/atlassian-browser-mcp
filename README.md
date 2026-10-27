<p align="center">
  <img src="docs/images/banner.svg" alt="atlassian-browser-mcp banner" width="900"/>
</p>

# atlassian-browser-mcp

[![CI](https://github.com/GeiserX/atlassian-browser-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/GeiserX/atlassian-browser-mcp/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/GeiserX/atlassian-browser-mcp?style=flat-square)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3572A5?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![mcp-atlassian](https://img.shields.io/badge/wraps-mcp--atlassian%200.x-blue?style=flat-square)](https://github.com/sooperset/mcp-atlassian)
[![GeiserX/atlassian-browser-mcp MCP server](https://glama.ai/mcp/servers/GeiserX/atlassian-browser-mcp/badges/score.svg)](https://glama.ai/mcp/servers/GeiserX/atlassian-browser-mcp)

MCP server that wraps the upstream [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) toolset with browser-cookie authentication via Playwright, for Atlassian Server/Data Center instances behind corporate SSO (Okta, SAML, etc.) where API tokens are not available. It runs locally over stdio: you log in once with the bundled CLI, and the server reuses the cookies.

## Features

- Every mcp-atlassian Jira and Confluence tool, authenticated with your SSO browser session instead of an API token, with upstream's own network hardening (SSRF redirect checks, DNS pinning, retries, rate limits, proxy and TLS settings) kept in force.
- One login per service with the CLI; the server itself never opens a browser, so it never hangs waiting for one.
- `atlassian_auth_status` tells an agent which service needs a login before it fails mid-task.
- Optional one-time seeding of the automation profile from your real Chrome profile (`ATLASSIAN_SEED_FROM_CHROME_PROFILE`), so the first login is often one click.
- On macOS, reuses a live session from an installed Chromium-family browser (Arc, Brave, Edge, Chrome and others) without opening a window. It reads that browser's Keychain key, says so on stderr, and can be turned off.
- Separate cookie jars for Jira and Confluence, holding only that host's cookies, mode 0600. Jira-only or Confluence-only setups work.
- A command-line front-end (`atlassian-cli`) for scripts and agents: get and search issues and pages.
- Falls back to token auth with `ATLASSIAN_BROWSER_AUTH_ENABLED=false`.

## Quick start

From PyPI, with [uv](https://docs.astral.sh/uv/) installed:

```bash
uvx --from atlassian-browser-mcp atlassian-cli login jira   # once per service, opens your browser
JIRA_URL=https://jira.example.com CONFLUENCE_URL=https://confluence.example.com uvx atlassian-browser-mcp
```

From a checkout, with the exact versions in `uv.lock`:

```bash
git clone https://github.com/GeiserX/atlassian-browser-mcp.git && cd atlassian-browser-mcp
UV_PROJECT_ENVIRONMENT=.venv-atlassian-browser uv sync --frozen   # installs the versions in uv.lock
JIRA_URL=https://jira.example.com CONFLUENCE_URL=https://confluence.example.com ./atlassian-cli login jira   # once per service
```

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/), Google Chrome and a display for the login. The server never opens a browser window. It reads the saved cookies; when they are missing or expired it returns an `AuthRequiredError` asking you to run the login again, and on macOS it also looks for a live session in your installed browsers in the background, so a retry a few seconds later may already work. Seeding from Chrome and every setting are in [Getting started](docs/getting-started.md).

## Add it to your client

Every client runs the server over stdio. Set only the URL of a service you use. The examples use the PyPI package through `uvx`; from a checkout, replace `uvx atlassian-browser-mcp` with `/path/to/atlassian-browser-mcp/run-atlassian-browser-mcp.sh`.

**Claude Code**

```bash
claude mcp add --transport stdio atlassian \
  --env JIRA_URL=https://jira.example.com \
  --env CONFLUENCE_URL=https://confluence.example.com \
  -- uvx atlassian-browser-mcp
```

**Claude Desktop** (`claude_desktop_config.json`) and **Cursor** (`~/.cursor/mcp.json` or `.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "atlassian": {
      "command": "uvx",
      "args": ["atlassian-browser-mcp"],
      "env": {
        "JIRA_URL": "https://jira.example.com",
        "CONFLUENCE_URL": "https://confluence.example.com"
      }
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`)

```json
{
  "servers": {
    "atlassian": {
      "type": "stdio",
      "command": "uvx",
      "args": ["atlassian-browser-mcp"],
      "env": {
        "JIRA_URL": "https://jira.example.com",
        "CONFLUENCE_URL": "https://confluence.example.com"
      }
    }
  }
}
```

**Codex** (`~/.codex/config.toml`)

```toml
[mcp_servers.atlassian]
command = "uvx"
args = ["atlassian-browser-mcp"]
env = { JIRA_URL = "https://jira.example.com", CONFLUENCE_URL = "https://confluence.example.com" }
```

**Protocol.** Speaks MCP 2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05 over stdio, which covers clients that open with an `initialize` handshake (Claude Code, Claude Desktop, Cursor and VS Code today). MCP 2026-07-28 is not supported yet: upstream mcp-atlassian pins `mcp<2` and `fastmcp<4` (https://github.com/sooperset/mcp-atlassian/issues/1541), and this server follows as soon as upstream moves. It runs on stdio only and refuses to start with any other `FASTMCP_TRANSPORT`.

## Read-only mode

Write tools act as you, with your SSO identity. To hide all of them (create, update, delete, transition, upload, restrictions), set `READ_ONLY_MODE=true` in the server's `env`. Upstream enforces it when listing and when calling. `ENABLED_TOOLS` (a comma-separated allow-list of tool names) and `TOOLSETS` narrow the list further; see [Configuration](docs/configuration.md).

## Tools

| Tool | What it does | Hints |
|------|--------------|-------|
| `atlassian_auth_status` | Whether each service's saved session exists and works: jar age, cookie expiry, a live check, the next step | read-only |
| `atlassian_login` | The exact `atlassian-cli login <service>` command to show the user | read-only |
| `jira_*` (63) | Every upstream Jira tool: issues, search, comments, transitions, sprints, boards, versions, links, worklogs, attachments, service desk, forms | 38 read-only, 25 write |
| `confluence_*` (35) | Every upstream Confluence tool: pages, search, comments, labels, attachments, history, templates, restrictions | 20 read-only, 15 write |

Every tool carries a title and all four annotation hints (read-only, destructive, idempotent, open-world), so clients can auto-approve reads and confirm writes. The full list and the CLI are in [Usage](docs/usage.md).

## Documentation

- [Getting started](docs/getting-started.md): requirements, install, the first login, seeding from Chrome, the MCP client config
- [Configuration](docs/configuration.md): every environment variable and its default
- [Usage](docs/usage.md): the MCP tools and the CLI commands
- [How it works](docs/how-it-works.md): why login and serving are separate processes, and the files
- [Troubleshooting](docs/troubleshooting.md): symptoms, causes, fixes, and what to put in a bug report

## License

[GPL-3.0-or-later](LICENSE)

<!-- mcp-name: io.github.GeiserX/atlassian-browser-mcp -->
