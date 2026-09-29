# Configuration

All settings are environment variables, read by both the CLI and the server.

| Variable | Default | Description |
|----------|---------|-------------|
| `JIRA_URL` | _(required)_ | Jira base URL (e.g. `https://jira.example.com`) |
| `CONFLUENCE_URL` | _(required)_ | Confluence base URL (e.g. `https://confluence.example.com`) |
| `ATLASSIAN_BROWSER_AUTH_ENABLED` | `true` | Enable browser auth (set `false` to fall back to token auth) |
| `ATLASSIAN_BROWSER_PROFILE_DIR` | `./.atlassian-browser-profile` | Persistent browser profile directory (shared across services) |
| `ATLASSIAN_SEED_FROM_CHROME_PROFILE` | _(none)_ | Seed the profile once from a real Chrome profile (name like `Default`/`Profile 1`, or an absolute path). Brings your cookies, saved logins, and existing SSO session |
| `ATLASSIAN_CHROME_USER_DATA_DIR` | _(macOS Chrome dir)_ | Where Chrome profiles live, for resolving the seed profile name |
| `ATLASSIAN_STORAGE_STATE` | `./.atlassian-browser-state-{service}.json` | Cookie-jar file. Per-service by default; an explicit value is still namespaced per service |
| `ATLASSIAN_COOKIE_HARVEST` | `true` | On macOS, try to reuse a live session from an installed browser before asking for a login (no window opens) |
| `ATLASSIAN_COOKIE_SOURCE_BROWSERS` | `arc,brave,vivaldi,edge,opera,chrome,chromium,dia` | Comma-separated browsers to harvest from, in order (e.g. `arc,chrome`); an explicit list is also an allow-list |
| `ATLASSIAN_LOGIN_TIMEOUT_SECONDS` | `300` | Seconds to wait for manual login |
| `ATLASSIAN_USERNAME` | _(none)_ | Optional: prefill username on SSO page |
| `ATLASSIAN_SSO_MARKERS` | _(auto)_ | Comma-separated URL/text markers for SSO redirect detection. Defaults cover Okta, ADFS, Azure AD, PingOne, Google SAML |
| `ATLASSIAN_BROWSER_CHANNEL` | `chrome` | Browser channel (`chrome`, `chromium`, `msedge`) |
| `ATLASSIAN_JIRA_LOGIN_URL` | `{JIRA_URL}/secure/Dashboard.jspa` | Override the Jira login entry point URL |
| `ATLASSIAN_CONFLUENCE_LOGIN_URL` | `{CONFLUENCE_URL}` | Override the Confluence login entry point URL |
| `ATLASSIAN_BROWSER_USER_AGENT` | _(Chrome 136)_ | Custom User-Agent string for API requests |
| `TOOLSETS` | `all` | Which upstream toolsets to enable |

Both the CLI and the server default to the real `chrome` channel, because the seeded cookies are encrypted with a keychain key only Chrome can read. Set `ATLASSIAN_BROWSER_CHANNEL=chromium` or `msedge` to use another; the launcher installs Playwright's Chromium for that case.
