#!/bin/sh
# Runs after the package was removed.
#
# Configuration, the encryption key and the data stay in place, so that reinstalling
# brings LANPilot back as it was. Only "apt purge" deletes them; with rpm and Arch remove
# /etc/lanpilot and /var/lib/lanpilot by hand if they are no longer wanted.
set -e

if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
fi

if [ "${1:-}" = "purge" ]; then
    rm -rf /etc/lanpilot /var/lib/lanpilot
    if getent passwd lanpilot >/dev/null 2>&1; then
        userdel lanpilot >/dev/null 2>&1 || true
    fi
fi
