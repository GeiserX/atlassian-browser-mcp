#!/usr/bin/env python3
"""Shared browser-backed authentication helpers for Atlassian requests."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import requests
from playwright.sync_api import Error, TimeoutError, sync_playwright

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


def _env_truthy(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def browser_auth_enabled() -> bool:
    return _env_truthy("ATLASSIAN_BROWSER_AUTH_ENABLED", True)


@dataclass(frozen=True)
class BrowserAuthConfig:
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
        jira_url = os.environ["JIRA_URL"].rstrip("/")
        confluence_url = os.environ["CONFLUENCE_URL"].rstrip("/")
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
        return self.jira_url if service == "jira" else self.confluence_url

    def login_target(self, service: ServiceName) -> str:
        return self.jira_login_url if service == "jira" else self.confluence_login_url


def _wait_for_any_selector(
    page, selectors: list[str], timeout_ms: int = 1800
) -> str | None:
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
    cfg = config or BrowserAuthConfig.from_env()
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    cfg.storage_state.parent.mkdir(parents=True, exist_ok=True)
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
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(target_url, wait_until="domcontentloaded")
            _best_effort_prefill(page, cfg.username)

            last_url = page.url
            while time.time() < deadline:
                current_url = page.url
                if current_url != last_url:
                    print(
                        f"[atlassian-browser-auth] Browser now at: {current_url}",
                        file=sys.stderr,
                        flush=True,
                    )
                    last_url = current_url

                if current_url.startswith((cfg.jira_url, cfg.confluence_url)):
                    context.storage_state(path=str(cfg.storage_state))
                    context.close()
                    return {
                        "status": "ok",
                        "service": service,
                        "final_url": current_url,
                        "storage_state": str(cfg.storage_state),
                    }
                time.sleep(1)

            current_url = page.url
            context.close()
            raise RuntimeError(
                "Timed out waiting for Atlassian login to complete. "
                f"Last page: {current_url}"
            )


def _load_storage_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Browser storage state does not exist yet: {path}"
        ) from exc


def _cookie_matches_base_url(cookie: dict[str, Any], base_url: str) -> bool:
    hostname = urlparse(base_url).hostname or ""
    domain = (cookie.get("domain") or "").lstrip(".")
    return bool(domain) and (hostname == domain or hostname.endswith(f".{domain}"))


def _apply_storage_state_cookies(
    session: requests.Session,
    storage_state: dict[str, Any],
    base_url: str,
) -> None:
    session.cookies.clear()
    for cookie in storage_state.get("cookies", []):
        if not _cookie_matches_base_url(cookie, base_url):
            continue
        rest: dict[str, Any] = {}
        if cookie.get("httpOnly") is not None:
            rest["HttpOnly"] = cookie.get("httpOnly")
        if cookie.get("sameSite"):
            rest["SameSite"] = cookie.get("sameSite")
        expires = cookie.get("expires")
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
        return tuple(m.strip() for m in custom.split(",") if m.strip())
    return (
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


def looks_like_sso_response(response: requests.Response) -> bool:
    final_url = response.url or ""
    content_type = response.headers.get("Content-Type", "")
    body_sample = response.text[:2000] if "text/" in content_type else ""
    markers = _load_sso_markers()
    url_markers = [m for m in markers if "/" in m or "." in m]
    if any(marker in final_url for marker in url_markers):
        return True
    if any(
        any(marker in prior.url for marker in url_markers)
        for prior in response.history
    ):
        return True
    return "text/html" in content_type and any(marker in body_sample for marker in markers)


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
        except Exception:
            print(
                f"[atlassian-browser-auth] Could not load browser cookies for {service}; "
                "session will start without authentication",
                file=sys.stderr,
                flush=True,
            )

    def refresh_cookies(self) -> None:
        if not self.browser_config.storage_state.exists():
            if not sys.stdin.isatty() and not os.environ.get("DISPLAY"):
                return
            interactive_login(self.service, config=self.browser_config)
        if not self.browser_config.storage_state.exists():
            return
        storage_state = _load_storage_state(self.browser_config.storage_state)
        _apply_storage_state_cookies(self, storage_state, self.base_url)

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:
        retry_on_auth = kwargs.pop("_retry_on_auth", True)
        response = super().request(method, url, *args, **kwargs)
        if retry_on_auth and looks_like_sso_response(response):
            response.close()
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
    return BrowserCookieSession(service=service, base_url=base_url, config=config)
