"""The launcher reinstalls when uv.lock changes, and only then."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Fake uv: log the call and lay down a venv whose python and server both succeed.
FAKE_UV = r"""#!/bin/sh
echo sync >> "$FAKE_UV_LOG"
mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
printf '#!/bin/sh\ncat >/dev/null\nexit 0\n' > "$UV_PROJECT_ENVIRONMENT/bin/python"
printf '#!/bin/sh\nexit 0\n' > "$UV_PROJECT_ENVIRONMENT/bin/atlassian-browser-mcp"
chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python" "$UV_PROJECT_ENVIRONMENT/bin/atlassian-browser-mcp"
"""


def test_launcher_syncs_again_only_when_the_lock_changes(tmp_path):
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

    assert start() == 1  # fresh checkout installs
    assert start() == 1  # unchanged lock: no install
    (tmp_path / "uv.lock").write_text("version = 1\n# security bump\n")
    assert start() == 2  # changed lock installs again
