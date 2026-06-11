# Atlassian access for agents (Jira + Confluence)

**TL;DR for an AI agent:** use the CLI at `./atlassian-cli`. It reuses an
existing Jira/Confluence session — first by **auto-harvesting live cookies from
whatever browser you actually use** (Arc, Chrome, Brave, Edge, …), and only if
none has a live session does it open a real Chrome window for a one-time SSO.
Read this file, then run the commands below. Do **not** rebuild or reconfigure
anything — it already works once `JIRA_URL` / `CONFLUENCE_URL` are set.

## How auth resolves (the order — this is the whole design)

1. **Saved cookie jar** still valid → used immediately, no work.
2. **Auto-harvest** (`cookie_autoauth.py` → `cookie_harvest.py`): scans every
   installed Chromium-family browser, decrypts its Jira/Confluence cookies via
   that browser's macOS Keychain key (the `<Browser> Safe Storage` entry), and
   probes the REST API with a **bounded** request. The first browser that
   answers HTTP 200 wins; its cookies are saved as the jar. **No window opens,
   nothing hangs.** This is why you don't have to be logged into *Chrome*
   specifically — if your live session is in **Arc**, it's reused from Arc.
3. **Interactive SSO** — ONLY when no browser has a live session: a visible
   **real Chrome** window opens at the login page; you complete SSO/MFA once; it
   closes and saves the jar. That Chrome automation profile then keeps the SSO
   session, so subsequent refreshes are hands-free.

The MCP **server never opens a window** (it can harvest silently, but on a true
miss it raises "run `atlassian-cli login`" instead of hanging). Only the CLI,
run by a human in a terminal, ever shows the interactive SSO window.

**Diagnose first, don't guess:** to see exactly which browsers have cookies and
whether any session is live, run `python3 cookie_autoauth.py jira` (or
`confluence`) — it prints a per-browser line like `arc/Default: 7 cookies ->
HTTP 200`. If it says no browser is live, the session genuinely expired and an
SSO `login` is required; that is not a tooling bug.

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

First, the tool already tried to auto-harvest a live session from every browser
and found none live. Confirm what's actually there:

```bash
python3 cookie_autoauth.py jira        # per-browser cookie + live-probe report
```

- If a browser shows `-> HTTP 200`, auth will just work (re-run your command).
- If all show non-200 (e.g. `HTTP 401`), the session genuinely expired
  everywhere. Re-establish it by signing in **in your normal browser** (Arc,
  Chrome, …) — the very next call auto-harvests it. Or do a one-time SSO via the
  tool's own Chrome:

```bash
./atlassian-cli login jira        # opens real Chrome for SSO, then saves the jar
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
| `ATLASSIAN_COOKIE_HARVEST` | `true` | Master switch for auto-harvest. Set falsy to disable and force the saved-jar / interactive path only. |
| `ATLASSIAN_COOKIE_SOURCE_BROWSERS` | (all installed) | Comma list of browsers to harvest from, in order, e.g. `arc,chrome`. Acts as an allow-list. Known: arc, brave, vivaldi, edge, opera, chrome, chromium, dia. |
| `ATLASSIAN_SEED_FROM_CHROME_PROFILE` | (unset) | Chrome profile name/path to seed the interactive-login profile from. Optional now that harvest covers reuse. |
| `ATLASSIAN_CHROME_USER_DATA_DIR` | `~/Library/Application Support/Google/Chrome` | Where Chrome profiles live (macOS default) |
| `ATLASSIAN_BROWSER_CHANNEL` | `chrome` | Playwright browser channel for interactive login (real Chrome, not bundled Chromium) |
| `ATLASSIAN_LOGIN_TIMEOUT_SECONDS` | `300` | How long the login window waits for SSO |

## Notes / gotchas

- **Quit Chrome before the first seeded `login`** if the copy looks incomplete:
  a live Chrome can hold the cookie DB mid-write. Re-running `login` re-seeds
  only if the automation profile is empty.
- Jira and Confluence keep **separate** cookie jars but share one browser
  profile, so one seeded SSO session covers both.
- Never commit the `.atlassian-browser-*` files — they contain live session
  cookies. They are git-ignored.
