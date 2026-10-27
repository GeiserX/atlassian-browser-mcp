# Usage

## MCP tools

The server exposes every tool of the upstream [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) toolsets (limit them with `TOOLSETS`, `ENABLED_TOOLS`, or hide all writes with `READ_ONLY_MODE=true`), backed by your browser session, plus two of its own:

| Tool | Arguments | Returns |
|------|-----------|---------|
| `atlassian_auth_status` | `service`: `jira`, `confluence` or `both` (default); `check_live`: also call the REST API, at most 13 s per service (default `true`) | Per service: configured, ready, jar age, unexpired and expired cookie counts, first cookie expiry, live-check result and HTTP status, whether a background browser search is running, and a one-line next step. Never opens a browser, never reads the Keychain |
| `atlassian_login` | `target`: `jira` (default) or `confluence` | The exact `atlassian-cli login <service>` command for the user to run in a terminal |

Both return structured content with an output schema, plus the same JSON as text for older clients. Every tool, upstream's included, carries a title and all four annotation hints: read tools are read-only and idempotent; write tools are destructive unless they only add something (create, comment, add label, add watcher); all upstream tools are open-world, because they return text people wrote.

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

The agent-oriented guide, including how auth resolves and how to diagnose it with `.venv-atlassian-browser/bin/python cookie_autoauth.py jira`, is [`AGENT_USAGE.md`](https://github.com/GeiserX/atlassian-browser-mcp/blob/main/AGENT_USAGE.md).
