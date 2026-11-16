"""The patched clients must keep every hardening step upstream's constructor applies.

Before this fix the wrapper rebuilt JiraClient/ConfluenceClient from scratch, so
upstream's SSRF redirect hook, DNS-rebinding pin, retry, concurrency cap, rate
limit and circuit breaker never ran. These tests build each client twice: once
through upstream's own constructor (the reference) and once through the patched
one, and require the patched session to carry the same hooks and adapters. A
wire test then proves cookies (not a token) go out and an SSRF redirect is
blocked.
"""

from __future__ import annotations

import pytest
from mcp_atlassian.confluence.client import ConfluenceClient
from mcp_atlassian.confluence.config import ConfluenceConfig
from mcp_atlassian.jira.client import JiraClient
from mcp_atlassian.jira.config import JiraConfig
from mcp_atlassian.utils.ssrf_adapter import SsrfPinningAdapter

import atlassian_browser_mcp_full as srv
from conftest import cookie, write_jar

# Turn on upstream's opt-in send wrappers so the test also sees them.
_HARDENING_ENV = {
    "ATLASSIAN_MAX_CONCURRENT_REQUESTS": "4",
    "ATLASSIAN_REQUESTS_PER_SECOND": "100",
    "ATLASSIAN_CIRCUIT_BREAKER_THRESHOLD": "50",
}

# The send wrappers upstream stacks on every adapter (0.24.6 and later), on top
# of the SsrfPinningAdapter's own send.
_HARDENING_LAYERS = (
    "send_with_default_timeout",
    "breaker_send",
    "rate_limited_send",
    "throttled_send",
)

_CASES = [
    ("jira", JiraClient, JiraConfig, "jira"),
    ("confluence", ConfluenceClient, ConfluenceConfig, "confluence"),
]


def _session(client, attr):
    return getattr(client, attr)._session


def _send_chain(adapter):
    """Name every layer of an adapter's send, outermost first.

    Upstream stacks its hardening by wrapping ``adapter.send`` again and again;
    each wrapper keeps the previous send in a closure variable named
    ``original_send``. Since 0.24.6 the default-timeout wrapper sits outermost,
    so the circuit breaker is no longer the top layer and has to be found
    further down.
    """
    chain = []
    send = adapter.send
    while send is not None:
        chain.append(getattr(send, "__qualname__", repr(send)))
        code = getattr(send, "__code__", None)
        if code is None or "original_send" not in code.co_freevars:
            break
        cell = send.__closure__[code.co_freevars.index("original_send")]
        send = cell.cell_contents
    return chain


def _shape(session):
    return {
        "hooks": [getattr(h, "__qualname__", repr(h)) for h in session.hooks["response"]],
        "adapters": {p: type(a).__name__ for p, a in session.adapters.items()},
        "sends": {p: _send_chain(a) for p, a in session.adapters.items()},
        "trust_env": session.trust_env,
    }


@pytest.mark.parametrize("service,client_cls,config_cls,attr", _CASES)
def test_patched_client_keeps_upstream_hardening(
    fake_atlassian, monkeypatch, service, client_cls, config_cls, attr
):
    base, _seen, tmp = fake_atlassian
    for k, v in _HARDENING_ENV.items():
        monkeypatch.setenv(k, v)
    write_jar(tmp / f"state-{service}.json", [cookie("JSESSIONID", "good", "127.0.0.1")])

    monkeypatch.setenv("ATLASSIAN_BROWSER_AUTH_ENABLED", "false")
    reference = _session(client_cls(srv._cookie_mode_config(config_cls.from_env())), attr)
    monkeypatch.setenv("ATLASSIAN_BROWSER_AUTH_ENABLED", "true")
    patched = _session(client_cls(config_cls.from_env()), attr)

    assert _shape(patched) == _shape(reference)
    hooks = _shape(patched)["hooks"]
    assert any("make_ssrf_redirect_hook" in h for h in hooks), hooks
    assert isinstance(patched.get_adapter(f"{base}/x"), SsrfPinningAdapter)
    assert isinstance(patched.get_adapter("https://example.com/x"), SsrfPinningAdapter)
    for prefix, adapter in patched.adapters.items():
        chain = _send_chain(adapter)
        layers = [layer.rsplit(".", 1)[-1] for layer in chain]
        for layer in _HARDENING_LAYERS:
            assert layer in layers, (prefix, chain)
        # The innermost send is the pinning adapter's own, so every layer
        # above still ends in the DNS-pinned transport.
        assert isinstance(adapter, SsrfPinningAdapter), (prefix, chain)
        assert chain[-1] == f"{type(adapter).__name__}.send", (prefix, chain)
    assert "Authorization" not in patched.headers
    assert patched.headers["X-Atlassian-Token"] == "no-check"
    assert patched.headers["Origin"] == base
    assert patched.cookies.get("JSESSIONID") == "good"


@pytest.mark.parametrize("service,client_cls,config_cls,attr", _CASES)
def test_cookie_request_on_the_wire_and_ssrf_redirect_blocked(
    fake_atlassian, service, client_cls, config_cls, attr
):
    base, seen, tmp = fake_atlassian
    write_jar(
        tmp / f"state-{service}.json",
        [cookie("JSESSIONID", "good", "127.0.0.1"), cookie("other", "leak", ".x.com")],
    )
    session = _session(client_cls(config_cls.from_env()), attr)

    resp = session.get(f"{base}/rest/api/2/myself", timeout=5)
    assert resp.status_code == 200
    sent = seen[-1]
    assert "JSESSIONID=good" in sent["cookie"]
    assert "leak" not in sent["cookie"]
    assert "authorization" not in sent
    assert sent["x-atlassian-token"] == "no-check"
    assert sent["origin"] == base

    # Upstream's redirect hook rejects a redirect to the metadata address
    # before any connection to it is made.
    with pytest.raises(ValueError, match="SSRF"):
        session.get(f"{base}/redirect", timeout=3)


def test_shared_global_config_is_not_mutated(fake_atlassian):
    _base, _seen, tmp = fake_atlassian
    write_jar(tmp / "state-jira.json", [cookie("JSESSIONID", "good", "127.0.0.1")])
    config = JiraConfig.from_env()
    token_before = config.personal_token
    client = JiraClient(config)
    assert config.personal_token == token_before == "BROWSER_SESSION"
    assert config.auth_type == "pat"
    assert client.config.auth_type == "external"
    assert client.config.personal_token is None
