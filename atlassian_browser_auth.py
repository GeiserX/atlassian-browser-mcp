#!/usr/bin/env python3
"""Shared browser-backed authentication helpers for Atlassian requests."""

from __future__ import annotations

import json
import logging
import os
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

_LOGIN_LOCK = threading.Lock()
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

    @classmethod
    def from_env(cls) -> "BrowserAuthConfig":
        """Build configuration from environment variables."""
        jira_url = os.environ.get("JIRA_URL", "").rstrip("/")
        confluence_url = os.environ.get("CONFLUENCE_URL", "").rstrip("/")
        if not jira_url:
            raise RuntimeError(
                "JIRA_URL environment variable is required. "
                "Set it to your Jira instance URL (e.g., https://jira.example.com)"
            )
        if not confluence_url:
            raise RuntimeError(
                "CONFLUENCE_URL environment variable is required. "
                "Set it to your Confluence instance URL (e.g., https://confluence.example.com)"
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
            storage_state=Path(
                os.environ.get(
                    "ATLASSIAN_STORAGE_STATE",
                    str(base_dir / ".atlassian-browser-state.json"),
                )
            ).expanduser(),
            channel=os.environ.get("ATLASSIAN_BROWSER_CHANNEL", "chromium"),
            login_timeout_seconds=int(
                os.environ.get("ATLASSIAN_LOGIN_TIMEOUT_SECONDS", "300")
            ),
            jira_login_url=os.environ.get(
                "ATLASSIAN_JIRA_LOGIN_URL", f"{jira_url}/secure/Dashboard.jspa"
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
        )

    def service_base(self, service: ServiceName) -> str:
        """Return the base URL for the given service."""
        return self.jira_url if service == "jira" else self.confluence_url

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


def interactive_login(
    service: ServiceName = "jira",
    url: str | None = None,
    config: BrowserAuthConfig | None = None,
) -> dict[str, Any]:
    """Open a browser window for interactive SSO/MFA login and save cookies."""
    cfg = config or BrowserAuthConfig.from_env()
    cfg.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg.profile_dir.chmod(0o700)
    cfg.storage_state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if cfg.storage_state.parent != cfg.profile_dir:
        cfg.storage_state.parent.chmod(0o700)
    target_url = url or cfg.login_target(service)

    with _LOGIN_LOCK:
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

                last_url = page.url
                while time.time() < deadline:
                    try:
                        current_url = page.url
                    except Error:
                        break

                    if current_url != last_url:
                        parsed = urlparse(current_url)
                        safe_url = urlunparse(parsed._replace(query="", fragment=""))
                        print(
                            f"[atlassian-browser-auth] Browser now at: {safe_url}",
                            file=sys.stderr,
                            flush=True,
                        )
                        last_url = current_url

                    if url_matches_base(current_url, cfg.jira_url) or url_matches_base(
                        current_url, cfg.confluence_url
                    ):
                        context.storage_state(path=str(cfg.storage_state))
                        cfg.storage_state.chmod(0o600)
                        context.close()
                        print(
                            f"[atlassian-browser-auth] Login successful for {service}. Cookies saved.",
                            file=sys.stderr,
                            flush=True,
                        )
                        return {
                            "status": "ok",
                            "service": service,
                            "final_url": current_url,
                            "storage_state": str(cfg.storage_state),
                        }
                    time.sleep(1)
            except Error as exc:
                context.close()
                raise RuntimeError(
                    f"Browser closed unexpectedly during {service} login: {exc}"
                ) from exc

            current_url = page.url
            context.close()
            parsed = urlparse(current_url)
            safe_url = urlunparse(parsed._replace(query="", fragment=""))
            raise RuntimeError(
                "Timed out waiting for Atlassian login to complete. "
                f"Last page: {safe_url}. "
                f"Increase ATLASSIAN_LOGIN_TIMEOUT_SECONDS (current: {cfg.login_timeout_seconds}s) "
                "or check that JIRA_URL/CONFLUENCE_URL match your post-login redirect."
            )


def _load_storage_state(path: Path) -> dict[str, Any]:
    """Load and validate the Playwright storage state JSON file."""
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
    return data


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


class BrowserCookieSession(requests.Session):
    """Requests session that refreshes itself through the Playwright browser profile."""

    def __init__(
        self,
        service: ServiceName,
        base_url: str,
        config: BrowserAuthConfig | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.base_url = base_url.rstrip("/")
        self.browser_config = config or BrowserAuthConfig.from_env()
        self.trust_env = False
        self.headers.update({"User-Agent": self.browser_config.user_agent})
        try:
            self.refresh_cookies()
        except Exception as exc:
            logger.debug("Cookie loading failed for %s", service, exc_info=True)
            print(
                f"[atlassian-browser-auth] Could not load browser cookies for {service}: {exc}. "
                "Run atlassian_login tool to authenticate.",
                file=sys.stderr,
                flush=True,
            )

    def refresh_cookies(self) -> None:
        """Reload cookies from storage state, triggering login if needed."""
        if not self.browser_config.storage_state.exists():
            if not sys.stdin.isatty() and not os.environ.get("DISPLAY") and sys.platform != "darwin":
                print(
                    "[atlassian-browser-auth] WARNING: No display available (headless environment). "
                    "Run atlassian_login tool manually or set ATLASSIAN_BROWSER_AUTH_ENABLED=false for token auth.",
                    file=sys.stderr,
                    flush=True,
                )
                return
            interactive_login(self.service, config=self.browser_config)
        if not self.browser_config.storage_state.exists():
            return
        storage_state = _load_storage_state(self.browser_config.storage_state)
        _apply_storage_state_cookies(self, storage_state, self.base_url)

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:
        """Make a request, automatically re-authenticating on SSO redirects or 401s."""
        retry_on_auth = kwargs.pop("_retry_on_auth", True)
        response = super().request(method, url, *args, **kwargs)
        needs_reauth = looks_like_sso_response(response) or response.status_code == 401
        if retry_on_auth and needs_reauth:
            response.close()
            with _LOGIN_LOCK:
                self.refresh_cookies()
                retest = super().request(method, url, *args, **kwargs)
                if not looks_like_sso_response(retest) and retest.status_code != 401:
                    return retest
                retest.close()
                interactive_login(self.service, config=self.browser_config)
                self.refresh_cookies()
            return self.request(
                method,
                url,
                *args,
                _retry_on_auth=False,
                **kwargs,
            )
        return response


def create_browser_session(
    service: ServiceName,
    base_url: str,
    config: BrowserAuthConfig | None = None,
) -> BrowserCookieSession:
    """Create a BrowserCookieSession for the given Atlassian service."""
    return BrowserCookieSession(service=service, base_url=base_url, config=config)
