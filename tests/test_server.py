"""The server over MCP: tool list, metadata, wrapper tools and startup guards.

These replace a protocol smoke test. The server cannot speak MCP 2026-07-28
until upstream mcp-atlassian moves off mcp<2 / fastmcp<4, so the checks here
run a real initialize + tools/list + tools/call through FastMCP's in-memory
client against the same server object main() runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import version as package_version
from pathlib import Path

import anyio
import pytest
from fastmcp import Client

import atlassian_browser_mcp_full as srv
from atlassian_browser_auth import BrowserAuthConfig
from conftest import cookie, write_jar

ROOT = Path(__file__).resolve().parent.parent
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def _session(action):
    async def go():
        async with Client(srv.main_mcp) as client:
            return await action(client)

    return anyio.run(go)


def _list():
    async def action(client):
        return client.initialize_result, await client.list_tools()

    return _session(action)


def _call(name, args):
    async def action(client):
        return await client.call_tool(name, args, raise_on_error=False)

    return _session(action)


def test_server_identity_and_instructions():
    init, _tools = _list()
    assert init.serverInfo.name == "atlassian-browser-mcp"
    assert init.serverInfo.version == package_version("atlassian-browser-mcp")
    assert init.instructions and "atlassian_auth_status" in init.instructions
    assert init.protocolVersion == "2025-11-25"


def test_every_tool_has_a_title_and_all_four_hints():
    _init, tools = _list()
    assert len(tools) > 90
    missing = []
    for tool in tools:
        ann = tool.annotations
        title = tool.title or (ann.title if ann else None)
        unset = [h for h in HINTS if ann is None or getattr(ann, h) is None]
        if not title or unset:
            missing.append((tool.name, title, unset))
    assert not missing, missing


def test_hints_match_what_each_tool_does():
    _init, tools = _list()
    by_name = {t.name: t.annotations for t in tools}
    assert by_name["jira_get_issue"].readOnlyHint is True
    assert by_name["jira_get_issue"].destructiveHint is False
    assert by_name["jira_delete_issue"].destructiveHint is True
    assert by_name["jira_add_comment"].readOnlyHint is False
    assert by_name["jira_add_comment"].destructiveHint is False
    assert by_name["jira_add_comment"].idempotentHint is False
    assert by_name["jira_assign_issue"].destructiveHint is True
    assert by_name["confluence_create_page"].destructiveHint is False
    assert by_name["atlassian_auth_status"].readOnlyHint is True
    assert by_name["atlassian_auth_status"].openWorldHint is False


def test_wrapper_tools_are_listed_first_with_output_schemas():
    _init, tools = _list()
    names = [t.name for t in tools]
    assert names[:2] == ["atlassian_auth_status", "atlassian_login"]
    for tool in tools[:2]:
        assert tool.outputSchema and tool.outputSchema.get("type") == "object"
    login = next(t for t in tools if t.name == "atlassian_login")
    assert set(login.inputSchema["properties"]) == {"target"}


def test_tool_list_is_identical_across_hash_seeds():
    """Upstream's default Jira field list comes from a set; it must not reorder."""
    script = (
        "import json, anyio\n"
        "from fastmcp import Client\n"
        "import atlassian_browser_mcp_full as srv\n"
        "async def go():\n"
        "    async with Client(srv.main_mcp) as c:\n"
        "        tools = await c.list_tools()\n"
        "    print(json.dumps([t.model_dump(mode='json') for t in tools], sort_keys=True))\n"
        "anyio.run(go)\n"
    )
    outputs = []
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=180, check=True,
        )
        outputs.append(out.stdout)
    assert outputs[0] == outputs[1] == outputs[2]


def test_login_tool_returns_structured_instructions():
    result = _call("atlassian_login", {"target": "confluence"})
    assert not result.is_error
    data = result.structured_content
    assert data["status"] == "action_required"
    assert data["command"].endswith("login confluence")
    assert "login confluence" in result.content[0].text


def test_auth_status_reports_a_live_session(fake_atlassian):
    _base, seen, tmp = fake_atlassian
    write_jar(tmp / "state-jira.json", [cookie("JSESSIONID", "good", "127.0.0.1")])
    result = _call("atlassian_auth_status", {"service": "jira"})
    assert not result.is_error, result.content
    (jira,) = result.structured_content["services"]
    assert jira["jar_exists"] and jira["live"] and jira["ready"]
    assert jira["http_status"] == 200
    assert jira["cookie_count"] == 1
    assert seen and "authorization" not in seen[-1]


def test_auth_status_catches_an_anonymous_confluence_session(fake_atlassian):
    _base, _seen, tmp = fake_atlassian
    write_jar(tmp / "state-confluence.json", [cookie("JSESSIONID", "expired", "127.0.0.1")])
    result = _call("atlassian_auth_status", {"service": "confluence"})
    (conf,) = result.structured_content["services"]
    assert conf["live"] is False
    assert "login confluence" in conf["next_step"]


def test_auth_status_without_a_jar_says_how_to_log_in(fake_atlassian):
    result = _call("atlassian_auth_status", {"service": "both", "check_live": True})
    services = {s["service"]: s for s in result.structured_content["services"]}
    assert set(services) == {"jira", "confluence"}
    assert not services["jira"]["jar_exists"]
    assert services["jira"]["live"] is None
    assert "login jira" in services["jira"]["next_step"]


# ---- startup guards ------------------------------------------------------------
def test_main_refuses_any_transport_but_stdio(monkeypatch):
    calls = []
    monkeypatch.setattr(srv.main_mcp, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setenv("FASTMCP_TRANSPORT", "http")
    with pytest.raises(RuntimeError, match="stdio only"):
        srv.main()
    assert calls == []
    monkeypatch.delenv("FASTMCP_TRANSPORT")
    srv.main()
    assert calls == [((), {"transport": "stdio"})]


def test_a_jira_only_deployment_starts(monkeypatch):
    monkeypatch.delenv("CONFLUENCE_URL", raising=False)
    calls = []
    monkeypatch.setattr(srv.main_mcp, "run", lambda *a, **k: calls.append(k))
    srv.main()
    assert calls
    cfg = BrowserAuthConfig.from_env("jira")
    assert cfg.jira_url and not cfg.confluence_url
    with pytest.raises(RuntimeError, match="CONFLUENCE_URL"):
        BrowserAuthConfig.from_env("confluence")


def test_placeholder_token_only_in_browser_mode():
    script = "import os, atlassian_browser_mcp_full; print(os.environ.get('JIRA_PERSONAL_TOKEN'))"
    base_env = {k: v for k, v in os.environ.items() if not k.endswith("_PERSONAL_TOKEN")}
    off = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=180,
        env={**base_env, "ATLASSIAN_BROWSER_AUTH_ENABLED": "false"}, check=True,
    )
    on = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=180,
        env={**base_env, "ATLASSIAN_BROWSER_AUTH_ENABLED": "true"}, check=True,
    )
    assert off.stdout.strip() == "None"
    assert on.stdout.strip() == "BROWSER_SESSION"
