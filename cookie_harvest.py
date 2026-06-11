#!/usr/bin/env python3
"""Harvest live Atlassian cookies from installed Chromium-family browsers (macOS).

Why this exists: this tool's whole job is to reuse an existing corporate SSO
session instead of forcing a fresh interactive login every time. Historically it
seeded from one hardcoded browser (Chrome) — but the user's live session often
lives in a *different* browser (Arc, Brave, Edge, ...). When the seeded browser
has no session, login fails. This module scans EVERY installed Chromium-family
browser, decrypts its Jira/Confluence cookies, and lets the caller use whichever
browser actually has a live session — turning "log in again" into "reuse what's
already there".

macOS specifics (all verified empirically):
  - Each browser encrypts cookie *values* with a key in the login Keychain under
    a service named "<Browser> Safe Storage" (e.g. "Arc Safe Storage"). The key
    is read non-interactively via `security find-generic-password -w -s <svc>`.
  - v10 scheme: AES-128-CBC, key = PBKDF2-HMAC-SHA1(keychain_pw, b"saltysalt",
    1003, dklen=16), IV = 16 spaces, PKCS7 padding. Newer Chromium prepends a
    32-byte SHA256(domain) authenticity hash to the plaintext — stripped here.
  - v20 scheme (Chrome 127+ "app-bound encryption"): NOT decryptable out of band
    (the key is sealed to the app). Such cookies are skipped, not fatal.
  - The cookie DB is SQLite and is locked while the browser runs, so we always
    read from a temp copy.

This module performs NO network I/O and opens NO browser window. It only reads
local cookie DBs + the Keychain. The caller decides what to do with the result.
"""

