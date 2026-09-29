# Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Tool calls return `AuthRequiredError` | No saved session, and no installed browser has a live one | Run `./atlassian-cli login <jira\|confluence>` in a terminal with a display, then retry |
| Browser doesn't open during `login` | Headless environment (SSH, Docker) | Forward X11 or run the login on a machine with a display |
| Login timed out | Didn't land on the Jira/Confluence URL within 300 s | Check `JIRA_URL`/`CONFLUENCE_URL` match exactly where your IdP redirects after login. Increase `ATLASSIAN_LOGIN_TIMEOUT_SECONDS` if needed |
| Tools return HTML instead of JSON | Session expired, SSO markers not matching your IdP | Set `ATLASSIAN_SSO_MARKERS` with your IdP's URL pattern |
| "Upstream compatibility check failed" | `mcp-atlassian` version changed its internal API | Pin to a compatible version or update the wrapper |
| "Executable doesn't exist" | The configured browser channel is not installed | Install Google Chrome, or run `python -m playwright install chromium` and set `ATLASSIAN_BROWSER_CHANNEL=chromium` |

## Reporting a bug

Open an issue at https://github.com/GeiserX/atlassian-browser-mcp/issues with the version (`pyproject.toml`), the `mcp-atlassian` version, your OS and browser channel, and the error. Remove cookies, tokens and internal hostnames first.
