#!/usr/bin/env python3
"""Shared browser-backed authentication helpers for Atlassian requests."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

import requests
from playwright.sync_api import Error, TimeoutError, sync_playwright

logger = logging.getLogger("atlassian-browser-auth")

ServiceName = Literal["jira", "confluence"]


class AuthRequiredError(RuntimeError):
    """Raised when a non-interactive session has no valid cookies.

    The caller (e.g. the MCP server) should surface this to the user as a
    clear instruction to authenticate out-of-band, instead of blocking on an
    interactive browser login. Carries the service so the message can name the
    exact command to run.
    """

    def __init__(self, service: ServiceName, detail: str | None = None) -> None:
        self.service = service
        message = (
            f"Not authenticated for {service}. Run `atlassian-cli login {service}` "
            f"in a terminal to sign in, then retry. (The server never opens a "
            f"browser itself to avoid hanging.)"
        )
        if detail:
            message = f"{message} {detail}"
        super().__init__(message)


# Reentrant: CookieAuth.request holds it while calling interactive_login, which
# takes it again to serialize profile seeding.
_LOGIN_LOCK = threading.RLock()
_USERNAME_SELECTORS = [
    'input[name="identifier"]',
    'input[name="username"]',
    'input[name="email"]',
    'input[type="email"]',
    'input[id*="user"]',
    'input[id*="email"]',
    'input[autocomplete="username"]',
    'input[type="text"]',
]

_DEFAULT_SSO_MARKERS = (
    "oauth2/authorize",
    "The page has timed out",
    "Sign in with your account",
    "saml2/idp/SSOService",
    "/adfs/ls",
    "login.microsoftonline.com",
    "accounts.google.com/o/saml2",
    "auth.pingone.com",
    "login.okta.com",
)


def _env_truthy(name: str, default: bool) -> bool:
    """Check if an environment variable holds a truthy value."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def browser_auth_enabled() -> bool:
    """Return whether browser-based authentication is enabled."""
    return _env_truthy("ATLASSIAN_BROWSER_AUTH_ENABLED", True)


def _default_port(parsed) -> int:
    """Return the effective port for a parsed URL, defaulting by scheme."""
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def url_matches_base(current_url: str, base_url: str) -> bool:
    """Check if current_url belongs to the same origin as base_url."""
    current = urlparse(current_url)
    base = urlparse(base_url)
    return (
        current.scheme == base.scheme
        and current.hostname == base.hostname
        and _default_port(current) == _default_port(base)
    )


