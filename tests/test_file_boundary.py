"""File tools stay inside a dedicated folder, whatever directory the client starts in.

Upstream confines content_file, attachment uploads and attachment download
targets to MCP_ATLASSIAN_FILES_DIR, or on stdio to the working directory. MCP
clients often start the server in "/" or the home directory, so the wrapper
must pin the folder itself. These tests drive upstream's real functions.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp_atlassian.confluence.attachments import AttachmentsMixin as ConfluenceAttachments
from mcp_atlassian.jira.attachments import AttachmentsMixin as JiraAttachments
from mcp_atlassian.servers.confluence import _resolve_page_content
from mcp_atlassian.utils.io import get_file_root

import atlassian_browser_mcp_full as srv

ROOT = Path(__file__).resolve().parent.parent
FILES_DIR_ENV = "MCP_ATLASSIAN_FILES_DIR"
URL = "https://jira.example.com/secure/attachment/1/a.txt"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    (home / "secret.txt").write_text("private key")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(FILES_DIR_ENV, raising=False)
    monkeypatch.setattr(srv.main_mcp, "run", lambda *a, **k: None)
    return home


def _start(monkeypatch, cwd: Path) -> Path:
    """Start the server the way the console script does, from ``cwd``."""
    monkeypatch.chdir(cwd)
    srv.main()
    return get_file_root()


@pytest.fixture
def files(home, monkeypatch):
    """The server started from "/", with one file inside the folder and two escapes."""
    _start(monkeypatch, Path("/"))
    root = home / ".atlassian-browser-mcp" / "files"
    root.mkdir(parents=True, exist_ok=True)
    (root / "page.md").write_text("inside")
    (root / "link.txt").symlink_to(home / "secret.txt")
    (root / "out").symlink_to(home)
    return root


class _Fake:
    """Stands in for a fetcher; records every upload and download that runs."""

    def __init__(self):
        self.calls: list[str] = []
        session = SimpleNamespace(get=self._get)
        self.jira = SimpleNamespace(_session=session, add_attachment_object=self._add)
        self.confluence = SimpleNamespace(_session=session)

    def _get(self, url, **_kw):
        self.calls.append(url)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            iter_content=lambda chunk_size: [b"data"],
            close=lambda: None,  # upstream closes the response since 0.24.4
        )

    def _add(self, _key, file):
        self.calls.append(file.name)
        return {"id": "1"}

    def _ensure_same_origin(self, _url):
        pass

    def _upload_attachment_direct(self, _content_id, file_path, *_args):
        self.calls.append(file_path)
        return {"id": "1"}


# Absolute path outside, ../ traversal, symlink inside the folder pointing out.
def _escapes(home: Path) -> list[str]:
    return [str(home / "secret.txt"), "../../secret.txt", "link.txt"]


@pytest.mark.parametrize("start_in", ["root", "home"])
def test_default_folder_is_dedicated_whatever_the_working_directory(home, monkeypatch, start_in):
    cwd = Path("/") if start_in == "root" else home
    root = _start(monkeypatch, cwd)
    assert root == home / ".atlassian-browser-mcp" / "files"
    assert root.is_dir()
    assert (root.stat().st_mode & 0o777) == 0o700
    assert (root.parent.stat().st_mode & 0o777) == 0o700


def test_content_file_is_confined(files, home):
    for path in _escapes(home):
        with pytest.raises(ValueError, match="resolves outside"):
            _resolve_page_content(None, path)
    assert _resolve_page_content(None, "page.md") == "inside"
    assert _resolve_page_content(None, str(files / "page.md")) == "inside"


@pytest.mark.parametrize("upload", [
    lambda fake, path: JiraAttachments.upload_attachment(fake, "PROJ-1", path),
    lambda fake, path: ConfluenceAttachments.upload_attachment(fake, "123", path),
], ids=["jira", "confluence"])
def test_upload_source_is_confined(files, home, upload):
    fake = _Fake()
    for path in _escapes(home):
        result = upload(fake, path)
        assert result["success"] is False and "outside" in result["error"], path
    assert fake.calls == []
    assert upload(fake, "page.md")["success"] is True
    assert fake.calls == [str(files / "page.md")]


@pytest.mark.parametrize("mixin", [JiraAttachments, ConfluenceAttachments], ids=["jira", "confluence"])
def test_download_target_is_confined(files, home, mixin, caplog):
    fake = _Fake()
    for target in (str(home / "evil.txt"), "../../evil.txt", "out/evil.txt"):
        caplog.clear()
        assert mixin.download_attachment(fake, URL, target) is False, target
        assert "resolves outside" in caplog.text, target
    assert not (home / "evil.txt").exists()
    assert fake.calls == []
    # A file straight in the folder root is fine: upstream's guard against the
    # working-directory root does not fire, because the folder is not the cwd.
    assert mixin.download_attachment(fake, URL, "a.txt") is True
    assert (files / "a.txt").read_bytes() == b"data"


def test_operator_folder_is_honoured(home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "page.md").write_text("from the workspace")
    monkeypatch.setenv(FILES_DIR_ENV, str(work))
    assert _start(monkeypatch, Path("/")) == work.resolve()
    assert _resolve_page_content(None, "page.md") == "from the workspace"
    with pytest.raises(ValueError, match="resolves outside"):
        _resolve_page_content(None, str(home / "secret.txt"))
    assert not (home / ".atlassian-browser-mcp").exists()


@pytest.mark.parametrize("value", ["/", "~", "~/..", "  "])
def test_root_or_home_is_refused_at_startup(home, monkeypatch, value):
    monkeypatch.setenv(FILES_DIR_ENV, value)
    if not value.strip():
        # Blank means unset: the dedicated default, never the working directory.
        assert _start(monkeypatch, Path("/")) == home / ".atlassian-browser-mcp" / "files"
        return
    with pytest.raises(RuntimeError, match=FILES_DIR_ENV):
        _start(monkeypatch, Path("/"))


def _other_spellings(home: Path) -> list[str]:
    """Names for home or a folder holding it that differ from it as strings."""
    names = [str(home).upper(), str(home.parent).swapcase()]
    firmlink = Path("/System/Volumes/Data")
    names += [str(firmlink) + str(home), str(firmlink)]
    return [n for n in names if os.path.exists(n) and n != str(home)]


def test_other_spellings_of_home_are_refused(home, monkeypatch):
    """A case variant (case-insensitive disk) or a firmlink names the same folder."""
    names = _other_spellings(home)
    if not names:
        pytest.skip("case-sensitive disk and no /System/Volumes/Data firmlink")
    for value in names:
        monkeypatch.setenv(FILES_DIR_ENV, value)
        with pytest.raises(RuntimeError, match=FILES_DIR_ENV):
            _start(monkeypatch, Path("/"))


def test_upstream_that_ignores_the_folder_is_refused(home, monkeypatch):
    """An upstream that still confines paths to the cwd (0.23.x) must not start."""
    monkeypatch.setattr(srv, "validate_safe_path", lambda p: (Path.cwd() / p).resolve())
    with pytest.raises(RuntimeError, match=FILES_DIR_ENV):
        _start(monkeypatch, Path("/"))


def test_unwritable_home_still_starts(home, monkeypatch, caplog):
    """Without a writable home the file tools fail; the server still starts."""
    home.chmod(0o555)
    try:
        root = _start(monkeypatch, Path("/"))
    finally:
        home.chmod(0o755)
    assert root == home / ".atlassian-browser-mcp" / "files"
    assert not root.exists()
    assert FILES_DIR_ENV in caplog.text


def test_boundary_is_set_on_import_before_main(home):
    """Importing the module is enough: no upstream tool can run with the cwd as root."""
    script = (
        "import atlassian_browser_mcp_full\n"
        "from mcp_atlassian.utils.io import get_file_root\n"
        "print(get_file_root())\n"
    )
    env = {k: v for k, v in os.environ.items() if k != FILES_DIR_ENV}
    out = subprocess.run(
        [sys.executable, "-c", script], cwd="/", env={**env, "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, timeout=180, check=True,
    )
    assert out.stdout.strip() == str(home / ".atlassian-browser-mcp" / "files")


def test_script_start_refuses_a_wide_folder(home):
    """`python atlassian_browser_mcp_full.py` runs the same startup check."""
    out = subprocess.run(
        [sys.executable, str(ROOT / "atlassian_browser_mcp_full.py")],
        cwd="/", env={**os.environ, FILES_DIR_ENV: "/"}, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=180,
    )
    assert out.returncode != 0
    assert FILES_DIR_ENV in out.stderr


def test_instructions_name_the_folder():
    assert os.environ[FILES_DIR_ENV] in srv.main_mcp.instructions
