#!/bin/sh
# Build a LANPilot package for the distribution this script runs on.
#
#   packaging/build.sh <deb|rpm|archlinux> <wheel> [output-directory]
#
# The package contains a complete Python environment under /opt/lanpilot, so installing
# it needs no internet access. That environment holds compiled extensions and is tied to
# the distribution's Python version; the package therefore depends on exactly that
# version and has to be built once per distribution release. CI does this in containers
# (.github/workflows/release.yml).
#
# Environment:
#   PYTHON   interpreter to build with (default: python3)
#   NFPM     nfpm executable (default: nfpm)
#   DISTRO   label that becomes part of the package release, e.g. "debian13"
set -eu
umask 022

FORMAT="${1:?usage: build.sh <deb|rpm|archlinux> <wheel> [output-directory]}"
WHEEL="${2:?usage: build.sh <deb|rpm|archlinux> <wheel> [output-directory]}"
OUTPUT="${3:-dist}"
PYTHON="${PYTHON:-python3}"
NFPM="${NFPM:-nfpm}"
DISTRO="${DISTRO:-local}"

cd "$(dirname "$0")/.."
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
STAGING="$BUILD/root"
PREFIX="$STAGING/opt/lanpilot"

# --- Python environment ---------------------------------------------------------------
"$PYTHON" -m venv "$PREFIX"
"$PREFIX/bin/python" -m pip install --quiet --upgrade pip
"$PREFIX/bin/python" -m pip install --quiet "$WHEEL"
# Nothing installs into this environment after the build: ship neither pip nor the
# activation scripts.
"$PREFIX/bin/python" -m pip uninstall --quiet --yes pip
rm -f "$PREFIX"/bin/activate* "$PREFIX"/bin/Activate.ps1 "$PREFIX/.gitignore"

# The environment was created in a temporary directory but will live in /opt/lanpilot.
# Its scripts name their interpreter by absolute path; point them at the final location.
for script in "$PREFIX"/bin/*; do
    if [ -f "$script" ] && [ ! -L "$script" ] && head -c 2 "$script" | grep -q '^#!'; then
        sed -i "1s|^#!$PREFIX/bin/|#!/opt/lanpilot/bin/|" "$script"
    fi
done
sed -i "s|$PREFIX|/opt/lanpilot|g" "$PREFIX/pyvenv.cfg"
# Compiled files remember the build path for tracebacks; rebuild them with the real one.
find "$PREFIX" -name __pycache__ -type d -prune -exec rm -rf {} +
"$PREFIX/bin/python" -m compileall -q -s "$STAGING" -p / "$PREFIX/lib" >/dev/null

chmod -R go-w "$PREFIX"

if grep -rIl "$BUILD" "$PREFIX" >/dev/null 2>&1; then
    echo "build.sh: the build path leaked into the package:" >&2
    grep -rIl "$BUILD" "$PREFIX" | head >&2
    exit 1
fi

# --- Package metadata -----------------------------------------------------------------
PY_VERSION="$("$PREFIX/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_NEXT="$("$PREFIX/bin/python" -c 'import sys; print("%d.%d" % (sys.version_info[0], sys.version_info[1] + 1))')"
VERSION="$("$PREFIX/bin/python" -c 'from importlib.metadata import version; print(version("lanpilot"))')"
case "$(uname -m)" in
    x86_64) ARCH=amd64 ;;
    aarch64) ARCH=arm64 ;;
    *) echo "build.sh: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

case "$FORMAT" in
    deb)
        # 0.1.0.dev0 sorts after 0.1.0 for dpkg; "~" makes it sort before.
        VERSION="$(echo "$VERSION" | sed 's/\.\(dev\|a\|b\|rc\)/~\1/')"
        DEPENDS="  - \"python3 (>= $PY_VERSION)\"
  - \"python3 (<< $PY_NEXT)\"
  - systemd
  - util-linux"
        ;;
    rpm)
        VERSION="$(echo "$VERSION" | sed 's/\.\(dev\|a\|b\|rc\)/~\1/')"
        DEPENDS="  - \"python(abi) = $PY_VERSION\"
  - systemd
  - util-linux"
        ;;
    archlinux)
        DEPENDS="  - \"python>=$PY_VERSION\"
  - \"python<$PY_NEXT\"
  - systemd
  - util-linux"
        ;;
    *)
        echo "build.sh: unknown format '$FORMAT' (use deb, rpm or archlinux)" >&2
        exit 1
        ;;
esac

export ARCH VERSION DEPENDS STAGING
export RELEASE="1~$DISTRO"
[ "$FORMAT" = archlinux ] && RELEASE=1
"$PREFIX/bin/python" - packaging/nfpm.yaml.in "$BUILD/nfpm.yaml" <<'PYTHON'
import os
import string
import sys

template = string.Template(open(sys.argv[1]).read())
open(sys.argv[2], "w").write(template.substitute(os.environ))
PYTHON

mkdir -p "$OUTPUT"
"$NFPM" package --config "$BUILD/nfpm.yaml" --packager "$FORMAT" --target "$OUTPUT/"
echo "build.sh: built for Python $PY_VERSION on $DISTRO ($ARCH):"
ls -1 "$OUTPUT"