@dataclass(frozen=True)
class BrowserAuthConfig:
    """Configuration for browser-based Atlassian authentication."""

    jira_url: str
    confluence_url: str
    username: str | None
    profile_dir: Path
    storage_state: Path
    channel: str
    login_timeout_seconds: int
    jira_login_url: str
    confluence_login_url: str
    user_agent: str
    seed_profile_dir: Path | None

    @classmethod
    def from_env(cls, service: "ServiceName | None" = None) -> "BrowserAuthConfig":
        """Build configuration from environment variables.

        When ``service`` is given, the storage-state cookie cache is namespaced
        per service (Jira vs Confluence) so their cookies do not overwrite each
        other — they share one browser profile (one seeded SSO session) but keep
        separate cookie jars. Passing no service preserves the legacy single
        state file for backward compatibility.
        """
        jira_url = os.environ.get("JIRA_URL", "").rstrip("/")
        confluence_url = os.environ.get("CONFLUENCE_URL", "").rstrip("/")
        # Only the URL of the service being used is required, so a Jira-only or
        # Confluence-only deployment works.
        if service == "jira" and not jira_url:
            raise RuntimeError(
                "JIRA_URL environment variable is required. "
                "Set it to your Jira instance URL (e.g., https://jira.example.com)"
            )
        if service == "confluence" and not confluence_url:
            raise RuntimeError(
                "CONFLUENCE_URL environment variable is required. "
                "Set it to your Confluence instance URL (e.g., https://confluence.example.com)"
            )
        if service is None and not (jira_url or confluence_url):
            raise RuntimeError(
                "Set JIRA_URL and/or CONFLUENCE_URL to your Atlassian instance URL "
                "(e.g., https://jira.example.com)."
            )
        base_dir = Path(__file__).resolve().parent
        return cls(
            jira_url=jira_url,
            confluence_url=confluence_url,
            username=os.environ.get("ATLASSIAN_USERNAME"),
            profile_dir=Path(
                os.environ.get(
                    "ATLASSIAN_BROWSER_PROFILE_DIR",
                    str(base_dir / ".atlassian-browser-profile"),
                )
            ).expanduser(),
            storage_state=cls._resolve_storage_state(base_dir, service),
            # Default to the user's REAL Google Chrome, not Playwright's bundled
            # Chromium. This tool seeds from / reuses the real Chrome profile and
            # its corporate SSO session, so it must drive the same browser. The
            # old "chromium" default sent the server path (which doesn't go
            # through the CLI wrapper that sets this) down a "missing Chromium
            # binary" dead end and broke profile/cookie reuse.
            channel=os.environ.get("ATLASSIAN_BROWSER_CHANNEL", "chrome"),
            login_timeout_seconds=int(
                os.environ.get("ATLASSIAN_LOGIN_TIMEOUT_SECONDS", "300")
            ),
            jira_login_url=os.environ.get(
                "ATLASSIAN_JIRA_LOGIN_URL",
                f"{jira_url}/secure/Dashboard.jspa" if jira_url else "",
            ),
            confluence_login_url=os.environ.get(
                "ATLASSIAN_CONFLUENCE_LOGIN_URL", confluence_url
            ),
            user_agent=os.environ.get(
                "ATLASSIAN_BROWSER_USER_AGENT",
                (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
            ),
            seed_profile_dir=cls._resolve_seed_profile_dir(),
        )

    @staticmethod
    def _resolve_seed_profile_dir() -> Path | None:
        """Resolve the real Chrome profile to seed the automation profile from.

        Chrome 136+ refuses to let automation drive the live default
        user-data-dir in place, so we cannot point Playwright straight at it.
        Instead we copy a real profile ONCE into the dedicated automation dir,
        which carries the user's saved passwords, installed extensions (e.g. a
        password manager) and any existing corporate SSO cookies — so the first
        login is one-click or skipped entirely. Opt in by setting
        ATLASSIAN_SEED_FROM_CHROME_PROFILE to a profile name (e.g. "Default",
        "Profile 1") or an absolute path.
        """
        raw = os.environ.get("ATLASSIAN_SEED_FROM_CHROME_PROFILE")
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate if candidate.is_dir() else None
        # Treat as a profile NAME under the Chrome user-data dir. Reject path
        # separators / traversal so an env typo can't point us outside it.
        if raw in {".", ".."} or "/" in raw or "\\" in raw or os.sep in raw:
            return None
        chrome_root = Path(
            os.environ.get(
                "ATLASSIAN_CHROME_USER_DATA_DIR",
                str(Path.home() / "Library/Application Support/Google/Chrome"),
            )
        ).expanduser().resolve()
        candidate = (chrome_root / raw).resolve()
        if chrome_root not in candidate.parents:
            return None
        return candidate if candidate.is_dir() else None

    @staticmethod
    def _resolve_storage_state(base_dir: Path, service: "ServiceName | None") -> Path:
        """Resolve the cookie-jar path, per service, with legacy adoption.

        - Explicit ATLASSIAN_STORAGE_STATE always wins; when a service is given
          we still namespace it (insert ``-{service}`` before the suffix) so the
          two services never share one jar and overwrite each other's cookies.
        - Otherwise default to ``.atlassian-browser-state-{service}.json`` (or
          the legacy single name when no service is given).
        - One-time migration: if the per-service file does not exist yet but the
          legacy ``.atlassian-browser-state.json`` does, adopt it (copy) so an
          already-authenticated user is NOT forced into a surprise re-login on
          upgrade. Cookies are per-profile, so reusing the legacy jar is sound.
        """
        override = os.environ.get("ATLASSIAN_STORAGE_STATE")
        if override:
            p = Path(override).expanduser()
            if service:
                p = p.with_name(f"{p.stem}-{service}{p.suffix}")
            return p
        if not service:
            return (base_dir / ".atlassian-browser-state.json").expanduser()
        path = (base_dir / f".atlassian-browser-state-{service}.json").expanduser()
        legacy = base_dir / ".atlassian-browser-state.json"
        if not path.exists() and legacy.exists():
            try:
                shutil.copy2(legacy, path)
                path.chmod(0o600)
                print(
                    f"[atlassian-browser-auth] Adopted legacy session for {service} "
                    "(no re-login needed).",
                    file=sys.stderr,
                    flush=True,
                )
            except OSError:
                pass
        return path

    def service_base(self, service: ServiceName) -> str:
        """Return the base URL for the given service."""
        base = self.jira_url if service == "jira" else self.confluence_url
        if not base:
            raise RuntimeError(
                f"{service.upper()}_URL environment variable is required for {service}."
            )
        return base

    def login_target(self, service: ServiceName) -> str:
        """Return the login entry point URL for the given service."""
        return self.jira_login_url if service == "jira" else self.confluence_login_url


def _wait_for_any_selector(
    page, selectors: list[str], timeout_ms: int = 1800
) -> str | None:
    """Wait for any of the given selectors to become visible."""
    try:
        page.locator(", ".join(selectors)).first.wait_for(
            state="visible",
            timeout=timeout_ms,
        )
    except TimeoutError:
        return None
    except Error:
        return None

    for selector in selectors:
        try:
            if page.locator(selector).first.is_visible():
                return selector
        except Error:
            continue
    return None


def _best_effort_prefill(page, username: str | None) -> None:
    """Attempt to prefill the username field on the login page."""
    if not username:
        return
    selector = _wait_for_any_selector(page, _USERNAME_SELECTORS)
    if not selector:
        return
    try:
        page.locator(selector).first.fill(username)
        print(
            f"[atlassian-browser-auth] Prefilled username into {selector}",
            file=sys.stderr,
            flush=True,
        )
    except Error as exc:
        print(
            f"[atlassian-browser-auth] Could not prefill username: {exc}",
            file=sys.stderr,
            flush=True,
        )


# Profile subpaths that must NOT be copied when seeding: caches (huge, useless)
# and the singleton lock files Chrome uses to enforce one process per profile —
# copying those would make the cloned profile think another Chrome owns it.
_SEED_SKIP = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "GraphiteDawnCache",
    "DawnCache",
    "Service Worker",
    "Singleton Lock",
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "lockfile",
}


