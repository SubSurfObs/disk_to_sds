#!/usr/bin/env bash
#
# Mount the seiscomp staging SMB share from the command line.
#
# Replaces the ~30 s Finder "Connect to Server" dance. Uses macOS's `open`
# which routes through Finder's mounter -- so it picks up your Keychain
# credentials and lands the share at the SAME path Finder would
# (/Volumes/<share>/), keeping all scripts that hard-code that path happy.
#
# Idempotent: if the share is already mounted and reachable, exits 0
# immediately. If the mount exists but the connection has dropped (stale
# /Volumes entry, errno 57 on any read), kicks a remount.
#
# Usage:
#   scripts/mount_staging.sh                       # default staging share
#   scripts/mount_staging.sh <share-name>          # other share
#   SMB_USER=dsand SMB_SERVER=foo scripts/mount_staging.sh
#
# Returns 0 on success, non-zero if the mount didn't come up in 30 s
# (e.g. off-network -- this share requires VPN or being on UoM LAN).

set -euo pipefail

SHARE="${1:-proj-6700_seiscomp_staging-1128.4.1649}"
SERVER="${SMB_SERVER:-mediaflux.researchsoftware.unimelb.edu.au}"
USER="${SMB_USER:-dsand}"
MOUNT="/Volumes/${SHARE}"

# Already mounted and healthy?
if mount | grep -q "on ${MOUNT} "; then
    if /bin/ls "${MOUNT}" >/dev/null 2>&1; then
        echo "${MOUNT} already mounted and reachable."
        exit 0
    fi
    echo "${MOUNT} appears mounted but is unreachable; remounting."
fi

URL="smb://${USER}@${SERVER}/${SHARE}"
echo "Mounting ${URL}"
open "${URL}"

# `open` returns immediately; poll for the mount to appear + be listable.
for i in $(seq 1 30); do
    if /bin/ls "${MOUNT}" >/dev/null 2>&1; then
        echo "Mounted: ${MOUNT}"
        exit 0
    fi
    sleep 1
done

echo "ERROR: ${MOUNT} did not become reachable within 30 s." >&2
echo "Check: are you on UoM LAN or VPN? Are Keychain credentials saved?" >&2
exit 1
