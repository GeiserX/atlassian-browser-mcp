#!/usr/bin/env python3
"""Atlassian CLI — browser-cookie-backed Jira/Confluence access.

A thin, predictable command-line wrapper over the same browser-cookie auth
the MCP server uses (atlassian_browser_auth.create_browser_session). It talks
straight to the Jira/Confluence Server/DC REST APIs with the authenticated
requests.Session, so there is no MCP transport or upstream mcp-atlassian layer
to break.

Auth: reuses the saved Playwright storage state. If cookies are missing or
expired, run `login` (opens a browser for SSO/MFA) and retry.

Env (required, no defaults — same as the rest of this project):
  JIRA_URL         e.g. <your-jira-host>
  CONFLUENCE_URL   e.g. <your-confluence-host>

Examples:
  atlassian-cli login jira
  atlassian-cli jira get PROJ-123 --comments
  atlassian-cli jira search 'project = PROJ AND status = "In Progress"' --max 10
  atlassian-cli confluence get 123456789 --markdown -o page.md
  atlassian-cli confluence search 'release process' --space DEV
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# JIRA_URL / CONFLUENCE_URL are required env vars (no hardcoded defaults — see
# BrowserAuthConfig.from_env, which raises if they are unset). Set them in your
# shell or .mcp.json for your Atlassian instance.

from atlassian_browser_auth import (  # noqa: E402
    BrowserAuthConfig,
    create_browser_session,
    interactive_login,
)


def _eprint(*a: Any) -> None:
    print(*a, file=sys.stderr, flush=True)


def _base(service: str) -> str:
    cfg = BrowserAuthConfig.from_env()
    return cfg.service_base(service)


def _session(service: str):
    """Build an authenticated session for the service (jira|confluence)."""
    return create_browser_session(service, _base(service))


def _get_json(service: str, path: str, params: dict | None = None) -> Any:
    s = _session(service)
    # requests timeouts are in SECONDS: (connect, read).
    r = s.get(f"{_base(service)}{path}", params=params or {}, timeout=(10, 30))
    if r.status_code != 200:
        _eprint(f"HTTP {r.status_code} for {path}")
        # Don't dump the raw body: an error/redirect page can contain CSRF
        # tokens, an SSO login page, or internal hostnames. Show a hint instead.
        ctype = r.headers.get("Content-Type", "?")
        _eprint(
            f"(response {len(r.content)} bytes, content-type={ctype}; "
            f"if this looks like an SSO page, re-run: atlassian-cli login {service})"
        )
        sys.exit(2)
    return r.json()


# ---- HTML -> Markdown (best-effort, dependency-free) ----------------------
def _markdownify_fallback(html: str) -> str:
    """Crude tag-stripping fallback when the markdownify lib is unavailable."""
    import re
    from html import unescape

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def _html_to_markdown(html: str) -> str:
    try:
        from markdownify import markdownify as md  # type: ignore
    except ImportError:
        return _markdownify_fallback(html)
    try:
        return md(html, heading_style="ATX")
    except (ValueError, TypeError) as exc:
        _eprint(f"(markdownify failed: {exc}; using plain tag-strip fallback)")
        return _markdownify_fallback(html)


# ---- commands -------------------------------------------------------------
def cmd_login(args: argparse.Namespace) -> None:
    res = interactive_login(args.service)
    print(json.dumps(res, indent=2))


def cmd_jira_get(args: argparse.Namespace) -> None:
    fields = args.fields or "summary,status,assignee,labels,description,comment"
    params = {"fields": fields, "expand": "renderedFields"}
    data = _get_json("jira", f"/rest/api/2/issue/{args.key}", params)
    if args.raw:
        print(json.dumps(data, indent=2))
        return
    f = data.get("fields", {})
    print(f"{data.get('key')}: {f.get('summary')}")
    print(f"Status:   {(f.get('status') or {}).get('name')}")
    print(f"Assignee: {(f.get('assignee') or {}).get('displayName')}")
    print(f"Labels:   {f.get('labels')}")
    print("\n--- Description ---")
    print(f.get("description") or "(empty)")
    if args.comments:
        cs = (f.get("comment") or {}).get("comments", [])
        print(f"\n--- Comments ({len(cs)}) ---")
        for c in cs:
            who = (c.get("author") or {}).get("displayName", "?")
            print(f"\n[{who} @ {c.get('created','?')}]")
            print(c.get("body", ""))


def cmd_jira_search(args: argparse.Namespace) -> None:
    params = {"jql": args.jql, "maxResults": args.max,
              "fields": args.fields or "summary,status,assignee"}
    data = _get_json("jira", "/rest/api/2/search", params)
    if args.raw:
        print(json.dumps(data, indent=2))
        return
    for it in data.get("issues", []):
        f = it.get("fields", {})
        st = (f.get("status") or {}).get("name", "?")
        who = (f.get("assignee") or {}).get("displayName", "-")
        print(f"  {it['key']:14} [{st:14}] {who:22} {f.get('summary','')}")
    print(f"\n  total: {data.get('total')}")


def cmd_conf_get(args: argparse.Namespace) -> None:
    params = {"expand": "body.storage,version,space,ancestors"}
    data = _get_json("confluence", f"/rest/api/content/{args.page_id}", params)
    title = data.get("title", "")
    html = (((data.get("body") or {}).get("storage") or {}).get("value")) or ""
    ver = (data.get("version") or {}).get("number")
    space = (data.get("space") or {}).get("key")
    out = _html_to_markdown(html) if args.markdown else html
    header = f"# {title}\n\n> space={space} id={args.page_id} version={ver}\n\n"
    body = header + out
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(body)
        _eprint(f"wrote {len(body)} bytes to {args.out}  (v{ver}, space={space})")
    else:
        print(body)


def cmd_conf_search(args: argparse.Namespace) -> None:
    cql = args.cql
    if args.space and "space" not in cql.lower():
        cql = f'space = "{args.space}" AND text ~ "{args.cql}"'
    elif "~" not in cql and "=" not in cql:
        cql = f'text ~ "{args.cql}"'
    data = _get_json("confluence", "/rest/api/content/search",
                     {"cql": cql, "limit": args.limit})
    for r in data.get("results", []):
        print(f"  {r.get('id'):12} [{r.get('type')}] {r.get('title')}")
    print(f"\n  size: {data.get('size')}  cql: {cql}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atlassian-cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login", help="open browser for SSO login")
    pl.add_argument("service", choices=["jira", "confluence"], default="jira", nargs="?")
    pl.set_defaults(func=cmd_login)

    pj = sub.add_parser("jira", help="Jira commands").add_subparsers(dest="jcmd", required=True)
    g = pj.add_parser("get", help="get an issue")
    g.add_argument("key")
    g.add_argument("--comments", action="store_true")
    g.add_argument("--fields")
    g.add_argument("--raw", action="store_true")
    g.set_defaults(func=cmd_jira_get)
    s = pj.add_parser("search", help="JQL search")
    s.add_argument("jql")
    s.add_argument("--max", type=int, default=20)
    s.add_argument("--fields")
    s.add_argument("--raw", action="store_true")
    s.set_defaults(func=cmd_jira_search)

    pc = sub.add_parser("confluence", help="Confluence commands").add_subparsers(dest="ccmd", required=True)
    cg = pc.add_parser("get", help="get a page by id")
    cg.add_argument("page_id")
    cg.add_argument("--markdown", action="store_true")
    cg.add_argument("-o", "--out")
    cg.set_defaults(func=cmd_conf_get)
    cs = pc.add_parser("search", help="search pages (CQL or text)")
    cs.add_argument("cql")
    cs.add_argument("--space")
    cs.add_argument("--limit", type=int, default=10)
    cs.set_defaults(func=cmd_conf_search)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