from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from hashlib import pbkdf2_hmac
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Chromium stores expires_utc as microseconds since 1601-01-01 (the Windows
# FILETIME epoch). This is the offset to the Unix epoch in seconds.
_CHROME_EPOCH_OFFSET_S = 11_644_473_600

# PKCS / KDF constants for the v10 scheme (fixed by Chromium, not per-machine).
_KDF_SALT = b"saltysalt"
_KDF_ITERATIONS = 1003
_KDF_KEYLEN = 16
_AES_IV = b" " * 16

_KEYCHAIN_TIMEOUT_S = 10


@dataclass(frozen=True)
class BrowserProfile:
    """A single Chromium-family browser profile that may hold live cookies."""

    browser: str  # logical name, e.g. "arc"
    keychain_service: str  # macOS Keychain service, e.g. "Arc Safe Storage"
    profile: str  # profile dir name, e.g. "Default" / "Profile 1"
    cookie_db: Path  # absolute path to that profile's Cookies SQLite DB


@dataclass(frozen=True)
class _BrowserSpec:
    """Static description of where a browser keeps its data on macOS."""

    name: str
    keychain_service: str
    user_data_dir: Path


@dataclass
class HarvestResult:
    """Outcome of harvesting one (browser, profile) for one base URL."""

    browser: str
    profile: str
    cookies: list[dict] = field(default_factory=list)  # Playwright-cookie shape
    skipped_appbound: int = 0  # count of v20 cookies we couldn't decrypt
    error: str | None = None


def _mac_app_support() -> Path:
    return Path.home() / "Library" / "Application Support"


# Registry of Chromium-family browsers. All share the same cookie DB schema and
# the same "<Browser> Safe Storage" Keychain convention; only paths/service
# names differ. Add a row to support a new browser.
def _browser_specs() -> list[_BrowserSpec]:
    asup = _mac_app_support()
    return [
        _BrowserSpec("chrome", "Chrome Safe Storage", asup / "Google/Chrome"),
        _BrowserSpec("arc", "Arc Safe Storage", asup / "Arc/User Data"),
        _BrowserSpec(
            "brave",
            "Brave Safe Storage",
            asup / "BraveSoftware/Brave-Browser",
        ),
        _BrowserSpec("edge", "Microsoft Edge Safe Storage", asup / "Microsoft Edge"),
        _BrowserSpec("dia", "Dia Safe Storage", asup / "Dia/User Data"),
        _BrowserSpec("chromium", "Chromium Safe Storage", asup / "Chromium"),
        _BrowserSpec("vivaldi", "Vivaldi Safe Storage", asup / "Vivaldi"),
        _BrowserSpec("opera", "Opera Safe Storage", asup / "com.operasoftware.Opera"),
    ]


def _default_browser_order() -> list[str]:
    """Preferred scan order. Arc/Brave first because power users keep their live
    corporate SSO there; Chrome later (and its own cookies are app-bound v20 on
    recent versions anyway). Overridable via ATLASSIAN_COOKIE_SOURCE_BROWSERS."""
    return ["arc", "brave", "vivaldi", "edge", "opera", "chrome", "chromium", "dia"]


def configured_browser_order() -> list[str]:
    """Resolve the browser scan order from env, falling back to the default.

    ATLASSIAN_COOKIE_SOURCE_BROWSERS is a comma-separated list of logical names
    (e.g. "arc,chrome"). Unknown names are ignored. An explicit list also acts
    as an allow-list: only the named browsers are scanned.
    """
    raw = os.environ.get("ATLASSIAN_COOKIE_SOURCE_BROWSERS", "").strip()
    known = {s.name for s in _browser_specs()}
    if raw:
        chosen = [b.strip().lower() for b in raw.split(",") if b.strip()]
        return [b for b in chosen if b in known]
    return _default_browser_order()


def installed_profiles() -> list[BrowserProfile]:
    """Enumerate every installed Chromium-family browser profile with a cookie DB.

    Returns one entry per (browser, profile) in configured scan order. A browser
    with no on-disk cookie DB (not installed / never run) is silently skipped.
    """
    specs = {s.name: s for s in _browser_specs()}
    out: list[BrowserProfile] = []
    for name in configured_browser_order():
        spec = specs.get(name)
        if not spec or not spec.user_data_dir.is_dir():
            continue
        # Default + any "Profile N" dirs. Each has its own Cookies DB.
        candidates = ["Default"] + [
            os.path.basename(p) for p in glob.glob(str(spec.user_data_dir / "Profile*"))
        ]
        for prof in candidates:
            cookie_db = spec.user_data_dir / prof / "Cookies"
            if cookie_db.is_file():
                out.append(
                    BrowserProfile(
                        browser=spec.name,
                        keychain_service=spec.keychain_service,
                        profile=prof,
                        cookie_db=cookie_db,
                    )
                )
    return out


def _read_keychain_password(service: str) -> str | None:
    """Read a browser's Safe Storage key from the login Keychain (non-interactive).

    `security find-generic-password -w -s "<service>"` prints the password and
    exits 0 when present, without prompting (the login keychain is unlocked for
    the logged-in user). Returns None on any failure so the caller can move on.
    """
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    pw = proc.stdout.strip()
    return pw or None


def _derive_key(keychain_pw: str) -> bytes:
    return pbkdf2_hmac(
        "sha1", keychain_pw.encode("utf-8"), _KDF_SALT, _KDF_ITERATIONS, _KDF_KEYLEN
    )


def decrypt_value(encrypted: bytes, key: bytes) -> str | None:
    """Decrypt one Chromium cookie value. Returns None if not decryptable.

    Handles the v10 scheme. v20 (app-bound) and any unknown prefix return None
    so the caller skips the cookie instead of crashing.
    """
    if not encrypted:
        return ""  # genuinely empty value
    prefix = encrypted[:3]
    if prefix != b"v10":
        # v20 = app-bound (Chrome 127+); v11 = Linux; anything else unknown.
        return None
    body = encrypted[3:]
    if len(body) < 16 or len(body) % 16 != 0:
        return None
    try:
        decryptor = Cipher(
            algorithms.AES(key), modes.CBC(_AES_IV), default_backend()
        ).decryptor()
        plain = decryptor.update(body) + decryptor.finalize()
    except Exception:  # noqa: BLE001 - any crypto error => undecryptable, skip
        return None
    # Strip PKCS7 padding.
    if plain and 1 <= plain[-1] <= 16:
        plain = plain[: -plain[-1]]
    # Newer Chromium prepends a 32-byte SHA256(host) authenticity hash to the
    # plaintext. Try a clean decode first; if that fails, strip 32 bytes.
    for candidate in (plain, plain[32:]):
        try:
            return candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _chrome_expires_to_unix(expires_utc: int) -> float | None:
    """Convert Chromium expires_utc (µs since 1601) to a Unix timestamp.

    Returns None for session cookies (expires_utc == 0), which never "expire"
    on a timestamp basis.
    """
    if not expires_utc:
        return None
    return expires_utc / 1_000_000 - _CHROME_EPOCH_OFFSET_S


def _host_matches(host_key: str, target_host: str) -> bool:
    """True if a cookie's host_key applies to target_host (exact or parent domain)."""
    h = host_key.lstrip(".")
    return target_host == h or target_host.endswith("." + h)


def harvest_cookies_for_url(
    profile: BrowserProfile,
    base_url: str,
    include_parent_domains: bool = True,
) -> HarvestResult:
    """Decrypt cookies from one browser profile that apply to base_url.

    Reads the cookie DB from a temp COPY (the live DB is locked while the
    browser runs). Returns cookies in Playwright's storage_state shape so they
    drop straight into an existing context/jar. Never raises for expected
    conditions (missing key, locked DB, app-bound cookies) — those land in
    `error`/`skipped_appbound` so the caller can try the next browser.
    """
    target_host = urlparse(base_url).hostname or ""
    result = HarvestResult(browser=profile.browser, profile=profile.profile)
    if not target_host:
        result.error = "invalid base_url"
        return result

    keychain_pw = _read_keychain_password(profile.keychain_service)
    if keychain_pw is None:
        result.error = f"no keychain key for '{profile.keychain_service}'"
        return result
    key = _derive_key(keychain_pw)

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="atl-cookies-", suffix=".db")
    os.close(tmp_fd)
    try:
        try:
            shutil.copy(profile.cookie_db, tmp_path)
        except OSError as exc:
            result.error = f"could not copy cookie DB: {exc}"
            return result
        try:
            conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT host_key, name, encrypted_value, path, is_secure, "
                "is_httponly, expires_utc FROM cookies"
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            result.error = f"cookie DB read failed: {exc}"
            return result

        now = time.time()
        for host_key, name, enc, path, is_secure, is_httponly, expires_utc in rows:
            if not _host_matches(host_key, target_host):
                continue
            if not include_parent_domains and host_key.lstrip(".") != target_host:
                continue
            expires_unix = _chrome_expires_to_unix(expires_utc)
            if expires_unix is not None and expires_unix < now:
                continue  # already expired
            value = decrypt_value(bytes(enc) if enc else b"", key)
            if value is None:
                if enc and bytes(enc)[:3] not in (b"v10", b""):
                    result.skipped_appbound += 1
                continue
            result.cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": host_key,
                    "path": path or "/",
                    "secure": bool(is_secure),
                    "httpOnly": bool(is_httponly),
                    "expires": expires_unix if expires_unix is not None else -1,
                }
            )
        return result
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "JIRA_URL", "https://jira.example.com"
    )
    print(f"Scanning installed browsers for cookies matching {url}\n")
    for prof in installed_profiles():
        res = harvest_cookies_for_url(prof, url)
        tag = f"{res.browser}/{res.profile}"
        if res.error:
            print(f"  {tag:24} ERROR: {res.error}")
        else:
            print(
                f"  {tag:24} {len(res.cookies):3} cookies"
                f" (skipped {res.skipped_appbound} app-bound)"
            )
