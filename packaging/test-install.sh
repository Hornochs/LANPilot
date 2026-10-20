#!/bin/sh
# Install test for a LANPilot package. Runs as root in a throw-away container.
#
#   packaging/test-install.sh <deb|rpm|archlinux> <package> [<older package>]
#
# With an older package given, that one is installed first and then upgraded, which is
# the case that matters most: after an upgrade the new code must be in place and the key,
# the configuration and the data must be untouched.
set -eu

FORMAT="${1:?usage: test-install.sh <deb|rpm|archlinux> <package> [<older package>]}"
PACKAGE="${2:?usage: test-install.sh <deb|rpm|archlinux> <package> [<older package>]}"
OLDER="${3:-}"
URL=http://127.0.0.1:9030

fail() {
    echo "FAILED: $*" >&2
    exit 1
}

install_package() {
    case "$FORMAT" in
        # The test edits lanpilot.toml; when a newer package ships a changed one, dpkg asks
        # which to keep. Keep the edited one, as an administrator would.
        deb) apt-get install -y --no-install-recommends \
            -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "$1" ;;
        rpm) dnf install -y "$1" ;;
        archlinux) pacman -U --noconfirm "$1" ;;
        *) fail "unknown format $FORMAT" ;;
    esac
}

remove_package() {
    case "$FORMAT" in
        deb) apt-get remove -y lanpilot ;;
        rpm) dnf remove -y lanpilot ;;
        archlinux) pacman -R --noconfirm lanpilot ;;
    esac
}

packaged_version() {
    /opt/lanpilot/bin/python -c 'from importlib.metadata import version; print(version("lanpilot"))'
}

check_layout() {
    getent passwd lanpilot >/dev/null || fail "service user missing"
    [ "$(stat -c '%U:%G %a' /etc/lanpilot/secret.key)" = "lanpilot:lanpilot 600" ] ||
        fail "secret.key has the wrong owner or mode: $(stat -c '%U:%G %a' /etc/lanpilot/secret.key)"
    [ "$(stat -c '%U:%G %a' /etc/lanpilot)" = "root:lanpilot 750" ] ||
        fail "/etc/lanpilot has the wrong owner or mode"
    [ "$(stat -c '%U %a' /var/lib/lanpilot)" = "lanpilot 700" ] ||
        fail "/var/lib/lanpilot has the wrong owner or mode"
    [ -z "$(find /opt/lanpilot -not -user root -print -quit)" ] ||
        fail "files in /opt/lanpilot do not belong to root"
    [ -z "$(find /opt/lanpilot -perm /022 -not -type l -print -quit)" ] ||
        fail "files in /opt/lanpilot are writable by group or others"
    [ "$(lanpilot --version)" = "lanpilot $(packaged_version)" ] || fail "lanpilot --version is wrong"
}

start_service() {
    # Containers have no systemd; run the service the way the unit does.
    runuser -u lanpilot -- env LANPILOT_CONFIG=/etc/lanpilot/lanpilot.toml \
        /opt/lanpilot/bin/lanpilot serve >/tmp/lanpilot.log 2>&1 &
    SERVICE_PID=$!
    for _ in $(seq 1 30); do
        if curl -fsS "$URL/healthz" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    cat /tmp/lanpilot.log >&2
    fail "the service did not answer on $URL/healthz"
}

stop_service() {
    kill "$SERVICE_PID"
    wait "$SERVICE_PID" 2>/dev/null || true
}

database() {
    # The service's own interpreter; its sqlite3 module is all that is needed.
    runuser -u lanpilot -- /opt/lanpilot/bin/python -c "
import sqlite3, sys
db = sqlite3.connect('/var/lib/lanpilot/lanpilot.db')
for row in db.execute(sys.argv[1]):
    print(*row, sep='|')
db.commit()
" "$1"
}

sign_in() {
    curl -s -o /dev/null -w '%{http_code}' -H "Origin: $URL" \
        --data-urlencode username=admin --data-urlencode "password=correct horse battery" \
        "$URL/login"
}

echo "== install $(basename "${OLDER:-$PACKAGE}")"
install_package "${OLDER:-$PACKAGE}"
check_layout
FIRST_VERSION="$(packaged_version)"

