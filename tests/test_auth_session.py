"""Cookie jar hygiene and the re-authentication rules of the cookie session."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
import requests

import atlassian_browser_auth as aba
import cookie_autoauth as aa
import cookie_harvest as ch
from conftest import cookie, write_jar

BASE = "https://jira.example.com"


# ---- the jar holds only the service's cookies --------------------------------
def test_login_jar_keeps_only_service_cookies(tmp_path):
    state = {
        "cookies": [
            cookie("JSESSIONID", "good", "jira.example.com"),
            cookie("sso", "corp", ".example.com"),
            cookie("bank", "secret", ".bank.test"),
            cookie("gmail", "secret", "accounts.google.com"),
            cookie("lookalike", "secret", ".ample.com"),
        ],
        "origins": [{"origin": "https://mail.google.com", "localStorage": []}],
    }
    jar = tmp_path / "jar.json"
    written = aba.save_login_jar(state, jar, BASE)
    data = json.loads(jar.read_text())
    assert written == 2
    assert sorted(c["name"] for c in data["cookies"]) == ["JSESSIONID", "sso"]
    assert data["origins"] == []
    assert (jar.stat().st_mode & 0o777) == 0o600


def test_old_unfiltered_jar_is_pruned_when_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLASSIAN_STORAGE_STATE", str(tmp_path / "state.json"))
    jar = write_jar(
        tmp_path / "state-jira.json",
        [cookie("JSESSIONID", "good", "jira.example.com"), cookie("bank", "secret", ".bank.test")],
    )
    aba.CookieAuth(requests.Session(), "jira", BASE, allow_interactive=False)
    names = [c["name"] for c in json.loads(jar.read_text())["cookies"]]
    assert names == ["JSESSIONID"]


def test_jar_is_private_from_creation(tmp_path, monkeypatch):
    """Even if every chmod were skipped, the jar must never be group/world readable."""
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)
    monkeypatch.setattr(Path, "chmod", lambda *a, **k: None)
    old = os.umask(0o022)
    try:
        jar = tmp_path / "jar.json"
        aa.write_storage_state([cookie("JSESSIONID", "good", "jira.example.com")], jar)
    finally:
        os.umask(old)
    assert (jar.stat().st_mode & 0o077) == 0
    assert not list(tmp_path.glob("*.tmp"))


def test_existing_parent_directory_is_not_chmodded(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    aa.write_storage_state([], shared / "jar.json")
    assert (shared.stat().st_mode & 0o777) == 0o755


# ---- re-authentication never replays a write -------------------------------
def _response(status: int, url: str = f"{BASE}/rest/api/2/issue") -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp.url = url
    resp.headers["Content-Type"] = "application/json"
    resp._content = b"{}"
    resp._content_consumed = True
    return resp


class _FakeTransport:
    """First call: a fresh jar lands on disk (as after `atlassian-cli login`) and 401."""

    def __init__(self, jar: Path):
        self.jar = jar
        self.calls: list[str] = []

    def __call__(self, method, url, *args, **kwargs):
        self.calls.append(method)
        if len(self.calls) == 1:
            time.sleep(0.01)  # make sure the jar's mtime moves
            write_jar(self.jar, [cookie("JSESSIONID", "fresh", "jira.example.com")])
            return _response(401)
        return _response(200)


@pytest.fixture
def server_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLASSIAN_STORAGE_STATE", str(tmp_path / "state.json"))
    jar = write_jar(tmp_path / "state-jira.json", [cookie("JSESSIONID", "old", "jira.example.com")])
    return aba.CookieAuth(requests.Session(), "jira", BASE, allow_interactive=False), jar


def test_get_is_retried_once_with_the_fresh_jar(server_auth):
    auth, jar = server_auth
    send = _FakeTransport(jar)
    resp = auth.request(send, "GET", f"{BASE}/rest/api/2/issue/X-1")
    assert resp.status_code == 200
    assert send.calls == ["GET", "GET"]
    assert auth.session.cookies.get("JSESSIONID") == "fresh"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_write_is_never_sent_twice(server_auth, method):
    auth, jar = server_auth
    send = _FakeTransport(jar)
    with pytest.raises(aba.AuthRequiredError, match="not sent again"):
        auth.request(send, method, f"{BASE}/rest/api/2/issue")
    assert send.calls == [method]
    # The fresh jar is loaded anyway, so the caller's retry will work.
    assert auth.session.cookies.get("JSESSIONID") == "fresh"


# ---- CLI mode: inline harvest, then a visible login ----------------------------
class _AlwaysRejected:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, method, url, *args, **kwargs):
        self.calls.append(method)
        return _response(401)


@pytest.fixture
def cli_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLASSIAN_STORAGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("ATLASSIAN_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    jar = write_jar(tmp_path / "state-jira.json", [cookie("JSESSIONID", "old", "jira.example.com")])
    return aba.CookieAuth(requests.Session(), "jira", BASE, allow_interactive=True), jar


def test_cli_relogin_reaches_the_browser_instead_of_deadlocking(cli_auth, monkeypatch):
    auth, _jar = cli_auth
    monkeypatch.setattr(aba.CookieAuth, "_harvest_inline", lambda self: False)

    class _ReachedLogin(Exception):
        pass

    def seed(_cfg):  # first step inside interactive_login's own lock
        raise _ReachedLogin

    monkeypatch.setattr(aba, "_seed_profile_if_needed", seed)
    outcome: list[BaseException] = []

    def run():
        try:
            auth.request(_AlwaysRejected(), "GET", f"{BASE}/rest/api/2/myself")
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "re-login blocked on the login lock"
    assert outcome and isinstance(outcome[0], _ReachedLogin)


def test_write_after_inline_harvest_raises_without_opening_a_login(cli_auth, monkeypatch):
    auth, jar = cli_auth

    def harvest(self):
        write_jar(jar, [cookie("JSESSIONID", "harvested", "jira.example.com")])
        return True

    logins: list[str] = []
    monkeypatch.setattr(aba.CookieAuth, "_harvest_inline", harvest)
    monkeypatch.setattr(aba, "interactive_login", lambda service, **kw: logins.append(service))
    send = _AlwaysRejected()
    with pytest.raises(aba.AuthRequiredError, match="not sent again"):
        auth.request(send, "POST", f"{BASE}/rest/api/2/issue")
    assert send.calls == ["POST"]
    assert logins == []
    assert jar.exists()
    assert auth.session.cookies.get("JSESSIONID") == "harvested"


# ---- server mode never harvests on the caller's thread ------------------------
def test_missing_jar_in_server_mode_fails_fast_and_harvests_in_background(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLASSIAN_STORAGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("ATLASSIAN_COOKIE_HARVEST", "1")
    monkeypatch.setattr(aba, "_HARVESTS", {})
    release = threading.Event()
    started = threading.Event()

    def slow_harvest(service, base_url, storage_state_path=None, user_agent=None):
        started.set()
        release.wait(10)  # a Keychain prompt nobody answers, or a slow probe
        return aa.AutoAuthResult(service=service, authenticated=False)

    monkeypatch.setattr(aa, "auto_harvest", slow_harvest)
    t0 = time.monotonic()
    with pytest.raises(aba.AuthRequiredError, match="background"):
        aba.CookieAuth(requests.Session(), "jira", BASE, allow_interactive=False)
    assert time.monotonic() - t0 < 1.0
    assert started.wait(2)
    assert aba.background_harvest_running("jira")
    # A second call while it runs does not start another harvest.
    with pytest.raises(aba.AuthRequiredError):
        aba.CookieAuth(requests.Session(), "jira", BASE, allow_interactive=False)
    release.set()
    aba._HARVESTS["jira"].thread.join(5)
    assert not aba.background_harvest_running("jira")


# ---- Keychain reads are announced, by absolute path ---------------------------
def test_keychain_read_is_announced_once_and_uses_absolute_path(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(ch, "_ANNOUNCED_KEYCHAIN_READS", set())
    argv_seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        argv_seen.append(argv)
        return subprocess.CompletedProcess(argv, 44, "", "")

    monkeypatch.setattr(ch.subprocess, "run", fake_run)
    profile = ch.BrowserProfile("arc", "Arc Safe Storage", "Default", tmp_path / "Cookies")
    with caplog.at_level(logging.WARNING, logger="atlassian-browser-mcp.cookie-harvest"):
        for _ in range(2):
            res = ch.harvest_cookies_for_url(profile, BASE)
            assert res.error
    notices = [r for r in caplog.records if "Arc Safe Storage" in r.getMessage()]
    assert len(notices) == 1
    assert "jira.example.com" in notices[0].getMessage()
    assert "ATLASSIAN_COOKIE_HARVEST=0" in notices[0].getMessage()
    assert argv_seen and all(a[0] == "/usr/bin/security" for a in argv_seen)


def test_probe_refuses_plain_http_without_opt_in(monkeypatch):
    monkeypatch.delenv("ATLASSIAN_ALLOW_HTTP", raising=False)
    monkeypatch.setattr(aa, "installed_profiles", lambda: (_ for _ in ()).throw(AssertionError("scanned")))
    res = aa.auto_harvest("jira", "http://jira.example.com")
    assert not res.authenticated
    assert "not https" in res.attempts[0]


def test_probe_keeps_the_secure_flag(monkeypatch):
    captured = {}

    def fake_get(self, url, allow_redirects=None, timeout=None):
        captured["secure"] = [c.secure for c in self.cookies]
        r = requests.Response()
        r.status_code = 200
        return r

    monkeypatch.setattr("requests.Session.get", fake_get)
    aa._probe_live(BASE, "jira", [cookie("JSESSIONID", "x", "jira.example.com", secure=True)], "ua")
    assert captured["secure"] == [True]
