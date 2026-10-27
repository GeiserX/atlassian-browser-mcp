# Configuration

All settings are environment variables, read by both the CLI and the server.

| Variable | Default | Description |
|----------|---------|-------------|
| `JIRA_URL` | _(none)_ | Jira base URL (e.g. `https://jira.example.com`). Set at least one of the two URLs; a service without a URL is simply not offered |
| `CONFLUENCE_URL` | _(none)_ | Confluence base URL (e.g. `https://confluence.example.com`) |
| `ATLASSIAN_BROWSER_AUTH_ENABLED` | `true` | Enable browser auth (set `false` to fall back to token auth) |
| `ATLASSIAN_BROWSER_PROFILE_DIR` | `./.atlassian-browser-profile` | Persistent browser profile directory (shared across services) |
| `ATLASSIAN_SEED_FROM_CHROME_PROFILE` | _(none)_ | Seed the profile once from a real Chrome profile (name like `Default`/`Profile 1`, or an absolute path). Brings your cookies, saved logins, and existing SSO session |
| `ATLASSIAN_CHROME_USER_DATA_DIR` | _(macOS Chrome dir)_ | Where Chrome profiles live, for resolving the seed profile name |
| `ATLASSIAN_STORAGE_STATE` | `./.atlassian-browser-state-{service}.json` | Cookie-jar file. Per-service by default; an explicit value is still namespaced per service |
| `ATLASSIAN_COOKIE_HARVEST` | `true` | On macOS, try to reuse a live session from an installed browser before asking for a login (no window opens). It reads each browser's "Safe Storage" key from the login Keychain and logs which one, once, on stderr. macOS may ask for consent; answering "Always Allow" lets any local process read that key from then on. The server runs it in a background thread (at most once a minute) and answers "retry in a few seconds"; the CLI runs it inline. Set `false` to turn it off |
| `ATLASSIAN_COOKIE_SOURCE_BROWSERS` | `arc,brave,vivaldi,edge,opera,chrome,chromium,dia` | Comma-separated browsers to harvest from, in order (e.g. `arc,chrome`); an explicit list is also an allow-list |
| `ATLASSIAN_ALLOW_HTTP` | `false` | Allow the harvest's live check against an `http://` URL. Off by default so session cookies never travel in clear text |
| `ATLASSIAN_LOGIN_TIMEOUT_SECONDS` | `300` | Seconds to wait for manual login |
| `ATLASSIAN_USERNAME` | _(none)_ | Optional: prefill username on SSO page |
| `ATLASSIAN_SSO_MARKERS` | _(auto)_ | Comma-separated URL/text markers for SSO redirect detection. Defaults cover Okta, ADFS, Azure AD, PingOne, Google SAML |
| `ATLASSIAN_BROWSER_CHANNEL` | `chrome` | Browser channel (`chrome`, `chromium`, `msedge`) |
| `ATLASSIAN_JIRA_LOGIN_URL` | `{JIRA_URL}/secure/Dashboard.jspa` | Override the Jira login entry point URL |
| `ATLASSIAN_CONFLUENCE_LOGIN_URL` | `{CONFLUENCE_URL}` | Override the Confluence login entry point URL |
| `ATLASSIAN_BROWSER_USER_AGENT` | _(Chrome 136)_ | Custom User-Agent string for API requests |
| `TOOLSETS` | `all` | Which upstream toolsets to enable (`all`, `default` for upstream's six core sets, or a comma-separated list) |
| `READ_ONLY_MODE` | `false` | Upstream switch: `true` hides every write tool (create, update, delete, transition, upload, restrictions) at listing and at call time |
| `ENABLED_TOOLS` | _(all)_ | Upstream switch: comma-separated allow-list of tool names, e.g. `jira_get_issue,jira_search` |
| `FASTMCP_TRANSPORT` | `stdio` | Only `stdio` is accepted; the server refuses to start otherwise, because it acts with the local user's browser session |

Upstream's network settings apply to the cookie session too, because the server lets upstream build each client and only adds the cookies afterwards: `JIRA_SSL_VERIFY` / `CONFLUENCE_SSL_VERIFY`, the `*_HTTP_PROXY`, `*_HTTPS_PROXY`, `*_NO_PROXY` and `*_SOCKS_PROXY` variables, `*_CUSTOM_HEADERS`, `MCP_ALLOWED_URL_DOMAINS`, and the opt-in `ATLASSIAN_RETRY_TOTAL`, `ATLASSIAN_MAX_CONCURRENT_REQUESTS`, `ATLASSIAN_REQUESTS_PER_SECOND` and `ATLASSIAN_CIRCUIT_BREAKER_THRESHOLD`. See upstream's documentation: https://github.com/sooperset/mcp-atlassian

Both the CLI and the server default to the real `chrome` channel, because the seeded cookies are encrypted with a keychain key only Chrome can read. Set `ATLASSIAN_BROWSER_CHANNEL=chromium` or `msedge` to use another. The launcher no longer downloads a browser (the server never opens one); for `chromium`, run `.venv-atlassian-browser/bin/python -m playwright install chromium` once.