# A fresh installation refuses to start until public_url is set.
if lanpilot config show >/dev/null 2>&1; then
    fail "the default configuration is accepted although public_url is missing"
fi
sed -i 's|^#public_url = ""|public_url = "http://127.0.0.1:9030"|' /etc/lanpilot/lanpilot.toml
lanpilot config show | grep -q '"public_url": "http://127.0.0.1:9030"' || fail "public_url was not applied"

echo "correct horse battery" | lanpilot user create admin --password-stdin
[ "$(stat -c '%U' /var/lib/lanpilot/lanpilot.db)" = "lanpilot" ] ||
    fail "the database created through the command line does not belong to the service user"

# Data the upgrade has to carry over, in the schema the first version created (servers
# and their map lists exist since its first release).
database "INSERT INTO servers (name, adapter_id, host, port, settings, credentials, enabled,
    created_at, updated_at) VALUES ('Upgrade test', 'demo', 'demo.lan', 27015, '{}', NULL, 1,
    '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
database "INSERT INTO server_maps (server_id, key, name, game_type, source, extra, updated_at)
    VALUES (1, 'de_dust2', NULL, '', 'protocol', '{}', '2026-01-01 00:00:00')"
SCHEMA_BEFORE="$(lanpilot db current 2>/dev/null || true)"

start_service
curl -fsS "$URL/login" | grep -q "Sign in" || fail "the login page is not served"
[ "$(sign_in)" = 303 ] || fail "signing in does not work"
curl -fsS "$URL/static/vendor/htmx/htmx.min.js" >/dev/null || fail "static files are missing"
stop_service

KEY_BEFORE="$(sha256sum /etc/lanpilot/secret.key)"
CONFIG_BEFORE="$(sha256sum /etc/lanpilot/lanpilot.toml)"

if [ -n "$OLDER" ]; then
    echo "== upgrade to $(basename "$PACKAGE")"
    install_package "$PACKAGE"
    check_layout
    NEW_VERSION="$(packaged_version)"
    [ "$NEW_VERSION" != "$FIRST_VERSION" ] || fail "the upgrade left version $FIRST_VERSION in place"
    # Nothing of the old version may be left behind in the environment.
    LEFTOVER="$(find /opt/lanpilot -name "lanpilot-$FIRST_VERSION.dist-info" -print -quit)"
    [ -z "$LEFTOVER" ] || fail "files of the old version remain: $LEFTOVER"
    [ "$(sha256sum /etc/lanpilot/secret.key)" = "$KEY_BEFORE" ] || fail "the upgrade replaced the key"
    [ "$(sha256sum /etc/lanpilot/lanpilot.toml)" = "$CONFIG_BEFORE" ] ||
        fail "the upgrade replaced the edited configuration"
    start_service
    lanpilot user list | grep -q '^admin ' || fail "the user is gone after the upgrade"
    lanpilot db current || fail "the database is not at the newest schema after the upgrade"
    [ "$(sign_in)" = 303 ] || fail "signing in does not work after the upgrade"
    [ "$(database "SELECT name, host FROM servers")" = "Upgrade test|demo.lan" ] ||
        fail "the server entry did not survive the upgrade"
    [ "$(database "SELECT key FROM server_maps")" = "de_dust2" ] ||
        fail "the map list did not survive the upgrade"
    echo "schema before the upgrade: $SCHEMA_BEFORE"
    stop_service
fi

echo "== reinstall keeps the key"
install_package "$PACKAGE"
[ "$(sha256sum /etc/lanpilot/secret.key)" = "$KEY_BEFORE" ] || fail "reinstalling replaced the key"

echo "== remove keeps key, configuration and data"
remove_package
[ ! -e /opt/lanpilot/bin/lanpilot ] || fail "the application is still installed"
[ -f /etc/lanpilot/secret.key ] || fail "removing the package deleted the key"
[ -f /var/lib/lanpilot/lanpilot.db ] || fail "removing the package deleted the database"

if [ "$FORMAT" = deb ]; then
    echo "== purge removes everything"
    apt-get purge -y lanpilot
    [ ! -e /etc/lanpilot ] || fail "purge left /etc/lanpilot"
    [ ! -e /var/lib/lanpilot ] || fail "purge left /var/lib/lanpilot"
fi

echo "ALL PACKAGE CHECKS PASSED"
