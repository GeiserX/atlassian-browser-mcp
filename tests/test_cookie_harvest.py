"""Tests for cookie_harvest — synthetic, local-only (no network, no real browser/Keychain).

These mirror the PRD acceptance criteria. Crypto fixtures build *real* v10
ciphertext with the `cryptography` lib using the exact scheme decrypt_value
expects (AES-128-CBC, IV = 16 spaces, PKCS7 pad, b"v10" prefix), so the
round-trip proves the actual decrypt path rather than a stub.

Everything synthetic/temp: _browser_specs and _read_keychain_password are
monkeypatched; ATLASSIAN_COOKIE_SOURCE_BROWSERS is set via monkeypatch. No real
~/Library cookie DBs are read and the `security` binary is never invoked.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import cookie_harvest as ch

# A fixed, fake Keychain password. Never a real secret; just drives the KDF.
FAKE_KEYCHAIN_PW = "test-safe-storage-password"


# --------------------------------------------------------------------------- #
# Synthetic-crypto helpers — mirror decrypt_value's scheme exactly.
# --------------------------------------------------------------------------- #
def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


def _make_v10(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt `plaintext` into a Chromium v10 cookie blob, the way the browser does.

    AES-128-CBC, IV = ch._AES_IV (16 spaces), PKCS7 pad, prefixed with b"v10".
    Uses the module's own constants so the test can't drift from the SUT.
    """
    encryptor = Cipher(
        algorithms.AES(key), modes.CBC(ch._AES_IV), default_backend()
    ).encryptor()
    body = encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()
    return b"v10" + body


def _chrome_us(unix_seconds: float) -> int:
    """Unix seconds -> Chromium expires_utc (µs since 1601), using the module offset."""
    return int((unix_seconds + ch._CHROME_EPOCH_OFFSET_S) * 1_000_000)


@pytest.fixture
def key() -> bytes:
    """The AES key derived from the fake Keychain password via the SUT's own KDF."""
    return ch._derive_key(FAKE_KEYCHAIN_PW)


# --------------------------------------------------------------------------- #
# 1) decrypt_value()
# --------------------------------------------------------------------------- #
class TestDecryptValue:
    def test_v10_round_trip_recovers_plaintext(self, key):
        encrypted = _make_v10(b"hello-session-token", key)
        assert ch.decrypt_value(encrypted, key) == "hello-session-token"

    def test_v10_round_trip_with_non_block_aligned_plaintext(self, key):
        # 13 bytes — exercises PKCS7 padding/stripping across the block boundary.
        encrypted = _make_v10(b"odd-length-13", key)
        assert ch.decrypt_value(encrypted, key) == "odd-length-13"

    def test_strips_32_byte_sha256_prefix_on_utf8_failure(self, key):
        # Newer Chromium prepends SHA256(host) to the plaintext. A real digest is
        # (almost always) invalid UTF-8, so decrypt_value's first decode fails and
        # it retries on plain[32:]. Use a genuine digest to exercise that path.
        sha_prefix = hashlib.sha256(b"jira.example.com").digest()
        assert len(sha_prefix) == 32
        with pytest.raises(UnicodeDecodeError):
            sha_prefix.decode("utf-8")  # guard: the prefix really is non-UTF-8
        encrypted = _make_v10(sha_prefix + b"real-cookie-value", key)
        assert ch.decrypt_value(encrypted, key) == "real-cookie-value"

    def test_does_not_strip_prefix_when_full_plaintext_is_valid_utf8(self, key):
        # Negative control locking the contract: the 32-byte strip only happens
        # on a UTF-8 *failure*. A NUL prefix decodes cleanly, so the whole blob is
        # returned verbatim (prefix NOT stripped). This proves the strip is driven
        # by decode failure, not by an unconditional slice.
        encrypted = _make_v10(b"\x00" * 32 + b"real-cookie-value", key)
        decrypted = ch.decrypt_value(encrypted, key)
        assert decrypted == "\x00" * 32 + "real-cookie-value"
        assert decrypted != "real-cookie-value"

    def test_v20_app_bound_returns_none(self, key):
        # v20 = Chrome 127+ app-bound; the key is sealed to the app, undecryptable.
        assert ch.decrypt_value(b"v20" + b"\x00" * 16, key) is None

    def test_v11_unknown_prefix_returns_none(self, key):
        assert ch.decrypt_value(b"v11" + b"\x00" * 16, key) is None

    def test_arbitrary_unknown_prefix_returns_none(self, key):
        assert ch.decrypt_value(b"xyz" + b"\x00" * 16, key) is None

    def test_empty_encrypted_value_returns_empty_string(self, key):
        assert ch.decrypt_value(b"", key) == ""

    def test_malformed_v10_body_returns_none_no_exception(self, key):
        # b"v10" prefix but a body that is not a whole number of AES blocks.
        assert ch.decrypt_value(b"v10" + b"\x01\x02\x03", key) is None

    def test_wrong_key_does_not_raise(self, key):
        # Decrypting with the wrong key yields garbage/None but must never raise.
        encrypted = _make_v10(b"secret", key)
        wrong_key = ch._derive_key("a-different-password")
        result = ch.decrypt_value(encrypted, wrong_key)
        assert result is None or isinstance(result, str)


