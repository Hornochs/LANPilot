#!/bin/sh
# Runs before the package is removed. On an upgrade the service keeps running; the
# post-install script of the new version restarts it.
#
# No "exit" in this file: some package formats embed the script in a function, where an
# exit would end the whole removal.
set -e

# deb passes "upgrade", rpm passes 1 when this is an upgrade rather than a removal.
if [ "${1:-}" != "upgrade" ] && [ "${1:-}" != "1" ] && [ -d /run/systemd/system ]; then
    systemctl disable --now lanpilot.service >/dev/null 2>&1 || true
fi
