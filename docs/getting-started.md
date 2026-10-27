# Getting started

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (for dependency management)
- Google Chrome: the CLI and the server use the real `chrome` channel by default (see [Configuration](configuration.md))
- A graphical display (macOS, X11, or Wayland) for the one-time interactive SSO login
- Network access to your Atlassian instance

## Install and log in

```bash
git clone https://github.com/GeiserX/atlassian-browser-mcp.git && cd atlassian-browser-mcp
UV_PROJECT_ENVIRONMENT=.venv-atlassian-browser uv sync --frozen   # the versions in uv.lock
export JIRA_URL="https://jira.example.com" CONFLUENCE_URL="https://confluence.example.com"
./atlassian-cli login jira          # once per service, in a terminal with a display
./atlassian-cli login confluence
```

## Reusing your real browser session (recommended)

To avoid re-entering your username/password + MFA on every login, **seed the
automation profile once from your real Chrome profile**. The copy carries your
existing SSO cookies (and saved logins / password-manager extension), so the
first login is typically one-click or fully hands-free:

```bash
# macOS
ATLASSIAN_SEED_FROM_CHROME_PROFILE=Default ./atlassian-cli login jira

# Linux: Chrome keeps its profiles elsewhere, so name the directory too
ATLASSIAN_CHROME_USER_DATA_DIR="$HOME/.config/google-chrome" \
  ATLASSIAN_SEED_FROM_CHROME_PROFILE=Default ./atlassian-cli login jira
```

An absolute profile path also works in place of the name.

Chrome 136+ blocks automation from driving the live profile in place, so a
one-time copy into the dedicated profile dir is the supported way to inherit the
session. The profile is **never auto-deleted** on an auth failure, so the
long-lived session persists and re-login stays instant. Jira and Confluence keep
separate cookie jars but share one seeded profile.

## Add the server to your MCP client

Add to your MCP client configuration (only the URL of each service you use is needed):

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

The launcher installs the locked dependencies (`uv sync --frozen`) if they are missing and starts the server over stdio; the server checks the upstream `mcp-atlassian` signatures before serving. Per-client snippets (Claude Code, Claude Desktop, Cursor, VS Code, Codex) are in the README. The server reuses the cookies from the CLI login and never opens a browser; when they expire, it returns an `AuthRequiredError` asking you to run `./atlassian-cli login <service>` again.
