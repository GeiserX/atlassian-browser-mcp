#!/usr/bin/env sh
# wait-for-pypi.sh <package> <version>: exit 0 once PyPI's JSON API serves that
# exact version, 1 after 15 minutes. PyPI publishes within seconds, but the
# registry asks PyPI at once and a 404 there fails the publish for good.
set -eu
pkg="$1"; ver="$2"
i=0
while [ "$i" -lt 90 ]; do
  if curl -fsS "https://pypi.org/pypi/${pkg}/${ver}/json" >/dev/null 2>&1; then
    echo "pypi serves ${pkg} ${ver}"
    exit 0
  fi
  i=$((i + 1))
  sleep 10
done
echo "pypi still does not serve ${pkg} ${ver} after 15 minutes" >&2
exit 1
