"""preflight.sh sends only the instance's cookies, and never on curl's command line."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE_CURL = r"""#!/bin/sh
# Record argv and stdin, write a body to the -o file, print the status code.
log="$FAKE_CURL_LOG"
printf '%s\n' "$@" > "$log.argv"
cat > "$log.stdin"
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift; fi
  shift
done
[ -n "$out" ] && printf '%s' "$FAKE_CURL_BODY" > "$out"
printf '%s' "${FAKE_CURL_CODE:-200}"
"""


def _run(tmp_path: Path, svc: str, url: str, body: str = "{}"):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    curl = bindir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)
    jar = tmp_path / "jar.json"
    jar.write_text(json.dumps({"cookies": [
        {"name": "JSESSIONID", "value": "good", "domain": "jira.acmex.com", "path": "/"},
        {"name": "sso", "value": "corp", "domain": ".acmex.com", "path": "/"},
        {"name": "xcom", "value": "leak1", "domain": ".x.com", "path": "/"},
        {"name": "excom", "value": "leak2", "domain": "ex.com", "path": "/"},
        {"name": "old", "value": "leak3", "domain": "jira.acmex.com", "path": "/", "expires": 1000},
    ]}))
    log = tmp_path / "curl"
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("JIRA_URL", "CONFLUENCE_URL")},
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "ATLASSIAN_STATE": str(jar),
        "ATLASSIAN_COOKIE_HARVEST": "0",
        "FAKE_CURL_LOG": str(log),
        "FAKE_CURL_BODY": body,
    }
    if url:
        env["JIRA_URL" if svc == "jira" else "CONFLUENCE_URL"] = url
    proc = subprocess.run(
        ["/bin/bash", str(ROOT / "preflight.sh"), svc],
        env=env, capture_output=True, text=True, timeout=60,
    )
    argv = log.with_suffix(".argv").read_text() if log.with_suffix(".argv").exists() else ""
    stdin = log.with_suffix(".stdin").read_text() if log.with_suffix(".stdin").exists() else ""
    return proc, argv, stdin


def test_only_this_hosts_cookies_and_never_on_argv(tmp_path):
    proc, argv, stdin = _run(tmp_path, "jira", "https://jira.acmex.com/jira/")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "GREEN jira" in proc.stdout
    assert "https://jira.acmex.com/jira/rest/api/2/myself" in argv.splitlines()
    assert "good" not in argv and "Cookie" not in argv
    assert "JSESSIONID=good" in stdin and "sso=corp" in stdin
    for leaked in ("leak1", "leak2", "leak3"):
        assert leaked not in stdin


def test_anonymous_confluence_user_is_red(tmp_path):
    proc, _argv, _stdin = _run(
        tmp_path, "confluence", "https://jira.acmex.com", body='{"type": "anonymous"}'
    )
    assert proc.returncode == 1
    assert "RED  confluence" in proc.stdout


def test_missing_url_message_works_on_any_bash(tmp_path):
    proc, _argv, _stdin = _run(tmp_path, "jira", "")
    assert proc.returncode == 1
    assert "RED  jira  (set JIRA_URL env var)" in proc.stdout
