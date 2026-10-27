#!/usr/bin/env bash
# Atlassian MCP preflight — bounded session check that CANNOT hang.
#
# Why this exists: the Atlassian MCP is browser-backed (Playwright). When its
# session is missing/expired it blocks indefinitely with no timeout, so the
# agent freezes until the user cancels. This script tests the saved browser
# cookies against the Jira/Confluence REST API with a HARD curl --max-time
# bound, so it always returns in <~8s with a clear verdict.
#
# Usage:
#   preflight.sh jira        # check Jira session
#   preflight.sh confluence  # check Confluence session
#   preflight.sh both        # check both (default)
#
# Exit codes: 0 = all checked services GREEN; 1 = at least one RED.
# Agent contract: a fast GREEN/RED check before Atlassian calls. If RED, re-auth
# with the CLI: `atlassian-cli login <svc>`. (The MCP server no longer hangs on a
# missing session — it fails fast — but a GREEN check still saves a round-trip.)

set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
MAXTIME="${PREFLIGHT_MAXTIME:-8}"   # hard ceiling per request, seconds

# The browser auth writes per-service state files (.atlassian-browser-state-<svc>.json)
# AND/OR a shared legacy file (.atlassian-browser-state.json). Cookies for a host can
# live in any of them, so we gather candidate state files and try each until one's
# cookies authenticate. ATLASSIAN_STATE can pin a single file if set.
candidate_states() {
  local svc="$1"
  if [[ -n "${ATLASSIAN_STATE:-}" ]]; then echo "$ATLASSIAN_STATE"; return; fi
  # newest first so a fresh re-login wins over a stale shared file
  ls -t "$DIR/.atlassian-browser-state-$svc.json" \
        "$DIR/.atlassian-browser-state.json" \
        "$DIR/.atlassian-browser-state-confluence.json" \
        "$DIR/.atlassian-browser-state-jira.json" 2>/dev/null | awk '!seen[$0]++'
}

# Print a curl config (one `header = "Cookie: ..."` line) built from the cookies
# in a jar that a browser would send to $url: same host or a parent domain
# (dot boundary), not expired, and Secure ones only over https. The config goes
# to curl on stdin, so cookie values never appear in the process list.
cookie_config() {
  python3 - "$1" "$2" <<'PY'
import json, sys, time
from urllib.parse import urlparse
state, url = sys.argv[1], sys.argv[2]
parsed = urlparse(url)
host, https = (parsed.hostname or ""), parsed.scheme == "https"
try:
    cookies = json.load(open(state)).get("cookies", [])
except Exception:
    sys.exit(0)
now = time.time()
parts = []
for c in cookies:
    domain = (c.get("domain") or "").lstrip(".")
    if not domain or not (host == domain or host.endswith("." + domain)):
        continue
    expires = c.get("expires")
    if expires not in (None, -1, 0) and float(expires) < now:
        continue
    if c.get("secure") and not https:
        continue
    parts.append(f"{c['name']}={c['value']}")
if parts:
    value = "; ".join(parts).replace("\\", "\\\\").replace('"', '\\"')
    print(f'header = "Cookie: {value}"')
PY
}

check() {
  local svc="$1" url path upper
  # The base URL comes from JIRA_URL / CONFLUENCE_URL verbatim (scheme and any
  # context path such as /jira included); no hardcoded hostnames.
  case "$svc" in
    jira)       url="${JIRA_URL:-}";       path="/rest/api/2/myself" ;;
    confluence) url="${CONFLUENCE_URL:-}"; path="/rest/api/user/current" ;;
    *) echo "RED  $svc  (unknown service)"; return 1 ;;
  esac
  if [[ -z "$url" ]]; then
    upper="$(printf '%s' "$svc" | tr '[:lower:]' '[:upper:]')"
    echo "RED  $svc  (set ${upper}_URL env var)"; return 1
  fi
  url="${url%/}"

  local states; states="$(candidate_states "$svc")"

  local had_cookies=0 last_code=""
  local state config code body
  body="$(mktemp "${TMPDIR:-/tmp}/atl-preflight.XXXXXX")"
  while IFS= read -r state; do
    [[ -z "$state" ]] && continue
    [[ -f "$state" ]] || continue
    config="$(cookie_config "$state" "$url")"
    [[ -z "$config" ]] && continue
    had_cookies=1
    code="$(printf '%s\n' "$config" | curl -s -o "$body" -w '%{http_code}' \
                --max-time "$MAXTIME" --connect-timeout 5 \
                -K - -H "Accept: application/json" \
                "${url}${path}" 2>/dev/null)"
    # Confluence answers 200 with an anonymous user on instances that allow
    # anonymous access, so a 200 alone does not prove the session is live.
    if [[ "$code" == "200" && "$svc" == "confluence" ]] \
       && grep -Eq '"type"[[:space:]]*:[[:space:]]*"anonymous"' "$body"; then
      code="401"
    fi
    last_code="$code"
    if [[ "$code" == "200" ]]; then
      rm -f "$body"
      echo "GREEN $svc  (session valid; ${state##*/})"
      return 0
    fi
  done <<< "$states"
  rm -f "$body"

  # Saved jars are missing or stale. Before declaring RED, try harvesting a LIVE
  # session from any installed browser (bounded, opens no window). This is what
  # makes preflight self-healing: if the user's normal browser (Arc/Chrome/...)
  # has a live session, it is reused with no interactive login. Opt out with
  # ATLASSIAN_COOKIE_HARVEST=0.
  if [[ "${ATLASSIAN_COOKIE_HARVEST:-1}" != "0" && -f "$DIR/cookie_autoauth.py" ]]; then
    local py harvested
    py="$DIR/.venv-atlassian-browser/bin/python3"; [[ -x "$py" ]] || py="python3"
    harvested="$("$py" "$DIR/cookie_autoauth.py" "$svc" 2>/dev/null | grep -E '\-> HTTP 200' | head -1)"
    if [[ -n "$harvested" ]]; then
      echo "GREEN $svc  (harvested live session from ${harvested%%:*})"
      return 0
    fi
  fi

  if [[ "$had_cookies" == "0" ]]; then
    echo "RED  $svc  (no cookies for $url; no live browser session) — run: atlassian-cli login $svc"
    return 1
  fi
  # had cookies but none authenticated — classify by the last response code
  local code="$last_code"
  case "$code" in
    200) echo "GREEN $svc  (session valid)"; return 0 ;;
    000) echo "RED  $svc  (timeout/no-route after ${MAXTIME}s — VPN down?)"; return 1 ;;
    301|302) echo "RED  $svc  (HTTP $code → SSO redirect = session expired) — re-auth"; return 1 ;;
    401|403) echo "RED  $svc  (HTTP $code unauthorized) — re-auth"; return 1 ;;
    *) echo "RED  $svc  (HTTP $code unexpected)"; return 1 ;;
  esac
}

svc="${1:-both}"
rc=0
if [[ "$svc" == "both" ]]; then
  check jira || rc=1
  check confluence || rc=1
else
  check "$svc" || rc=1
fi
exit $rc
