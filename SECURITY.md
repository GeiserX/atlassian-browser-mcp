# Security Policy

## Reporting Security Issues

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please use GitHub's private vulnerability reporting:

1. Go to https://github.com/GeiserX/atlassian-browser-mcp/security/advisories
2. Click "Report a vulnerability"
3. Fill out the form with details

We will respond within **48 hours** and work with you to understand and address the issue.

### What to Include

- Type of issue (e.g., cookie leakage, session hijacking, credential exposure)
- Full paths of affected source files
- Step-by-step instructions to reproduce
- Proof-of-concept or exploit code (if possible)
- Impact assessment and potential attack scenarios

### Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.x.x   | Current release   |

Only the latest version receives security updates. We recommend always running the latest version.

## Security Architecture

### Authentication

- **Browser-based SSO.** No API tokens or passwords in config. With browser auth on (the default) the server sends your browser session cookies, never an `Authorization` header. `ATLASSIAN_BROWSER_AUTH_ENABLED=false` hands authentication back to upstream's token settings.
- **Upstream hardening kept.** The server lets upstream mcp-atlassian build each Jira and Confluence client and only adds the cookies afterwards, so upstream's SSRF redirect check, DNS-rebinding pinning, retry, rate-limit, proxy and TLS settings all apply. A test compares the patched session with upstream's own.
- **stdio only.** The server refuses to start on any other transport, because over HTTP any caller would be served with your browser session.
- **No replayed writes.** After an expired session only GET, HEAD and OPTIONS are retried; a write is never sent twice.

### Data on disk

- **Cookie jars** (`.atlassian-browser-state-jira.json`, `.atlassian-browser-state-confluence.json`, next to the code unless `ATLASSIAN_STORAGE_STATE` says otherwise) are plaintext JSON, mode 0600 from creation. They hold only the cookies a browser would send to that service's host and its parent domains. Jars written before 1.4 held every cookie of the browser profile; they are pruned when next loaded. The legacy shared `.atlassian-browser-state.json`, if you still have one, is not pruned: delete it once both per-service jars exist.
- **The automation browser profile** (`.atlassian-browser-profile/`, mode 0700). If you seed it from Chrome (`ATLASSIAN_SEED_FROM_CHROME_PROFILE`), it is a copy of that whole Chrome profile: every site's cookies, saved logins and extensions. Treat it like your Chrome profile. Seeding is off unless you set that variable.

### Browser cookie reuse (macOS)

With `ATLASSIAN_COOKIE_HARVEST` on (the default), a missing or expired session makes the tool read the "Safe Storage" key of each installed Chromium-family browser from your login Keychain, with `/usr/bin/security`, to decrypt that browser's cookies for your Atlassian host. Each key read is announced once on stderr. macOS normally asks for consent; answering **Always Allow** adds `/usr/bin/security` to that Keychain item for good, after which any local process running as you can read the key and decrypt all of that browser's cookies. Answer **Allow** instead, limit the browsers with `ATLASSIAN_COOKIE_SOURCE_BROWSERS`, or turn reuse off with `ATLASSIAN_COOKIE_HARVEST=false`. The live check never sends those cookies over plain `http://` unless `ATLASSIAN_ALLOW_HTTP=1`.

### For Users

1. **Never commit `.atlassian-browser-*` files.** They contain session cookies (the repo's `.gitignore` covers them).
2. **Keep the browser profile directory private.** It may contain cached credentials.
3. **Hide write tools** with `READ_ONLY_MODE=true` if your agents only need to read.
4. **Keep updated.** Run the latest version of both this wrapper and `mcp-atlassian`.

## Contact

For security questions that aren't vulnerabilities, open a regular issue.

---

*Last updated: October 2026*
