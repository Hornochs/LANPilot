#!/bin/sh
# Command line entry point of the LANPilot package.
#
# When called by root, the command runs as the service user. Otherwise files it creates,
# such as the database on first use, would belong to root and the service could not write
# to them.
export LANPILOT_CONFIG="${LANPILOT_CONFIG:-/etc/lanpilot/lanpilot.toml}"

if [ "$(id -u)" = "0" ]; then
    # A clean environment: nothing of root's (PYTHONPATH, PYTHONSTARTUP and the like) may
    # change what runs as the service user. Settings come from the configuration file.
    exec runuser -u lanpilot -- env -i \
        PATH=/usr/sbin:/usr/bin:/sbin:/bin \
        HOME=/var/lib/lanpilot \
        LANG="${LANG:-C.UTF-8}" \
        LANPILOT_CONFIG="$LANPILOT_CONFIG" \
        /opt/lanpilot/bin/lanpilot "$@"
fi
exec /opt/lanpilot/bin/lanpilot "$@"
