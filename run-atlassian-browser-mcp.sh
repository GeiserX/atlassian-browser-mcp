#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv-atlassian-browser"
PYTHON_BIN="${VENV_DIR}/bin/python"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required but not installed." >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  uv venv "${VENV_DIR}"
fi

if ! "${PYTHON_BIN}" - <<'PY' >/dev/null 2>&1
import mcp_atlassian
import playwright
import requests
PY
then
  uv pip install --python "${PYTHON_BIN}" "mcp-atlassian~=0.21.0" "playwright>=1.40" "requests>=2.31"
fi

"${PYTHON_BIN}" -m playwright install chromium >/dev/null

# Startup compatibility assertion: verify the patched classes exist
"${PYTHON_BIN}" - <<'PY'
from mcp_atlassian.confluence.client import ConfluenceClient
from mcp_atlassian.jira.client import JiraClient
from mcp_atlassian.jira.users import UsersMixin
from mcp_atlassian.jira.forms_api import FormsApiMixin
assert hasattr(JiraClient, '__init__'), "JiraClient.__init__ missing"
assert hasattr(ConfluenceClient, '__init__'), "ConfluenceClient.__init__ missing"
assert hasattr(UsersMixin, '_lookup_user_by_permissions'), "UsersMixin._lookup_user_by_permissions missing"
assert hasattr(FormsApiMixin, '_make_forms_api_request'), "FormsApiMixin._make_forms_api_request missing"
PY

exec "${PYTHON_BIN}" "${ROOT_DIR}/atlassian_browser_mcp_full.py"
