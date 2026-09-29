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
uv venv --python 3.11 .venv-atlassian-browser && uv pip install --python .venv-atlassian-browser/bin/python -e .
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

The launcher creates the venv if it is missing, installs the dependencies, checks the upstream `mcp-atlassian` version and starts the server over stdio. The server reuses the cookies from the CLI login and never opens a browser; when they expire, it returns an `AuthRequiredError` asking you to run `./atlassian-cli login <service>` again.
