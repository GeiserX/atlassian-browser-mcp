# Atlassian access for agents (Jira + Confluence)

**TL;DR for an AI agent:** use the CLI at `./atlassian-cli`. It talks to Jira /
Confluence using a real Chrome browser session (seeded from the user's own
Chrome profile, so saved passwords / Bitwarden / existing SSO all come along).
Read this file, then run the commands below. Do **not** rebuild or reconfigure
anything — it already works once `JIRA_URL` / `CONFLUENCE_URL` are set.

## Why this exists

Many Atlassian Server / Data Center instances sit behind corporate SSO, which
blocks API tokens. This tool captures a real browser session (Playwright +
Chrome) once, saves the cookies, and reuses them for headless REST calls. There
are two front-ends over the same auth core (`atlassian_browser_auth.py`):

- **`./atlassian-cli`** — command-line, best for agents. Start here.
- **MCP server** (`atlassian_browser_mcp_full.py`) — for the Cursor/Claude MCP
  integration. Same auth, same session files.

## Configuration

`JIRA_URL` and `CONFLUENCE_URL` are **required** (no hardcoded defaults). Export
them for your instance, e.g.:

```bash
export JIRA_URL="https://jira.example.com"
export CONFLUENCE_URL="https://confluence.example.com"
```

## First-time setup (once per machine)

The launcher strips sandbox CA env vars that can break TLS and runs in the
project venv. From this directory:

```bash
# Log in once per service. A Chrome window opens; if the user is already
# signed into SSO in their Chrome profile, it completes hands-free.
ATLASSIAN_SEED_FROM_CHROME_PROFILE=Default ./atlassian-cli login jira
ATLASSIAN_SEED_FROM_CHROME_PROFILE=Default ./atlassian-cli login confluence
```

`ATLASSIAN_SEED_FROM_CHROME_PROFILE=Default` copies the user's real Chrome
"Default" profile (cookies, saved logins, Bitwarden extension) into a dedicated
automation profile **once**. This is required on Chrome 136+, which refuses to
let automation drive the live profile in place. Use a different profile name
(e.g. `"Profile 1"`) or an absolute path if needed.

After the first login the saved session is reused automatically — later
commands need **no** browser and **no** seed variable.

## Daily commands (no browser opens once logged in)

```bash
# Jira: get an issue (add --comments for the thread, --raw for full JSON)
./atlassian-cli jira get PROJ-123 --comments

# Jira: JQL search
./atlassian-cli jira search 'project = PROJ AND status = "In Progress"' --max 10

# Confluence: get a page as markdown, optionally write to a file
./atlassian-cli confluence get 123456789 --markdown -o /tmp/page.md

# Confluence: search (text or full CQL)
./atlassian-cli confluence search 'release process' --space DEV
```

## If a command says it can't authenticate

The session expired (~24h). Just re-run `login` for that service:

```bash
ATLASSIAN_SEED_FROM_CHROME_PROFILE=Default ./atlassian-cli login jira
```

The browser profile is **never** auto-deleted, so re-login is usually instant
(the long-lived SSO session in the profile is still valid).

## Files (all git-ignored, local only)

| File | What |
| --- | --- |
| `.atlassian-browser-profile/` | The seeded Chrome automation profile (cookies, logins, Bitwarden) |
| `.atlassian-browser-state-jira.json` | Saved Jira cookie jar |
| `.atlassian-browser-state-confluence.json` | Saved Confluence cookie jar |

## Env vars

| Var | Default | Purpose |
| --- | --- | --- |
| `JIRA_URL` | (required) | Jira base URL |
| `CONFLUENCE_URL` | (required) | Confluence base URL |
| `ATLASSIAN_SEED_FROM_CHROME_PROFILE` | (unset) | Chrome profile name/path to seed from. Set on first `login`. |
| `ATLASSIAN_CHROME_USER_DATA_DIR` | `~/Library/Application Support/Google/Chrome` | Where Chrome profiles live (macOS default) |
| `ATLASSIAN_BROWSER_CHANNEL` | `chrome` | Playwright browser channel |
| `ATLASSIAN_LOGIN_TIMEOUT_SECONDS` | `300` | How long the login window waits for SSO |

## Notes / gotchas

- **Quit Chrome before the first seeded `login`** if the copy looks incomplete:
  a live Chrome can hold the cookie DB mid-write. Re-running `login` re-seeds
  only if the automation profile is empty.
- Jira and Confluence keep **separate** cookie jars but share one browser
  profile, so one seeded SSO session covers both.
- Never commit the `.atlassian-browser-*` files — they contain live session
  cookies. They are git-ignored.
