"""Shared test setup.

Environment is set before any test module imports the server, because the
server reads JIRA_URL / CONFLUENCE_URL at import time. Browser cookie harvest
is off for the whole run so no test ever reads the macOS Keychain, and the
cookie jars point at a throwaway directory so no real jar is ever touched.
"""

from __future__ import annotations

import http.server
import json
import os
import tempfile
import threading
from pathlib import Path

import pytest

_JAR_DIR = Path(tempfile.mkdtemp(prefix="atl-test-jars-"))

os.environ.setdefault("JIRA_URL", "https://jira.example.com")
os.environ.setdefault("CONFLUENCE_URL", "https://confluence.example.com")
os.environ["ATLASSIAN_COOKIE_HARVEST"] = "0"
os.environ["ATLASSIAN_STORAGE_STATE"] = str(_JAR_DIR / "state.json")
os.environ.pop("FASTMCP_TRANSPORT", None)


def write_jar(path: Path, cookies: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies, "origins": []}))
    path.chmod(0o600)
    return path


def cookie(name: str, value: str, domain: str, secure: bool = False) -> dict:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "secure": secure,
        "httpOnly": True,
        "expires": -1,
    }


class _FakeAtlassian(http.server.BaseHTTPRequestHandler):
    """A tiny Jira/Confluence stand-in that records every request's headers."""

    seen: list[dict[str, str]] = []

    def log_message(self, *args):  # keep test output quiet
        pass

    def _reply(self, code: int, body: dict | None = None, headers: dict | None = None):
        payload = json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()} | {"path": self.path})
        authed = "JSESSIONID=good" in (self.headers.get("Cookie") or "")
        if self.path.startswith("/redirect"):
            # An attacker-steerable redirect to the cloud metadata address.
            self._reply(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        elif self.path.startswith("/rest/api/2/myself"):
            self._reply(200, {"name": "tester"}) if authed else self._reply(401)
        elif self.path.startswith("/rest/api/user/current"):
            user = {"type": "known", "username": "tester"} if authed else {"type": "anonymous"}
            self._reply(200, user)
        else:
            self._reply(404)


@pytest.fixture
def fake_atlassian(monkeypatch, tmp_path):
    """Serve a fake instance on 127.0.0.1 and point both services at it."""
    _FakeAtlassian.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeAtlassian)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("JIRA_URL", base)
    monkeypatch.setenv("CONFLUENCE_URL", base)
    monkeypatch.setenv("ATLASSIAN_STORAGE_STATE", str(tmp_path / "state.json"))
    try:
        yield base, _FakeAtlassian.seen, tmp_path
    finally:
        server.shutdown()
        server.server_close()
