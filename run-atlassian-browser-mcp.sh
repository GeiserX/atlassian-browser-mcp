#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv-atlassian-browser"
PYTHON_BIN="${VENV_DIR}/bin/python"
MCP_BIN="${VENV_DIR}/bin/atlassian-browser-mcp"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required but not installed." >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  uv venv "${VENV_DIR}"
fi

if ! "${PYTHON_BIN}" - <<'PY' >/dev/null 2>&1
from importlib.metadata import version
assert version("atlassian-browser-mcp") == "1.0.0"
import mcp_atlassian
import playwright
import requests
PY
then
  uv pip install --python "${PYTHON_BIN}" -e "${ROOT_DIR}"
fi

"${PYTHON_BIN}" -m playwright install chromium >/dev/null

# Startup compatibility assertion: verify the upstream version and patched signatures
"${PYTHON_BIN}" - <<'PY'
from atlassian_browser_mcp_full import assert_upstream_compatibility
assert_upstream_compatibility()
PY

exec "${MCP_BIN}"
