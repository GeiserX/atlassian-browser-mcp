# Usage

## MCP tools

The server exposes every tool of the upstream [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) toolsets (limit them with `TOOLSETS`), backed by your browser session, plus `atlassian_login`, which returns the exact `atlassian-cli login <service>` command to run when a session is missing.

## CLI

The same auth core has a command-line front-end, handy for scripts and agents:

```bash
export JIRA_URL="https://jira.example.com"
export CONFLUENCE_URL="https://confluence.example.com"

./atlassian-cli login jira                       # one-time per service
./atlassian-cli jira get PROJ-123 --comments
./atlassian-cli jira search 'project = PROJ AND status = "In Progress"'
./atlassian-cli confluence get 123456789 --markdown -o page.md
./atlassian-cli confluence search 'release process' --space DEV
```

The agent-oriented guide, including how auth resolves and how to diagnose it with `python3 cookie_autoauth.py jira`, is [`AGENT_USAGE.md`](https://github.com/GeiserX/atlassian-browser-mcp/blob/main/AGENT_USAGE.md).
