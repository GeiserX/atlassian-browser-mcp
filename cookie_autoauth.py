#!/usr/bin/env python3
"""Auto-authenticate by reusing a live session from ANY installed browser.

This is the "harvest-first" layer that sits in front of interactive login. It:
  1. enumerates installed Chromium-family browser profiles (cookie_harvest),
  2. decrypts each one's Jira/Confluence cookies,
  3. probes the real REST API with a BOUNDED request (no retries, hard timeout),
  4. returns the first browser whose cookies yield HTTP 200 (a live session),
  5. writes those cookies as a Playwright-compatible storage_state file so the
     existing BrowserCookieSession consumes them with zero changes.

Design rules honored:
  - Bounded everywhere: every probe has a hard timeout and no retry loop, so a
    dead/slow endpoint can never hang the caller.
  - Opens no browser window and does no interactive anything — safe to call from
    the MCP server path (allow_interactive=False) so the server can self-heal
    silently when a browser has a live session, while still never launching a UI.
  - Read-only w.r.t. the user's browsers (reads cookie DB copies + Keychain).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

from cookie_harvest import (
    HarvestResult,
    harvest_cookies_for_url,
    installed_profiles,
)

# Per-probe hard ceiling (seconds). Bounded so a dead endpoint can't hang us.
# (connect, read) — kept small because this is a liveness check, not a fetch.
_PROBE_TIMEOUT = (5, 8)

# REST endpoints that return 200 ONLY for an authenticated session and that a
# logged-out request 302-redirects to SSO (which, with redirects disabled,
# surfaces as a non-200 — so only a genuine live session passes).
_VERIFY_PATHS = {
    "jira": "/rest/api/2/myself",
    "confluence": "/rest/api/space?limit=1",
}

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


@dataclass
class AutoAuthResult:
    """Outcome of an auto-harvest attempt for one service."""

    service: str
    authenticated: bool
    browser: str | None = None
    profile: str | None = None
    cookie_count: int = 0
    storage_state_path: str | None = None
    # Per-candidate notes for diagnostics ("arc/Default: 200", "chrome/Default: no cookies").
    attempts: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.attempts is None:
            self.attempts = []


def _probe_live(base_url: str, service: str, cookies: list[dict], user_agent: str) -> int:
    """Return the HTTP status of a bounded, redirect-free liveness probe.

    0 means the request could not be completed (timeout / connection error) —
    treated as "not live" by the caller. Never raises.
    """
    path = _VERIFY_PATHS[service]
    sess = requests.Session()
    sess.trust_env = False  # ignore ambient proxy env that could hijack the probe
    sess.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    for c in cookies:
        try:
            sess.cookies.set(
                c["name"],
                c["value"],
                domain=str(c.get("domain", "")).lstrip("."),
                path=c.get("path", "/"),
            )
        except Exception:  # noqa: BLE001 - a malformed cookie shouldn't kill the probe
            continue
    try:
        resp = sess.get(
            f"{base_url}{path}",
            allow_redirects=False,
            timeout=_PROBE_TIMEOUT,
        )
        return resp.status_code
    except requests.RequestException:
        return 0
    finally:
        sess.close()


def _to_storage_state(cookies: list[dict]) -> dict:
    """Wrap harvested cookies in Playwright storage_state shape."""
    return {"cookies": cookies, "origins": []}


def write_storage_state(cookies: list[dict], path: Path) -> None:
    """Persist cookies as a Playwright storage_state JSON (mode 0600)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_to_storage_state(cookies), indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


def auto_harvest(
    service: str,
    base_url: str,
    storage_state_path: Path | str | None = None,
    user_agent: str | None = None,
) -> AutoAuthResult:
    """Find a browser with a LIVE session for ``service`` and save its cookies.

    Iterates installed browser profiles in configured order, harvests + probes
    each, and stops at the first that returns HTTP 200. On success, writes the
    storage_state jar (if a path is given) and returns a populated result; on
    failure returns authenticated=False with per-candidate diagnostics so the
    caller can decide to fall back to interactive login.
    """
    ua = user_agent or os.environ.get("ATLASSIAN_BROWSER_USER_AGENT", _DEFAULT_UA)
    result = AutoAuthResult(service=service, authenticated=False)

    for prof in installed_profiles():
        harvest: HarvestResult = harvest_cookies_for_url(prof, base_url)
        tag = f"{prof.browser}/{prof.profile}"
        if harvest.error:
            result.attempts.append(f"{tag}: {harvest.error}")
            continue
        if not harvest.cookies:
            note = "no matching cookies"
            if harvest.skipped_appbound:
                note += f" ({harvest.skipped_appbound} app-bound skipped)"
            result.attempts.append(f"{tag}: {note}")
            continue
        status = _probe_live(base_url, service, harvest.cookies, ua)
        result.attempts.append(f"{tag}: {len(harvest.cookies)} cookies -> HTTP {status}")
        if status == 200:
            result.authenticated = True
            result.browser = prof.browser
            result.profile = prof.profile
            result.cookie_count = len(harvest.cookies)
            if storage_state_path is not None:
                path = Path(storage_state_path)
                write_storage_state(harvest.cookies, path)
                result.storage_state_path = str(path)
            return result
    return result


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    svc = sys.argv[1] if len(sys.argv) > 1 else "jira"
    env_key = "JIRA_URL" if svc == "jira" else "CONFLUENCE_URL"
    base = os.environ.get(env_key, "")
    if not base:
        print(f"set {env_key}", file=sys.stderr)
        sys.exit(2)
    res = auto_harvest(svc, base.rstrip("/"))
    for a in res.attempts:
        print(f"  {a}")
    if res.authenticated:
        print(f"\nLIVE session via {res.browser}/{res.profile} ({res.cookie_count} cookies)")
        sys.exit(0)
    print("\nNo browser has a live session — interactive login required.")
    sys.exit(1)
