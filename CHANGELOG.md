# Changelog

## 1.5.3 - 2026-11-23

### Security

- Requires mcp-atlassian 0.24.8 (#54). Version 1.5.2 allowed mcp-atlassian 0.24.7, so a new install could still get it. In 0.24.7, a Confluence comment body that starts with `<` is sent as raw storage format, although the comment tools document Markdown (GHSA-68x4-2r5j-m2jr). Crafted Markdown, wiki markup or ADF can keep a Jira or Confluence conversion busy for a long time (GHSA-6567-g8vf-xq69, GHSA-rw8f-cvm7-4j63). With PAC/WPAD proxy support on, Jira and Confluence sessions lose the dot-segment request guard (GHSA-v49j-6r3q-mm6w). Some paths still let a caller reach issues outside `JIRA_PROJECTS_FILTER` (GHSA-45jx-jvrm-qvm2). 0.24.8 fixes all of these. Its other three advisories cover the HTTP transport, which this server does not run.

### Changed

- `confluence_add_comment`, `confluence_reply_to_comment` and `confluence_add_inline_comment` now always treat the body as Markdown, also when it starts with `<`. To send Confluence storage format, pass `content_format="storage"`.
- Responses that are not streamed now stop at 64 MiB after decompression. Set `ATLASSIAN_MAX_RESPONSE_BYTES` to change the limit. Attachment downloads are streamed and have no limit.
- `ATLASSIAN_MAX_CONCURRENT_REQUESTS` and `ATLASSIAN_REQUESTS_PER_SECOND` now count per site. With Jira and Confluence on different hosts, the total can be up to twice the value you set.

## 1.5.2 - 2026-11-23

### Security

- Requires mcp-atlassian 0.24.7 (#51). Version 1.5.1 allowed mcp-atlassian 0.24.6, so a new install could still get it. In 0.24.6, a Confluence write with Markdown content that holds a short URL followed by many dots can run without end (GHSA-62qq-4q8q-5244). 0.24.7 fixes this.

## 1.5.1 - 2026-11-17

### Security

- Requires mcp-atlassian 0.24.6 (#48). Version 1.5.0 allowed any mcp-atlassian from 0.24.1, so a new install could get 0.24.5. In 0.24.5, Confluence writes with Markdown content that run at the same time can store malformed content, or the content of another call (GHSA-3c26-hw8g-vvc9). 0.24.6 fixes this.
- The lock moves soupsieve to 4.1, which fixes a denial of service in CSS selector matching (GHSA-mmhr-3c5m-f64f) (#44).
- mcp-atlassian 0.24.4 and later require cryptography 50 or newer, so `uvx` and `pip` installs now get cryptography 50 too. The lock override from 1.5.0 is removed (#44).

### Fixed

- Jira and Confluence attachments by path and `content_file` work on Windows again. mcp-atlassian 0.24.1 and 0.24.2 failed every confined file read or write there with `PermissionError` (#42).
- The stdio server exits on SIGTERM and Ctrl+C, and attachment download responses are closed (mcp-atlassian 0.24.4) (#44).

### Changed

- Requires Python 3.11.5 or later, because soupsieve 4 needs it (#44).
- The lock moves Playwright to 1.65.1 (#47).

## 1.5.0 - 2026-11-11

### Security

- File tools no longer reach files outside a dedicated folder (GHSA-gv82-cfv8-5p3p). Confluence `content_file` and the Jira and Confluence attachment uploads used to accept any path under the directory the MCP client started the server in, often `/` or your home directory. Text in a Jira issue or Confluence page could then steer an agent into publishing a local file, such as an SSH key. Now they only read files inside `MCP_ATLASSIAN_FILES_DIR`, by default `~/.atlassian-browser-mcp/files` (created with mode 0700). The server refuses to start when that folder is a filesystem root or contains your home directory, by any spelling. Reported by @Gal3m.
- Requires mcp-atlassian 0.24.1, the first release that honours `MCP_ATLASSIAN_FILES_DIR` on stdio. The server refuses to start on an older upstream that still confines file tools to the working directory.
- mcp-atlassian 0.24.1 caps cryptography below 47, and 46.x has published advisories. The lock overrides the cap and keeps cryptography 50.0.2, so checkout and launcher installs are not affected. `uvx` and `pip` installs follow upstream's cap and get cryptography 46.x until upstream raises it.

### Changed

- A `content_file` or attachment path outside the files folder, including any path relative to the launch directory, no longer works. Relative paths now resolve inside the files folder. To use files from a workspace, set `MCP_ATLASSIAN_FILES_DIR` to that folder. Keep this server's checkout and cookie jars out of it.
- With mcp-atlassian 0.24.1, the Cloud-only Jira tools `jira_batch_get_changelogs` and `jira_move_issue` are no longer listed for Server/Data Center URLs.

## 1.4.1 - 2026-10-27

### Added

- Published on PyPI as `atlassian-browser-mcp`: `uvx atlassian-browser-mcp` runs the server and `uvx --from atlassian-browser-mcp atlassian-cli login jira` saves a session, so no checkout is needed (#33).
- Listed in the official MCP Registry as `io.github.GeiserX/atlassian-browser-mcp` (`server.json`) (#33).
- Release workflow: a `v*` tag builds the package, publishes it to PyPI through a trusted publisher (OIDC, no stored token) and then publishes `server.json` to the registry (#33).

## 1.4.0 - 2026-10-27

### Security

- The patched Jira and Confluence clients now run upstream mcp-atlassian's own constructor and add the browser cookies afterwards, so upstream's SSRF redirect check, DNS pinning, retries, rate limits, proxy and TLS settings stay in force on browser-cookie sessions (#31).
- Cookie jars keep only the cookies of their own service host and are written with mode 0600 (#31).
- After an expired session only GET, HEAD and OPTIONS requests are sent again; a write gets an `AuthRequiredError` instead, so it never runs twice (#31).
- The server refuses to start on any transport other than stdio (#31).
- Requires `cryptography>=44.0.1`, which carries the fixes for CVE-2024-26130 and CVE-2024-12797 (#31).

### Added

- `atlassian_auth_status` tool: reports which service has a live session, so an agent can ask for a login before it fails mid-task (#31).
- Every tool carries a title and all four MCP tool hints (#31).
- Jira-only and Confluence-only setups work (#31).
- Client setup instructions for Claude Code, Claude Desktop, Cursor, VS Code and Codex in the README (#31).
- Exact dependency versions locked in `uv.lock`; install with `uv sync --frozen` (#31).
- CodeQL analysis workflow (#31).

### Changed

- Seeding the automation profile from your real Chrome profile is opt-in (`ATLASSIAN_SEED_FROM_CHROME_PROFILE`) (#31).
- On macOS the background session reuse from installed Chromium-family browsers says on stderr that it reads the browser's Keychain key, and can be turned off (#31).
- Requires mcp-atlassian 0.23.1 or newer, so Confluence page operations keep working (#18, #21).
- License metadata reads GPL-3.0-or-later (#27).

### CI

- The test suite runs on every pull request on Python 3.11, 3.12 and 3.13 (#22).
- The upstream tracker issue reports Dependabot status correctly (#23), and the stale job keeps its state between runs (#26).