# --------------------------------------------------------------------------- #
# 2) _chrome_expires_to_unix()
# --------------------------------------------------------------------------- #
class TestChromeExpiresToUnix:
    def test_zero_means_session_cookie_returns_none(self):
        assert ch._chrome_expires_to_unix(0) is None

    def test_known_microseconds_converts_to_expected_unix(self):
        # 2021-01-01T00:00:00Z == 1609459200 Unix. Build the Chromium µs value
        # from the documented 11_644_473_600 s offset and assert the round-trip.
        unix_target = 1609459200
        chrome_value = (unix_target + 11_644_473_600) * 1_000_000
        assert ch._chrome_expires_to_unix(chrome_value) == pytest.approx(unix_target)

    def test_offset_constant_matches_documented_value(self):
        assert ch._CHROME_EPOCH_OFFSET_S == 11_644_473_600


# --------------------------------------------------------------------------- #
# 3) _host_matches()
# --------------------------------------------------------------------------- #
class TestHostMatches:
    def test_exact_host_matches(self):
        assert ch._host_matches("jira.example.com", "jira.example.com") is True

    def test_parent_domain_with_leading_dot_matches_subdomain(self):
        assert ch._host_matches(".example.com", "jira.example.com") is True

    def test_parent_domain_without_leading_dot_matches_subdomain(self):
        assert ch._host_matches("example.com", "jira.example.com") is True

    def test_unrelated_host_does_not_match(self):
        assert ch._host_matches("evil.com", "jira.example.com") is False

    def test_partial_suffix_is_not_a_match(self):
        # "ample.com" is a string-suffix of "example.com" but NOT a domain
        # parent; the "." + h guard must reject it.
        assert ch._host_matches("ample.com", "example.com") is False

    def test_sibling_subdomain_does_not_match(self):
        assert ch._host_matches("confluence.example.com", "jira.example.com") is False


