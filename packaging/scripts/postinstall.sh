#!/bin/sh
# Runs after the package was installed or upgraded (deb, rpm and Arch).
# Everything here is safe to run again: nothing that exists is replaced.
set -e

CONFIG_DIR=/etc/lanpilot
KEY_FILE="$CONFIG_DIR/secret.key"
STATE_DIR=/var/lib/lanpilot

# Service user.
if ! getent passwd lanpilot >/dev/null 2>&1; then
    if command -v systemd-sysusers >/dev/null 2>&1; then
        systemd-sysusers /usr/lib/sysusers.d/lanpilot.conf
    else
        useradd --system --user-group --home-dir "$STATE_DIR" --shell /usr/sbin/nologin \
            --comment "LANPilot game server control panel" lanpilot
    fi
fi

# Configuration: readable by the service, writable by root only.
install -d -m 0750 -o root -g lanpilot "$CONFIG_DIR"
if [ -f "$CONFIG_DIR/lanpilot.toml" ]; then
    chown root:lanpilot "$CONFIG_DIR/lanpilot.toml"
    chmod 0640 "$CONFIG_DIR/lanpilot.toml"
fi

# Encryption key for the game servers' passwords. Created once; an existing key is never
# replaced, because the stored passwords can only be read with it.
first_install=no
if [ ! -e "$KEY_FILE" ]; then
    first_install=yes
    # No bytecode files: nothing may be written into /opt/lanpilot outside the package.
    PYTHONDONTWRITEBYTECODE=1 /opt/lanpilot/bin/lanpilot secret generate-key \
        --output "$KEY_FILE" >/dev/null
fi
chown lanpilot:lanpilot "$KEY_FILE"
chmod 0600 "$KEY_FILE"

install -d -m 0700 -o lanpilot -g lanpilot "$STATE_DIR"

if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
    # Restarts the service after an upgrade; does nothing if it is not running.
    systemctl try-restart lanpilot.service || true
fi

if [ "$first_install" = yes ]; then
    cat <<'MESSAGE'

LANPilot is installed. To get it running:

  1. Set public_url in /etc/lanpilot/lanpilot.toml to the address users will open.
  2. Create the first administrator:   lanpilot user create admin
  3. Start the service:                systemctl enable --now lanpilot

The key that protects the stored game server passwords is /etc/lanpilot/secret.key.
Keep a copy of it together with your backups of /var/lib/lanpilot.

MESSAGE
fi
