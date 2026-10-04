# CLAUDE.md - atlassian-browser-mcp

## Project Overview

MCP server wrapping upstream [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) with browser-cookie authentication via Playwright. Designed for Atlassian Server/Data Center instances behind corporate SSO where API tokens are not available.

## Architecture

- `atlassian_browser_mcp_full.py` — MCP entrypoint. Monkey-patches upstream `JiraClient` and `ConfluenceClient` constructors to inject browser-backed sessions. Registers `atlassian_login` tool. Runs the MCP server.
- `atlassian_browser_auth.py` — Shared auth core: `BrowserCookieSession` (requests.Session subclass), `interactive_login()`, profile seeding, SSO redirect detection. Both the MCP server and the CLI use this.
- `atlassian_cli.py` + `atlassian-cli` — Command-line front-end over the same auth core (Jira/Confluence get/search, login). Preferred for agents and scripting; no MCP transport involved. See `AGENT_USAGE.md`.
- `run-atlassian-browser-mcp.sh` — MCP launcher: creates venv via `uv`, installs deps, runs upstream compatibility check, starts server.

## ⚠️ Two-process architecture: the server NEVER opens a browser

Authentication and serving are **deliberately separate jobs**:

- **Authentication is interactive and slow** → done **only** by the CLI in the
  foreground (`atlassian-cli login <jira|confluence>`), where a browser can
  actually open. `BrowserCookieSession(allow_interactive=True)` (the CLI
  default) may call `interactive_login()`.
- **Serving is fast, stateless HTTP** → the MCP server constructs sessions with
  `allow_interactive=False`. Such a session reads the saved cookie jar and, on a
  cache miss or 401, raises `AuthRequiredError` **immediately** ("run
  `atlassian-cli login …`") instead of launching a browser.

**Why this exists (the bug it fixes):** the server used to call
`interactive_login()` from inside its detached, async-dispatched process. That
(a) blocked the tool call up to `ATLASSIAN_LOGIN_TIMEOUT_SECONDS` (≈5 min, often
forever) waiting for a human who can't see the window, and (b) ran
`sync_playwright()` on the asyncio event loop, which deadlocks ("Playwright Sync
API inside the asyncio loop"). Net effect: **MCP Atlassian calls hung forever.**

**Hard rules — do not regress:**
- The MCP server (`_patch_*_client_init`, `atlassian_login` tool) must NEVER
  call `interactive_login()` or otherwise drive Playwright in-process. The
  `atlassian_login` tool returns instructions only.
- Any new server-side session must pass `allow_interactive=False`.
- Login happens out-of-band via the CLI; the server reuses the saved jar.

## ⚠️ Authentication: ALWAYS reuse the browser profile (do not re-enter user/pw)

Re-entering corporate username/password + MFA on every run is a real pain and
must be avoided. The whole point of this tool is a **persistent, reusable
session**. Two rules make that reliable:

1. **Seed from the user's real Chrome profile.** On first login, set
   `ATLASSIAN_SEED_FROM_CHROME_PROFILE=Default` (or another profile name / an
   absolute path). This copies the real Chrome profile **once** — cookies, saved
   logins, and any password-manager extension — into the dedicated automation
   profile (`.atlassian-browser-profile/`). Because the user's live corporate
   SSO cookies come along, the first login typically completes **hands-free**,
   no password and no MFA prompt. Chrome 136+ refuses to drive the live profile
   in place, so seeding a copy is the supported way to reuse it.
2. **NEVER delete the browser profile on an auth failure.** A previous version
   ran `shutil.rmtree(profile_dir)` on a transient 401, which silently destroyed
   the long-lived SSO session and forced a full manual re-login every time.
   That self-wipe has been removed and must not come back. On re-auth, only the
   short-lived per-service cookie cache (`.atlassian-browser-state-<svc>.json`)
   is refreshed; the profile is preserved so re-login stays instant.

Jira and Confluence keep **separate cookie jars** (`*-state-jira.json`,
`*-state-confluence.json`) but **share one browser profile**, so a single seeded
SSO session covers both services. All `.atlassian-browser-*` artifacts hold live
credentials and are git-ignored — never commit them.

Operational how-to (commands, env vars) lives in [`AGENT_USAGE.md`](AGENT_USAGE.md);
this section is the rationale and the two hard rules.

## Key Design Decisions

- **Monkey-patching over forking**: We patch upstream client `__init__` methods at import time rather than maintaining a fork. An `assert_upstream_compatibility()` check validates signatures on startup.
- **Version pinning**: Pinned to `mcp-atlassian>=0.23.1,<1.0.0`. Bump carefully — upstream signature changes will break patches.
- **No hardcoded URLs**: `JIRA_URL` and `CONFLUENCE_URL` are required env vars with no defaults.
- **Configurable SSO detection**: `ATLASSIAN_SSO_MARKERS` env var accepts comma-separated markers. Defaults cover Okta, ADFS, Azure AD, PingOne, Google SAML.

## Development Guidelines

- Keep browser auth logic in `atlassian_browser_auth.py`, MCP/patching logic in `atlassian_browser_mcp_full.py`.
- When upstream `mcp-atlassian` releases a new minor version, update the pin in `pyproject.toml` and verify `assert_upstream_compatibility()` passes.
- Never store credentials, URLs, or company-specific references in source code.

*Generated by [LynxPrompt](https://lynxprompt.com) CLI*

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

## Where the tracker syncs

This repo is public, so its tracker syncs only to the private remote named by `sync.remote` in `.beads/config.yaml`. The block above says sync uses "your git remote". Here that never means this GitHub repo. Don't add it as a Dolt remote and don't push `refs/dolt/*` to it.
