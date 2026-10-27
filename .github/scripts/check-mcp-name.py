#!/usr/bin/env python3
"""check-mcp-name.py <sdist.tar.gz> <server.json>: exit 0 when the sdist's
PKG-INFO (the description PyPI shows) carries the "mcp-name: <name>" token the
MCP Registry reads to prove the package belongs to this server, 1 otherwise.
The token needs a boundary after the name: whitespace, a tag or "-->"."""
import json
import re
import sys
import tarfile

sdist, server_json = sys.argv[1], sys.argv[2]
with open(server_json, encoding="utf-8") as fh:
    name = json.load(fh)["name"]
with tarfile.open(sdist) as tar:
    member = next(m for m in tar.getmembers() if m.name.endswith("/PKG-INFO"))
    pkg_info = tar.extractfile(member).read().decode("utf-8")
if re.search(r"mcp-name: " + re.escape(name) + r"(?=\s|<|-->)", pkg_info):
    print(f"PKG-INFO carries mcp-name: {name}")
    sys.exit(0)
print(f"::error::README.md has no 'mcp-name: {name}' token; the registry would refuse the package", file=sys.stderr)
sys.exit(1)
