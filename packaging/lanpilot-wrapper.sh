#!/bin/sh
# Command line entry point of the LANPilot package.
#
# When called by root, the command runs as the service user. Otherwise files it creates,
# such as the database on first use, would belong to root and the service could not write
# to them.
export LANPILOT_CONFIG="${LANPILOT_CONFIG:-/etc/lanpilot/lanpilot.toml}"

if [ "$(id -u)" = "0" ]; then
    exec runuser -u lanpilot -- /opt/lanpilot/bin/lanpilot "$@"
fi
exec /opt/lanpilot/bin/lanpilot "$@"