# Sentinel written only after a complete, verified seed. We guard on this
# (not on the existence of "Default/", which Playwright itself creates on first
# launch) so a partial/failed copy is never mistaken for a finished seed.
_SEED_SENTINEL = ".seeded"


def _seed_profile_if_needed(cfg: BrowserAuthConfig) -> None:
    """One-time copy of a real Chrome profile into the automation profile dir.

    Runs only when ATLASSIAN_SEED_FROM_CHROME_PROFILE resolved to an existing
    profile AND this automation profile has not been successfully seeded yet
    (no sentinel). The seed carries the user's cookies, saved logins and
    existing SSO session so the first login is one-click or skipped. Caches and
    singleton locks are skipped so the clone is small and not seen as owned by
    the live Chrome.

    Seeding is atomic-ish and verified: we copy into a temp dir, confirm the
    load-bearing "Cookies" DB landed, then move it into place and write the
    sentinel. A failed/partial copy logs and is discarded, leaving the slot
    free to retry next run (it does NOT lock in a broken profile). To re-seed,
    delete the automation profile dir.
    """
    seed = cfg.seed_profile_dir
    if not seed or not seed.exists():
        return
    dest = cfg.profile_dir
    if (dest / _SEED_SENTINEL).exists():
        return
    # Don't clobber a real pre-existing profile (older code, or a prior login
    # with no seed). A genuine profile has multiple core files; require Cookies
    # AND Preferences so a lone Cookies file — the signature of a seed that was
    # interrupted after the Cookies move but before the sentinel — is treated as
    # a half-copy and retried/overwritten rather than locked in.
    default = dest / "Default"
    if (default / "Cookies").exists() and (default / "Preferences").exists():
        return

    print(
        f"[atlassian-browser-auth] Seeding automation profile from {seed} "
        "(one-time copy of cookies and saved logins).",
        file=sys.stderr,
        flush=True,
    )
    staging = dest.parent / f"{dest.name}.seed-tmp"
    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, mode=0o700)
        _copy_tree_filtered(seed, staging / "Default")
        local_state = seed.parent / "Local State"
        if local_state.exists():
            shutil.copy2(local_state, staging / "Local State")

        # Verify the most load-bearing file actually copied. Without Cookies the
        # seed is useless (it would silently fall through to a manual login), so
        # treat a missing/empty Cookies DB as a failed seed.
        cookies = staging / "Default" / "Cookies"
        if not cookies.exists() or cookies.stat().st_size == 0:
            raise RuntimeError(
                "Cookies DB missing/empty after copy (is Chrome running? "
                "quit it and retry)"
            )

        dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        for item in staging.iterdir():
            target = dest / item.name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink()
            shutil.move(str(item), str(target))
        (dest / _SEED_SENTINEL).touch(mode=0o600)
        _chmod_tree_dirs(dest, 0o700)
        print(
            "[atlassian-browser-auth] Seed complete.",
            file=sys.stderr,
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[atlassian-browser-auth] Profile seeding failed ({exc}); "
            "falling back to a blank profile (will retry on next login).",
            file=sys.stderr,
            flush=True,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _chmod_tree_dirs(root: Path, mode: int) -> None:
    """Restrict every directory under root to ``mode`` (defensive: keep 0o700)."""
    try:
        root.chmod(mode)
        for d in root.rglob("*"):
            if d.is_dir() and not d.is_symlink():
                try:
                    d.chmod(mode)
                except OSError:
                    continue
    except OSError:
        pass


def _copy_tree_filtered(src: Path, dst: Path) -> None:
    """Copy a Chrome profile dir, skipping cache and singleton-lock entries."""
    dst.mkdir(parents=True, exist_ok=True, mode=0o700)
    for entry in src.iterdir():
        if entry.name in _SEED_SKIP:
            continue
        target = dst / entry.name
        try:
            if entry.is_dir():
                shutil.copytree(
                    entry, target, dirs_exist_ok=True, symlinks=True,
                    ignore=shutil.ignore_patterns(*_SEED_SKIP),
                )
            else:
                shutil.copy2(entry, target, follow_symlinks=False)
        except (OSError, shutil.Error):
            # Locked/transient files (e.g. an open LevelDB) are non-fatal.
            continue


def interactive_login(
    service: ServiceName = "jira",
    url: str | None = None,
    config: BrowserAuthConfig | None = None,
) -> dict[str, Any]:
    """Open a browser window for interactive SSO/MFA login and save cookies."""
    cfg = config or BrowserAuthConfig.from_env(service)
    # The automation profile is this tool's own directory, so it is kept 0700.
    cfg.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg.profile_dir.chmod(0o700)
    # The jar's parent may be the repo, site-packages or $HOME: never chmod an
    # existing directory. write_private_json creates a missing one as 0700.
    target_url = url or cfg.login_target(service)

    with _LOGIN_LOCK:
        # Seed under the lock: two concurrent logins share cfg.profile_dir and
        # the fixed .seed-tmp staging path, so one could delete/partially move
        # the other's seed and corrupt the profile. Serializing here prevents it.
        _seed_profile_if_needed(cfg)
        print(
            f"[atlassian-browser-auth] Opening browser for {service} login at {target_url}",
            file=sys.stderr,
            flush=True,
        )
        print(
            "[atlassian-browser-auth] Complete SSO / MFA in the browser window. "
            "The request will resume automatically once the page lands on Jira or Confluence.",
            file=sys.stderr,
            flush=True,
        )

        deadline = time.time() + cfg.login_timeout_seconds
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(cfg.profile_dir),
                channel=cfg.channel,
                headless=False,
                viewport={"width": 1440, "height": 960},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(target_url, wait_until="domcontentloaded")
                _best_effort_prefill(page, cfg.username)

                # Auth detection is fully URL-INDEPENDENT and IdP-agnostic. We do
                # NOT watch page.url to decide when login is done — after a SAML
                # form POST the page handle can stay stuck reporting the IdP URL
                # (e.g. the corporate Okta/ADFS/Azure AD SSO host) even though the
                # user is looking at a logged-in Confluence/Jira tab. Instead we
                # probe the browser CONTEXT's cookies against the real REST API
                # every tick via context.request (which shares the context cookie
                # jar). This works for any Atlassian Server/DC instance behind any
                # SSO provider, with no hardcoded host or marker assumptions.
                # max_redirects=0 means an unauthenticated session (302 -> login)
                # surfaces as a non-200 instead of following through to a 200 HTML
                # login page, so only a genuine authenticated 200 closes the window.
                check_path = "/rest/api/space?limit=1" if service == "confluence" else "/rest/api/2/myself"
                api_url = f"{cfg.service_base(service)}{check_path}"
                last_url = None
                while time.time() < deadline:
                    try:
                        current_url = page.url
                        if current_url != last_url:
                            parsed = urlparse(current_url)
                            safe_url = urlunparse(parsed._replace(query="", fragment=""))
                            print(
                                f"[atlassian-browser-auth] Browser now at: {safe_url}",
                                file=sys.stderr,
                                flush=True,
                            )
                            last_url = current_url
                    except Error:
                        # Page may be mid-navigation; keep probing cookies anyway.
                        pass

                    authenticated = False
                    try:
                        resp = context.request.get(
                            api_url,
                            max_redirects=0,
                            fail_on_status_code=False,
                            headers={"Accept": "application/json"},
                            timeout=15000,
                        )
                        authenticated = resp.status == 200
                    except Error:
                        authenticated = False

                    if authenticated:
                        save_login_jar(
                            context.storage_state(),
                            cfg.storage_state,
                            cfg.service_base(service),
                        )
                        context.close()
                        print(
                            f"[atlassian-browser-auth] Login successful for {service}. Cookies saved.",
                            file=sys.stderr,
                            flush=True,
                        )
                        return {
                            "status": "ok",
                            "service": service,
                            "storage_state": str(cfg.storage_state),
                        }
                    time.sleep(1.5)
            except Error as exc:
                context.close()
                raise RuntimeError(
                    f"Browser closed unexpectedly during {service} login: {exc}"
                ) from exc

            try:
                safe_url = urlunparse(urlparse(page.url)._replace(query="", fragment=""))
            except Error:
                safe_url = "(unknown)"
            context.close()
            raise RuntimeError(
                "Timed out waiting for Atlassian login to complete. "
                f"Last page: {safe_url}. "
                f"Increase ATLASSIAN_LOGIN_TIMEOUT_SECONDS (current: {cfg.login_timeout_seconds}s) "
                "or check that JIRA_URL/CONFLUENCE_URL match your post-login redirect."
            )


# Parsed jars, keyed by path, reused while the file's (mtime, size) is unchanged.
# The server builds a new client per tool call; this keeps each call from
# re-reading and re-parsing the jar from disk.
_JAR_CACHE: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}
_JAR_CACHE_LOCK = threading.Lock()