# --------------------------------------------------------------------------- #
# 4) configured_browser_order()
# --------------------------------------------------------------------------- #
class TestConfiguredBrowserOrder:
    def test_unset_returns_default_order(self, monkeypatch):
        monkeypatch.delenv("ATLASSIAN_COOKIE_SOURCE_BROWSERS", raising=False)
        order = ch.configured_browser_order()
        assert isinstance(order, list)
        assert "arc" in order
        assert "chrome" in order
        assert order == ch._default_browser_order()

    def test_explicit_list_is_honored_in_order(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_COOKIE_SOURCE_BROWSERS", "chrome,arc")
        assert ch.configured_browser_order() == ["chrome", "arc"]

    def test_unknown_names_are_filtered_out(self, monkeypatch):
        monkeypatch.setenv(
            "ATLASSIAN_COOKIE_SOURCE_BROWSERS", "chrome,netscape,arc,lynx"
        )
        assert ch.configured_browser_order() == ["chrome", "arc"]

    def test_whitespace_and_case_are_tolerated(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_COOKIE_SOURCE_BROWSERS", "  Chrome , ARC ")
        assert ch.configured_browser_order() == ["chrome", "arc"]

    def test_empty_string_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ATLASSIAN_COOKIE_SOURCE_BROWSERS", "   ")
        assert ch.configured_browser_order() == ch._default_browser_order()

    def test_all_unknown_names_yield_empty_allow_list(self, monkeypatch):
        # An explicit list acts as an allow-list; if nothing is known, scan nothing.
        monkeypatch.setenv("ATLASSIAN_COOKIE_SOURCE_BROWSERS", "netscape,lynx")
        assert ch.configured_browser_order() == []


# --------------------------------------------------------------------------- #
# 5) + 6) installed_profiles() and harvest_cookies_for_url() with a FAKE browser.
# --------------------------------------------------------------------------- #
def _create_cookie_db(db_path: Path, rows: list[tuple]) -> None:
    """Create a real Chromium-schema Cookies SQLite DB at db_path with given rows.

    rows: (host_key, name, encrypted_value, path, is_secure, is_httponly, expires_utc)
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE cookies ("
            "host_key TEXT, name TEXT, encrypted_value BLOB, path TEXT, "
            "is_secure INTEGER, is_httponly INTEGER, expires_utc INTEGER)"
        )
        conn.executemany(
            "INSERT INTO cookies "
            "(host_key, name, encrypted_value, path, is_secure, is_httponly, "
            "expires_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def fake_browser(tmp_path, monkeypatch, key):
    """Stand up a synthetic Chromium-family browser under tmp_path.

    Builds <tmp>/FakeBrowser/User Data/Default/Cookies with a known set of v10
    (and one v20) cookies, then wires the module to discover it:
      - _browser_specs -> our single fake spec
      - ATLASSIAN_COOKIE_SOURCE_BROWSERS -> "fake" (the configured scan order is
        validated against _browser_specs, so "fake" becomes a known name)
      - _read_keychain_password -> the fixed fake password (no `security` call)

    Yields a small bundle the tests can use to build expectations.
    """
    user_data_dir = tmp_path / "FakeBrowser" / "User Data"
    default_profile = user_data_dir / "Default"
    default_profile.mkdir(parents=True)
    db_path = default_profile / "Cookies"

    future = _chrome_us(time.time() + 86400)  # +1 day, still valid
    past = _chrome_us(time.time() - 86400)  # -1 day, expired

    rows = [
        # exact host, secure + httpOnly, valid
        ("jira.example.com", "JSESSIONID", _make_v10(b"sess-abc", key), "/", 1, 1, future),
        # parent domain (leading dot), valid
        (".example.com", "atlassian.xsrf.token", _make_v10(b"xsrf-123", key), "/", 1, 0, future),
        # exact host but already expired -> must be filtered out
        ("jira.example.com", "STALE", _make_v10(b"stale-value", key), "/", 0, 0, past),
        # app-bound v20 -> undecryptable, must bump skipped_appbound and be omitted
        ("jira.example.com", "APPBOUND", b"v20" + b"\x00" * 16, "/", 1, 1, future),
        # unrelated host -> must never appear
        ("malicious.example.com", "EVIL", _make_v10(b"nope", key), "/", 0, 0, future),
        # session cookie (expires_utc == 0) on exact host -> kept, expires -> -1
        ("jira.example.com", "SESSION_ONLY", _make_v10(b"sticky", key), "/app", 0, 1, 0),
    ]
    _create_cookie_db(db_path, rows)

    spec = ch._BrowserSpec(
        name="fake",
        keychain_service="Fake Safe Storage",
        user_data_dir=user_data_dir,
    )
    monkeypatch.setattr(ch, "_browser_specs", lambda: [spec])
    monkeypatch.setattr(ch, "_read_keychain_password", lambda service: FAKE_KEYCHAIN_PW)
    monkeypatch.setenv("ATLASSIAN_COOKIE_SOURCE_BROWSERS", "fake")

    return {
        "user_data_dir": user_data_dir,
        "db_path": db_path,
        "spec": spec,
        "base_url": "https://jira.example.com",
    }


class TestInstalledProfiles:
    def test_discovers_fake_default_profile(self, fake_browser):
        profiles = ch.installed_profiles()
        assert len(profiles) == 1
        prof = profiles[0]
        assert prof.browser == "fake"
        assert prof.profile == "Default"
        assert prof.keychain_service == "Fake Safe Storage"
        assert prof.cookie_db == fake_browser["db_path"]

    def test_discovers_numbered_profiles(self, fake_browser, key):
        # Add a "Profile 1" alongside "Default"; both should be enumerated.
        prof1 = fake_browser["user_data_dir"] / "Profile 1"
        prof1.mkdir()
        _create_cookie_db(
            prof1 / "Cookies",
            [("jira.example.com", "P1", _make_v10(b"v", key), "/", 1, 1, 0)],
        )
        names = {(p.browser, p.profile) for p in ch.installed_profiles()}
        assert names == {("fake", "Default"), ("fake", "Profile 1")}

    def test_browser_without_cookie_db_is_skipped(self, tmp_path, monkeypatch):
        # user_data_dir exists but has no Default/Cookies -> no profiles, no error.
        empty_udd = tmp_path / "EmptyBrowser" / "User Data"
        (empty_udd / "Default").mkdir(parents=True)
        spec = ch._BrowserSpec("fake", "Fake Safe Storage", empty_udd)
        monkeypatch.setattr(ch, "_browser_specs", lambda: [spec])
        monkeypatch.setenv("ATLASSIAN_COOKIE_SOURCE_BROWSERS", "fake")
        assert ch.installed_profiles() == []

    def test_uninstalled_browser_dir_is_skipped(self, tmp_path, monkeypatch):
        spec = ch._BrowserSpec(
            "fake", "Fake Safe Storage", tmp_path / "does-not-exist"
        )
        monkeypatch.setattr(ch, "_browser_specs", lambda: [spec])
        monkeypatch.setenv("ATLASSIAN_COOKIE_SOURCE_BROWSERS", "fake")
        assert ch.installed_profiles() == []


class TestHarvestCookiesForUrl:
    def test_returns_decrypted_cookies_in_playwright_shape(self, fake_browser):
        profile = ch.installed_profiles()[0]
        result = ch.harvest_cookies_for_url(
            profile, fake_browser["base_url"], include_parent_domains=True
        )
        assert result.error is None
        by_name = {c["name"]: c for c in result.cookies}

        # Exact-host cookie decrypted to the right value, full Playwright shape.
        js = by_name["JSESSIONID"]
        assert js == {
            "name": "JSESSIONID",
            "value": "sess-abc",
            "domain": "jira.example.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expires": js["expires"],  # exact value asserted below
        }
        assert js["expires"] > time.time()  # in the future
        assert set(js.keys()) == {
            "name", "value", "domain", "path", "secure", "httpOnly", "expires",
        }

    def test_parent_domain_cookie_included_when_flag_true(self, fake_browser):
        profile = ch.installed_profiles()[0]
        result = ch.harvest_cookies_for_url(
            profile, fake_browser["base_url"], include_parent_domains=True
        )
        by_name = {c["name"]: c for c in result.cookies}
        assert "atlassian.xsrf.token" in by_name
        # domain is preserved verbatim from host_key, including the leading dot.
        assert by_name["atlassian.xsrf.token"]["domain"] == ".example.com"
        assert by_name["atlassian.xsrf.token"]["value"] == "xsrf-123"

    def test_parent_domain_cookie_excluded_when_flag_false(self, fake_browser):
        profile = ch.installed_profiles()[0]
        result = ch.harvest_cookies_for_url(
            profile, fake_browser["base_url"], include_parent_domains=False
        )
        names = {c["name"] for c in result.cookies}
        assert "atlassian.xsrf.token" not in names
        # Exact-host cookies still come through.
        assert "JSESSIONID" in names

    def test_expired_cookie_is_filtered_out(self, fake_browser):
        profile = ch.installed_profiles()[0]
        result = ch.harvest_cookies_for_url(profile, fake_browser["base_url"])
        names = {c["name"] for c in result.cookies}
        assert "STALE" not in names

    def test_v20_cookie_increments_skipped_appbound_and_is_omitted(self, fake_browser):
        profile = ch.installed_profiles()[0]
        result = ch.harvest_cookies_for_url(profile, fake_browser["base_url"])
        names = {c["name"] for c in result.cookies}
        assert "APPBOUND" not in names
        assert result.skipped_appbound == 1

    def test_unrelated_host_cookie_is_excluded(self, fake_browser):
        profile = ch.installed_profiles()[0]
        result = ch.harvest_cookies_for_url(profile, fake_browser["base_url"])
        names = {c["name"] for c in result.cookies}
        assert "EVIL" not in names

    def test_session_cookie_kept_with_expires_minus_one(self, fake_browser):
        profile = ch.installed_profiles()[0]
        result = ch.harvest_cookies_for_url(profile, fake_browser["base_url"])
        by_name = {c["name"]: c for c in result.cookies}
        assert "SESSION_ONLY" in by_name
        assert by_name["SESSION_ONLY"]["expires"] == -1
        assert by_name["SESSION_ONLY"]["value"] == "sticky"
        assert by_name["SESSION_ONLY"]["path"] == "/app"

    def test_reads_via_temp_copy_so_read_only_source_db_works(self, fake_browser):
        # The module copies the DB to a temp file before opening it. Make the
        # source read-only to prove harvesting doesn't require write access to
        # (or a lock on) the live DB. Must still decrypt successfully.
        fake_browser["db_path"].chmod(0o444)
        try:
            profile = ch.installed_profiles()[0]
            result = ch.harvest_cookies_for_url(profile, fake_browser["base_url"])
            assert result.error is None
            assert any(c["name"] == "JSESSIONID" for c in result.cookies)
        finally:
            fake_browser["db_path"].chmod(0o644)  # restore for tmp cleanup


class TestHarvestGracefulFailures:
    def test_missing_keychain_password_sets_error_no_exception(
        self, fake_browser, monkeypatch
    ):
        monkeypatch.setattr(ch, "_read_keychain_password", lambda service: None)
        profile = ch.installed_profiles()[0]
        result = ch.harvest_cookies_for_url(profile, fake_browser["base_url"])
        assert result.error is not None
        assert "Fake Safe Storage" in result.error
        assert result.cookies == []

    def test_missing_cookie_db_path_sets_error_no_exception(self, tmp_path):
        # Point a BrowserProfile at a cookie DB that does not exist. Even with a
        # valid password, copy fails and the error is captured (no raise).
        missing = ch.BrowserProfile(
            browser="fake",
            keychain_service="Fake Safe Storage",
            profile="Default",
            cookie_db=tmp_path / "nope" / "Cookies",
        )
        # Provide a password so we get past the keychain check to the copy step.
        import unittest.mock as mock

        with mock.patch.object(ch, "_read_keychain_password", return_value="pw"):
            result = ch.harvest_cookies_for_url(missing, "https://jira.example.com")
        assert result.error is not None
        assert result.cookies == []

    def test_invalid_base_url_sets_error(self, fake_browser):
        profile = ch.installed_profiles()[0]
        # No hostname component -> "invalid base_url", returned before any DB work.
        result = ch.harvest_cookies_for_url(profile, "not-a-url")
        assert result.error == "invalid base_url"
        assert result.cookies == []
