"""Scenario tests for cookie_autoauth — the harvest-first selection layer.

These prove the "flawless" decision logic across the failure modes the user
cares about, WITHOUT needing a live session, real browsers, the Keychain, or the
network: we monkeypatch the two seams cookie_autoauth depends on —
`installed_profiles()` (which browsers exist) and `harvest_cookies_for_url()`
(what each yields) — plus `requests.Session.get` (the liveness probe). Every test
is synthetic, deterministic, and fast.

Scenario coverage (maps to PRD US-003/US-006):
  (a) a browser HAS a live session   -> selected, jar written, stops early
  (b) NO browser has a live session  -> authenticated=False, clear diagnostics
  (c) cookie DB locked / harvest err -> that browser skipped, next one tried
  (d) expired cookies (probe != 200) -> that browser skipped, next one tried
  (e) app-bound v20 cookies          -> reported, treated as "no cookies"
  (f) probe network failure (status 0) -> treated as not-live, no crash
  (g) browser order is honored (first live wins)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cookie_autoauth as aa
from cookie_harvest import BrowserProfile, HarvestResult


def _profile(browser: str, profile: str = "Default") -> BrowserProfile:
    return BrowserProfile(
        browser=browser,
        keychain_service=f"{browser.title()} Safe Storage",
        profile=profile,
        cookie_db=Path(f"/nonexistent/{browser}/{profile}/Cookies"),
    )


def _cookie(name: str = "JSESSIONID", value: str = "abc123") -> dict:
    return {
        "name": name,
        "value": value,
        "domain": "jira.example.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "expires": -1,
    }


def _patch_profiles(monkeypatch, profiles):
    monkeypatch.setattr(aa, "installed_profiles", lambda: profiles)


def _patch_harvest(monkeypatch, results_by_browser):
    """results_by_browser: {browser_name: HarvestResult} keyed by profile.browser."""
    def fake(profile, base_url, include_parent_domains=True):
        return results_by_browser[profile.browser]
    monkeypatch.setattr(aa, "harvest_cookies_for_url", fake)


def _patch_probe(monkeypatch, status_by_browser):
    """Make _probe_live return a status keyed by which browser's cookies it got.

    We tag each browser's cookies with a sentinel name so the probe stub can tell
    them apart deterministically without touching the network.
    """
    def fake_probe(base_url, service, cookies, user_agent):
        # The first cookie's value encodes the browser (set by the test).
        tag = cookies[0]["value"] if cookies else ""
        return status_by_browser.get(tag, 0)
    monkeypatch.setattr(aa, "_probe_live", fake_probe)


# ---- (a) a browser has a live session -------------------------------------
def test_live_session_selected_and_jar_written(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, [_profile("arc")])
    _patch_harvest(monkeypatch, {
        "arc": HarvestResult("arc", "Default", cookies=[_cookie(value="arc")]),
    })
    _patch_probe(monkeypatch, {"arc": 200})

    jar = tmp_path / "state-jira.json"
    res = aa.auto_harvest("jira", "https://jira.example.com", storage_state_path=jar)

    assert res.authenticated is True
    assert res.browser == "arc"
    assert res.cookie_count == 1
    assert res.storage_state_path == str(jar)
    # jar is a valid Playwright storage_state with our cookie + mode 0600
    data = json.loads(jar.read_text())
    assert data["cookies"][0]["name"] == "JSESSIONID"
    assert "origins" in data
    assert (jar.stat().st_mode & 0o777) == 0o600


# ---- (b) no browser has a live session ------------------------------------
def test_no_live_session_returns_false_with_diagnostics(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, [_profile("arc"), _profile("chrome")])
    _patch_harvest(monkeypatch, {
        "arc": HarvestResult("arc", "Default", cookies=[_cookie(value="arc")]),
        "chrome": HarvestResult("chrome", "Default", cookies=[]),
    })
    _patch_probe(monkeypatch, {"arc": 401})  # arc stale, chrome empty

    jar = tmp_path / "state-jira.json"
    res = aa.auto_harvest("jira", "https://jira.example.com", storage_state_path=jar)

    assert res.authenticated is False
    assert res.browser is None
    assert not jar.exists()  # nothing written on failure
    # diagnostics name both browsers and their outcomes
    joined = "; ".join(res.attempts)
    assert "arc/Default" in joined and "HTTP 401" in joined
    assert "chrome/Default" in joined and "no matching cookies" in joined


# ---- (c) cookie DB locked / harvest error -> skip, try next ---------------
def test_harvest_error_skips_to_next_browser(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, [_profile("brave"), _profile("arc")])
    _patch_harvest(monkeypatch, {
        "brave": HarvestResult("brave", "Default", error="could not copy cookie DB: locked"),
        "arc": HarvestResult("arc", "Default", cookies=[_cookie(value="arc")]),
    })
    _patch_probe(monkeypatch, {"arc": 200})

    res = aa.auto_harvest("jira", "https://jira.example.com", storage_state_path=tmp_path / "j.json")

    assert res.authenticated is True
    assert res.browser == "arc"
    assert any("brave/Default" in a and "locked" in a for a in res.attempts)


# ---- (d) expired cookies (probe != 200) -> skip, try next -----------------
def test_expired_first_browser_falls_through_to_live_second(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, [_profile("chrome"), _profile("arc")])
    _patch_harvest(monkeypatch, {
        "chrome": HarvestResult("chrome", "Default", cookies=[_cookie(value="chrome")]),
        "arc": HarvestResult("arc", "Default", cookies=[_cookie(value="arc")]),
    })
    _patch_probe(monkeypatch, {"chrome": 401, "arc": 200})

    res = aa.auto_harvest("jira", "https://jira.example.com", storage_state_path=tmp_path / "j.json")

    assert res.authenticated is True and res.browser == "arc"
    # chrome was tried first and reported 401 before arc won
    assert res.attempts[0].startswith("chrome/Default") and "HTTP 401" in res.attempts[0]


# ---- (e) app-bound v20 cookies -> reported, treated as no cookies ----------
def test_appbound_cookies_reported_and_skipped(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, [_profile("chrome")])
    _patch_harvest(monkeypatch, {
        "chrome": HarvestResult("chrome", "Default", cookies=[], skipped_appbound=5),
    })
    _patch_probe(monkeypatch, {})  # never called (no cookies)

    res = aa.auto_harvest("jira", "https://jira.example.com", storage_state_path=tmp_path / "j.json")

    assert res.authenticated is False
    assert any("app-bound skipped" in a for a in res.attempts)


# ---- (f) probe network failure (status 0) -> not-live, no crash ------------
def test_probe_network_failure_is_not_live(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, [_profile("arc")])
    _patch_harvest(monkeypatch, {
        "arc": HarvestResult("arc", "Default", cookies=[_cookie(value="arc")]),
    })
    _patch_probe(monkeypatch, {"arc": 0})  # timeout/connection error sentinel

    res = aa.auto_harvest("jira", "https://jira.example.com", storage_state_path=tmp_path / "j.json")

    assert res.authenticated is False
    assert "HTTP 0" in "; ".join(res.attempts)


# ---- (g) first live browser wins (order honored, stops early) -------------
def test_first_live_browser_wins_and_stops(monkeypatch, tmp_path):
    calls = []

    def fake(profile, base_url, include_parent_domains=True):
        calls.append(profile.browser)
        return HarvestResult(profile.browser, "Default", cookies=[_cookie(value=profile.browser)])
    monkeypatch.setattr(aa, "installed_profiles", lambda: [_profile("arc"), _profile("chrome")])
    monkeypatch.setattr(aa, "harvest_cookies_for_url", fake)
    _patch_probe(monkeypatch, {"arc": 200, "chrome": 200})

    res = aa.auto_harvest("jira", "https://jira.example.com", storage_state_path=tmp_path / "j.json")

    assert res.browser == "arc"
    assert calls == ["arc"]  # stopped after the first live one; never harvested chrome


# ---- _probe_live real behavior (bounded, redirect-free) via stubbed get ----
def test_probe_live_disables_redirects_and_is_bounded(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200
        def close(self):
            pass

    def fake_get(self, url, allow_redirects=None, timeout=None):
        captured["allow_redirects"] = allow_redirects
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr("requests.Session.get", fake_get)
    status = aa._probe_live("https://jira.example.com", "jira", [_cookie()], "ua")
    assert status == 200
    assert captured["allow_redirects"] is False  # logged-out 302 must NOT be followed to a 200 login page
    assert captured["timeout"] == aa._PROBE_TIMEOUT  # hard bound, no retries


def test_probe_live_network_error_returns_zero(monkeypatch):
    import requests

    def boom(self, url, **kwargs):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr("requests.Session.get", boom)
    assert aa._probe_live("https://jira.example.com", "jira", [_cookie()], "ua") == 0


def test_write_storage_state_shape_and_perms(tmp_path):
    jar = tmp_path / "sub" / "state.json"
    aa.write_storage_state([_cookie()], jar)
    data = json.loads(jar.read_text())
    assert data["cookies"][0]["name"] == "JSESSIONID"
    assert data["origins"] == []
    assert (jar.stat().st_mode & 0o777) == 0o600