def _jar_stamp(path: Path) -> tuple[int, int] | None:
    """Return (mtime_ns, size) of the jar, or None when it does not exist."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _load_storage_state(path: Path) -> dict[str, Any]:
    """Load and validate the Playwright storage state JSON file.

    The parsed result is cached until the file changes on disk (for example
    after `atlassian-cli login` or a harvest rewrites it).
    """
    stamp = _jar_stamp(path)
    if stamp is not None:
        with _JAR_CACHE_LOCK:
            cached = _JAR_CACHE.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Browser storage state does not exist yet: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        logger.warning("Corrupt storage state file %s, removing: %s", path, exc)
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Browser storage state was corrupt and has been removed: {path}"
        ) from exc

    if not isinstance(data.get("cookies"), list):
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Invalid storage state structure in {path} (missing cookies list)"
        )
    if stamp is not None:
        with _JAR_CACHE_LOCK:
            _JAR_CACHE[path] = (stamp, data)
    return data


def filter_cookies_for_base(
    cookies: list[dict[str, Any]], base_url: str
) -> list[dict[str, Any]]:
    """Keep only cookies that a browser would send to ``base_url``'s host.

    That is cookies for the host itself and for its parent domains (where a
    corporate SSO cookie may live). Everything else in a browser profile
    (other sites, the IdP's own cookies) is dropped.
    """
    return [c for c in cookies if _cookie_matches_base_url(c, base_url)]


def save_login_jar(
    storage_state: dict[str, Any], path: Path, base_url: str
) -> int:
    """Write the service's cookies from a browser storage state as the jar.

    Only cookies for ``base_url``'s host (and parent domains) are kept, and the
    file is 0600 from creation. Returns the number of cookies written.
    """
    from cookie_autoauth import write_storage_state

    cookies = filter_cookies_for_base(storage_state.get("cookies", []), base_url)
    write_storage_state(cookies, Path(path))
    return len(cookies)


def _cookie_matches_base_url(cookie: dict[str, Any], base_url: str) -> bool:
    """Check if a cookie's domain matches the target base URL."""
    hostname = urlparse(base_url).hostname or ""
    domain = (cookie.get("domain") or "").lstrip(".")
    if not domain or domain.count(".") < 1:
        return False
    return hostname == domain or hostname.endswith(f".{domain}")


def _apply_storage_state_cookies(
    session: requests.Session,
    storage_state: dict[str, Any],
    base_url: str,
) -> None:
    """Apply cookies from Playwright storage state to a requests session."""
    session.cookies.clear()
    now = time.time()
    for cookie in storage_state.get("cookies", []):
        if not _cookie_matches_base_url(cookie, base_url):
            continue
        expires = cookie.get("expires")
        if expires and expires not in (-1, 0) and float(expires) < now:
            continue
        rest: dict[str, Any] = {}
        if cookie.get("httpOnly") is not None:
            rest["HttpOnly"] = cookie.get("httpOnly")
        if cookie.get("sameSite"):
            rest["SameSite"] = cookie.get("sameSite")
        session.cookies.set(
            name=cookie["name"],
            value=cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
            secure=bool(cookie.get("secure")),
            expires=None
            if expires in (None, -1, 0)
            else int(float(expires)),
            rest=rest,
        )


def _load_sso_markers() -> tuple[str, ...]:
    """Load SSO detection markers from env or use sensible defaults."""
    custom = os.environ.get("ATLASSIAN_SSO_MARKERS")
    if custom:
        markers = tuple(m.strip() for m in custom.split(",") if len(m.strip()) >= 3)
        if markers:
            return markers
        logger.warning(
            "ATLASSIAN_SSO_MARKERS is set but contains no valid markers (min 3 chars); "
            "falling back to defaults"
        )
    return _DEFAULT_SSO_MARKERS


def looks_like_sso_response(response: requests.Response) -> bool:
    """Detect whether an HTTP response is an SSO/login redirect."""
    final_url = response.url or ""
    content_type = response.headers.get("Content-Type", "")
    is_html = "text/html" in content_type or "xhtml" in content_type
    body_sample = (
        response.content[:2000].decode(errors="ignore")
        if is_html
        else ""
    )
    markers = _load_sso_markers()
    url_markers = [m for m in markers if "/" in m or "." in m]
    if any(marker in final_url for marker in url_markers):
        return True
    if any(
        any(marker in prior.url for marker in url_markers)
        for prior in response.history
    ):
        return True
    return is_html and any(marker in body_sample for marker in markers)




# Methods that are safe to send again after re-authenticating. Anything else
# (POST, PUT, PATCH, DELETE) may already have changed data, or carry a file
# body that the first send consumed, so it is never replayed automatically.
_REPLAYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Seconds to wait before another background harvest after one finished without
# a live session. Each harvest may read the Keychain, so it must not repeat on
# every tool call.
_HARVEST_COOLDOWN_S = 60.0


@dataclass
class _HarvestState:
    lock: threading.Lock
    thread: threading.Thread | None = None
    last_finished: float = 0.0
    last_result: str | None = None


_HARVESTS: dict[ServiceName, _HarvestState] = {}
_HARVESTS_LOCK = threading.Lock()


def harvest_enabled() -> bool:
    """Return whether reusing cookies from installed browsers is enabled."""
    return _env_truthy("ATLASSIAN_COOKIE_HARVEST", True)


def _harvest_state(service: ServiceName) -> _HarvestState:
    with _HARVESTS_LOCK:
        state = _HARVESTS.get(service)
        if state is None:
            state = _HarvestState(lock=threading.Lock())
            _HARVESTS[service] = state
        return state


def background_harvest_running(service: ServiceName) -> bool:
    """True while a background harvest for ``service`` is in progress."""
    state = _harvest_state(service)
    with state.lock:
        return bool(state.thread and state.thread.is_alive())


def _run_auto_harvest(
    service: ServiceName, base_url: str, storage_state: Path, user_agent: str
) -> bool:
    """Run one bounded harvest; write the jar on success. Never raises."""
    if not harvest_enabled():
        return False
    try:
        # Imported lazily so a harvest-disabled environment never pays for it.
        from cookie_autoauth import auto_harvest
    except Exception as exc:  # noqa: BLE001
        logger.debug("auto-harvest unavailable: %s", exc)
        return False
    try:
        res = auto_harvest(
            service,
            base_url,
            storage_state_path=storage_state,
            user_agent=user_agent,
        )
    except Exception as exc:  # noqa: BLE001 - harvest must never break auth
        logger.debug("auto-harvest error for %s: %s", service, exc)
        return False
    if res.authenticated:
        print(
            f"[atlassian-browser-auth] Reused live {service} session from "
            f"{res.browser}/{res.profile} ({res.cookie_count} cookies), no login needed.",
            file=sys.stderr,
            flush=True,
        )
        return True
    if res.attempts:
        print(
            f"[atlassian-browser-auth] No live {service} session in any browser "
            f"({'; '.join(res.attempts)}).",
            file=sys.stderr,
            flush=True,
        )
    return False


def start_background_harvest(
    service: ServiceName, base_url: str, storage_state: Path, user_agent: str
) -> bool:
    """Look for a live browser session in a daemon thread, never blocking.

    The MCP server calls this instead of harvesting inline: a harvest reads the
    Keychain and probes the network for every browser profile, which can take
    a long time, and the server builds its clients on the asyncio event loop.
    Returns True if a harvest is now running (started here or already running).
    """
    if not harvest_enabled():
        return False
    state = _harvest_state(service)
    with state.lock:
        if state.thread and state.thread.is_alive():
            return True
        if state.last_finished and time.monotonic() - state.last_finished < _HARVEST_COOLDOWN_S:
            return False

        def _worker() -> None:
            ok = _run_auto_harvest(service, base_url, storage_state, user_agent)
            with state.lock:
                state.last_finished = time.monotonic()
                state.last_result = "live" if ok else "none"

        state.thread = threading.Thread(
            target=_worker, name=f"atlassian-harvest-{service}", daemon=True
        )
        state.thread.start()
        return True


class CookieAuth:
    """Browser-cookie authentication installed on an existing requests session.

    It loads the service's saved jar into ``session.cookies``, sets the headers
    Data Center needs for cookie-authenticated writes, and wraps requests so an
    expired session is detected and refreshed. It never replaces the session,
    so whatever the session already carries (upstream's SSRF redirect hook,
    DNS-pinning adapter, retry, rate limit, circuit breaker, proxy and TLS
    settings) stays in force.
    """

    def __init__(
        self,
        session: requests.Session,
        service: ServiceName,
        base_url: str,
        config: BrowserAuthConfig | None = None,
        allow_interactive: bool = True,
    ) -> None:
        self.session = session
        self.service = service
        self.base_url = base_url.rstrip("/")
        self.browser_config = config or BrowserAuthConfig.from_env(service)
        # When False, this session NEVER launches a browser and never harvests
        # inline. It only reads the saved cookie jar and, on a cache miss or
        # auth failure, raises AuthRequiredError instead of blocking. That is
        # what keeps the MCP server from hanging; the CLI keeps True so
        # `atlassian-cli login` can open a browser in the foreground.
        self.allow_interactive = allow_interactive
        self._applied_stamp: tuple[int, int] | None = None

        session.trust_env = False
        session.auth = None
        session.headers.pop("Authorization", None)
        session.headers.update({
            "User-Agent": self.browser_config.user_agent,
            # Jira/Confluence Data Center reject cookie-authenticated mutating
            # requests (POST/PUT/DELETE) as XSRF unless both this header AND a
            # same-origin Origin header are present. Origin is the load-bearing
            # one; X-Atlassian-Token alone is not sufficient.
            "X-Atlassian-Token": "no-check",
            "Origin": self.base_url,
        })
        try:
            self.refresh_cookies()
        except AuthRequiredError:
            # No saved session in non-interactive mode: re-raise so the caller
            # gets an immediate, clear "log in" signal.
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("Cookie loading failed for %s", service, exc_info=True)
            print(
                f"[atlassian-browser-auth] Could not load browser cookies for {service}: {exc}. "
                "Run `atlassian-cli login` to authenticate.",
                file=sys.stderr,
                flush=True,
            )

    # -- cookie loading -------------------------------------------------
    def _apply_jar(self) -> None:
        path = self.browser_config.storage_state
        storage_state = _load_storage_state(path)
        stamp = _jar_stamp(path)
        foreign = [
            c for c in storage_state.get("cookies", [])
            if not _cookie_matches_base_url(c, self.base_url)
        ]
        if foreign:
            # Jars written by older versions held every cookie of the browser
            # profile. Rewrite them with only this service's cookies.
            try:
                save_login_jar(storage_state, path, self.base_url)
                stamp = _jar_stamp(path)
            except OSError as exc:
                logger.debug("Could not prune jar %s: %s", path, exc)
        _apply_storage_state_cookies(self.session, storage_state, self.base_url)
        self._applied_stamp = stamp

    def _background_harvest(self) -> bool:
        return start_background_harvest(
            self.service,
            self.base_url,
            self.browser_config.storage_state,
            self.browser_config.user_agent,
        )

    def _harvest_inline(self) -> bool:
        return _run_auto_harvest(
            self.service,
            self.base_url,
            self.browser_config.storage_state,
            self.browser_config.user_agent,
        )

    def _missing_session_error(self, extra: str | None = None) -> AuthRequiredError:
        """Start a background harvest and build the error the caller raises."""
        details = []
        if self._background_harvest():
            details.append(
                "A search of your installed browsers for a live session is running "
                "in the background; retrying in a few seconds may already work."
            )
        if extra:
            details.append(extra)
        return AuthRequiredError(self.service, " ".join(details) or None)

    def refresh_cookies(self) -> None:
        """Load cookies from the saved jar, harvesting or logging in if needed.

        Order of preference:
          1. A valid saved storage-state jar (fast path, cached by mtime).
          2. Server mode (allow_interactive=False): start a background harvest
             of installed browsers and raise AuthRequiredError at once. The
             next call picks up the jar the harvest wrote.
          3. CLI mode: harvest inline (bounded, no window), then an interactive
             SSO login in a visible browser.
        """
        if self.browser_config.storage_state.exists():
            self._apply_jar()
            return
        if not self.allow_interactive:
            raise self._missing_session_error()
        if self._harvest_inline():
            self._apply_jar()
            return
        if not sys.stdin.isatty() and not os.environ.get("DISPLAY") and sys.platform != "darwin":
            print(
                "[atlassian-browser-auth] WARNING: No display available (headless environment). "
                "Run `atlassian-cli login` manually or set ATLASSIAN_BROWSER_AUTH_ENABLED=false for token auth.",
                file=sys.stderr,
                flush=True,
            )
            return
        interactive_login(self.service, config=self.browser_config)
        if self.browser_config.storage_state.exists():
            self._apply_jar()

    def _reload_if_changed(self) -> bool:
        """Re-apply the jar if it changed on disk since it was applied."""
        stamp = _jar_stamp(self.browser_config.storage_state)
        if stamp is None:
            return False
        if stamp == self._applied_stamp:
            return False
        self._apply_jar()
        return True

    # -- requests -------------------------------------------------------
    @staticmethod
    def _rejected(response: requests.Response) -> bool:
        return looks_like_sso_response(response) or response.status_code == 401

    def request(
        self,
        send: Any,
        method: str,
        url: str,
        *args: Any,
        **kwargs: Any,
    ) -> requests.Response:
        """Send through ``send``; on an SSO page or 401, refresh and maybe retry.

        Only GET, HEAD and OPTIONS are sent again. Any other method gets an
        AuthRequiredError after the session is refreshed, so a write is never
        performed twice and a file body is never re-sent empty.
        """
        retry_on_auth = kwargs.pop("_retry_on_auth", True)
        response = send(method, url, *args, **kwargs)
        if not retry_on_auth or not self._rejected(response):
            return response
        response.close()
        replayable = str(method).upper() in _REPLAYABLE_METHODS

        def _resend() -> requests.Response | None:
            if not replayable:
                return None
            retest = send(method, url, *args, **kwargs)
            if not self._rejected(retest):
                return retest
            retest.close()
            return None

        not_replayed = (
            f"The {str(method).upper()} request was not sent again because it may "
            "change data; run it again once signed in."
        )

        with _LOGIN_LOCK:
            # Another process (`atlassian-cli login`, a harvest) may have written
            # a fresh jar since this one was loaded.
            if self._reload_if_changed():
                retest = _resend()
                if retest is not None:
                    return retest
                if not replayable:
                    raise AuthRequiredError(
                        self.service,
                        "The saved session was reloaded from disk. " + not_replayed,
                    )

            if not self.allow_interactive:
                raise self._missing_session_error(None if replayable else not_replayed)

            # CLI: harvest inline, then a visible login, reusing the browser
            # profile. The profile is never deleted here: it holds the user's
            # long-lived SSO session. Only the short-lived jar is refreshed.
            if self._harvest_inline():
                self._apply_jar()
                if not replayable:
                    raise AuthRequiredError(
                        self.service,
                        "The session was restored from your browser. " + not_replayed,
                    )
                retest = _resend()
                if retest is not None:
                    return retest
            if self.browser_config.storage_state.exists():
                self.browser_config.storage_state.unlink()
            interactive_login(self.service, config=self.browser_config)
            if self.browser_config.storage_state.exists():
                self._apply_jar()
            retest = _resend()
            if retest is not None:
                return retest
        raise AuthRequiredError(self.service, None if replayable else not_replayed)


class BrowserCookieSession(requests.Session):
    """A requests session authenticated with the saved browser cookies (CLI use)."""

    def __init__(
        self,
        service: ServiceName,
        base_url: str,
        config: BrowserAuthConfig | None = None,
        allow_interactive: bool = True,
    ) -> None:
        super().__init__()
        self.cookie_auth = CookieAuth(
            self,
            service,
            base_url,
            config=config,
            allow_interactive=allow_interactive,
        )

    @property
    def service(self) -> ServiceName:
        return self.cookie_auth.service

    @property
    def base_url(self) -> str:
        return self.cookie_auth.base_url

    @property
    def browser_config(self) -> BrowserAuthConfig:
        return self.cookie_auth.browser_config

    @property
    def allow_interactive(self) -> bool:
        return self.cookie_auth.allow_interactive

    def refresh_cookies(self) -> None:
        self.cookie_auth.refresh_cookies()

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Make a request, re-authenticating on SSO redirects or 401s."""
        return self.cookie_auth.request(super().request, method, url, *args, **kwargs)


def install_browser_auth(
    session: requests.Session,
    service: ServiceName,
    base_url: str,
    config: BrowserAuthConfig | None = None,
    allow_interactive: bool = False,
) -> CookieAuth:
    """Authenticate an existing session with the browser cookies, in place.

    Used by the MCP server on the session upstream's client constructor built,
    so every hardening step upstream applied to that session is kept. The
    session's own ``request`` (which may be a PAC/WPAD session's) stays the
    transport; this only wraps it with the re-authentication logic.
    """
    auth = CookieAuth(
        session,
        service,
        base_url,
        config=config,
        allow_interactive=allow_interactive,
    )
    transport = session.request

    def request(method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:
        return auth.request(transport, method, url, *args, **kwargs)

    session.request = request  # type: ignore[method-assign]
    session.atlassian_cookie_auth = auth  # type: ignore[attr-defined]
    return auth


def create_browser_session(
    service: ServiceName,
    base_url: str,
    config: BrowserAuthConfig | None = None,
    allow_interactive: bool = True,
) -> BrowserCookieSession:
    """Create a BrowserCookieSession for the given Atlassian service.

    Pass allow_interactive=False for server contexts so the session reads saved
    cookies but never opens a browser; it raises AuthRequiredError on a cache
    miss instead of hanging.
    """
    return BrowserCookieSession(
        service=service,
        base_url=base_url,
        config=config,
        allow_interactive=allow_interactive,
    )
