"""The launcher reinstalls when uv.lock changes, and only then."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Fake uv: log the call and lay down a venv whose python and server both succeed.
# Like real uv, a sync of an already-installed project leaves a deleted console
# script missing unless the project is reinstalled.
FAKE_UV = r"""#!/bin/sh
echo sync >> "$FAKE_UV_LOG"
fresh=0
[ -d "$UV_PROJECT_ENVIRONMENT/bin" ] || fresh=1
case " $* " in *" --reinstall-package atlassian-browser-mcp "*) fresh=1 ;; esac
mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
printf '#!/bin/sh\ncat >/dev/null\nexit 0\n' > "$UV_PROJECT_ENVIRONMENT/bin/python"
chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python"
if [ "$fresh" = 1 ]; then
  printf '#!/bin/sh\nexit 0\n' > "$UV_PROJECT_ENVIRONMENT/bin/atlassian-browser-mcp"
  chmod +x "$UV_PROJECT_ENVIRONMENT/bin/atlassian-browser-mcp"
fi
"""


def _launcher(tmp_path):
    shutil.copy(ROOT / "run-atlassian-browser-mcp.sh", tmp_path)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "uv").write_text(FAKE_UV)
    (bindir / "uv").chmod(0o755)
    log = tmp_path / "uv.log"
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin", "FAKE_UV_LOG": str(log)}

    def start() -> int:
        subprocess.run(
            ["/bin/bash", str(tmp_path / "run-atlassian-browser-mcp.sh")],
            env=env, check=True, capture_output=True, timeout=30,
        )
        return len(log.read_text().splitlines()) if log.exists() else 0

    return start


def test_launcher_syncs_again_only_when_the_lock_changes(tmp_path):
    start = _launcher(tmp_path)
    assert start() == 1  # fresh checkout installs
    assert start() == 1  # unchanged lock: no install
    (tmp_path / "uv.lock").write_text("version = 1\n# security bump\n")
    assert start() == 2  # changed lock installs again


def test_launcher_restores_a_deleted_server_script(tmp_path):
    start = _launcher(tmp_path)
    assert start() == 1
    (tmp_path / ".venv-atlassian-browser" / "bin" / "atlassian-browser-mcp").unlink()
    # Same lock, imports still fine: only the missing script triggers the repair,
    # and the repair must reinstall the project so the script comes back.
    assert start() == 2
    assert start() == 2
