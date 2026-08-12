#!/usr/bin/env bash
# seed_toolcache_python.sh — seed a python-build-standalone CPython into a GitHub
# Actions self-hosted runner tool cache, in the exact layout setup-python@v5 expects
# (T-2733 floor-bump tail; the T-2219 3.10/3.12 seed was an undocumented on-box op).
#
# Usage: seed_toolcache_python.sh <minor>   e.g. seed_toolcache_python.sh 3.11
# Idempotent-ish: refuses if the version dir already has a .complete marker.
set -euo pipefail

MINOR="${1:?usage: seed_toolcache_python.sh <minor e.g. 3.11>}"
CACHE="/opt/hostedtoolcache/Python"
ARCH="x64"                       # setup-python's arch dir name on linux x64
ASSET_ARCH="x86_64-unknown-linux-gnu"
RUNNER_USER="runner"

echo "[seed] resolving latest python-build-standalone ${MINOR}.x install_only (${ASSET_ARCH})..."
# Parse the latest release JSON for the matching install_only asset (NOT _stripped, NOT debug).
URL="$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest \
  | python3 -c "
import json,sys,re
d=json.load(sys.stdin)
pat=re.compile(r'^cpython-(${MINOR//./\\.}\.\d+)\+\d+-${ASSET_ARCH}-install_only\.tar\.gz$')
best=None
for a in d.get('assets',[]):
    m=pat.match(a['name'])
    if m:
        best=(m.group(1), a['browser_download_url'])
        break
if not best:
    sys.exit('no ${MINOR}.x install_only ${ASSET_ARCH} asset in latest release')
print(best[0]); print(best[1])
")"
FULLVER="$(echo "$URL" | sed -n 1p)"
DLURL="$(echo "$URL" | sed -n 2p)"
echo "[seed] version=${FULLVER}  url=${DLURL}"

DEST="${CACHE}/${FULLVER}"
if [[ -f "${DEST}/${ARCH}.complete" ]]; then
  echo "[seed] ${DEST}/${ARCH}.complete already present — nothing to do (idempotent)."
  "${DEST}/${ARCH}/bin/python3" --version
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "[seed] downloading..."
curl -fsSL "$DLURL" -o "$TMP/py.tar.gz"
echo "[seed] extracting..."
tar -xzf "$TMP/py.tar.gz" -C "$TMP"       # install_only extracts to $TMP/python/{bin,lib,...}
test -x "$TMP/python/bin/python3" || { echo "[seed] FATAL: extracted tree has no python/bin/python3"; exit 1; }

echo "[seed] placing into ${DEST}/${ARCH}/ ..."
mkdir -p "${DEST}"
rm -rf "${DEST}/${ARCH}"
mv "$TMP/python" "${DEST}/${ARCH}"
touch "${DEST}/${ARCH}.complete"
chown -R "${RUNNER_USER}:${RUNNER_USER}" "${DEST}"

echo "[seed] verifying..."
"${DEST}/${ARCH}/bin/python3" --version
echo "[seed] OK: seeded ${FULLVER} at ${DEST} (marker + chown ${RUNNER_USER} done)."
