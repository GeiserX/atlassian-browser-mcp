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

# Run a command with a time bound when a timeout binary exists. Stock macOS
# has none (GNU coreutils provides `timeout` or `gtimeout`); uv's own network
# timeouts still bound the install there.
bounded() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 600 "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout 600 "$@"
  else
    "$@"
  fi
}

# Check the package and its deps are importable, NOT an exact version.
# Pinning an exact version here meant every version bump forced a reinstall
# on startup (slow, network-dependent: a hang vector). We only install when
# something is actually missing/broken.
if [[ ! -x "${PYTHON_BIN}" ]] || ! "${PYTHON_BIN}" - <<'PY' >/dev/null 2>&1
import importlib.metadata as m
m.version("atlassian-browser-mcp")  # raises if not installed
import mcp_atlassian, playwright, requests  # noqa: F401
PY
then
  # Install exactly the versions in uv.lock (no fresh resolution at startup).
  UV_PROJECT_ENVIRONMENT="${VENV_DIR}" bounded uv sync --frozen --project "${ROOT_DIR}" >&2 \
    || { echo "atlassian-browser-mcp: dependency install failed or timed out" >&2; exit 1; }
fi

# No browser download here: the server never opens a browser. `atlassian-cli
# login` drives your installed Google Chrome by default. Only if you set
# ATLASSIAN_BROWSER_CHANNEL=chromium do you need:
#   .venv-atlassian-browser/bin/python -m playwright install chromium
# main() checks upstream compatibility before serving.
exec "${MCP_BIN}"
