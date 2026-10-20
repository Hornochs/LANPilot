#!/bin/sh
# Download the pinned nfpm release and verify its checksum.
#
#   packaging/fetch-nfpm.sh <target-directory>
#
# nfpm (https://nfpm.goreleaser.com) builds deb, rpm and Arch packages from one
# description without needing each distribution's own packaging tools.
set -eu

TARGET="${1:?usage: fetch-nfpm.sh <target-directory>}"
VERSION=2.48.0

case "$(uname -m)" in
    x86_64)
        ARCH=x86_64
        SHA256=6d7f9a28b2f397934403950fcbba83f598b08d02edbe79a0815b3b4897341750
        ;;
    aarch64)
        ARCH=arm64
        SHA256=a9a75213763783b7669719e505474c748c5ad7dc66591a195619deb50f85c2f4
        ;;
    *)
        echo "fetch-nfpm.sh: unsupported architecture $(uname -m)" >&2
        exit 1
        ;;
esac

ARCHIVE="nfpm_${VERSION}_Linux_${ARCH}.tar.gz"
mkdir -p "$TARGET"
cd "$TARGET"
curl -fsSLO "https://github.com/goreleaser/nfpm/releases/download/v${VERSION}/${ARCHIVE}"
echo "${SHA256}  ${ARCHIVE}" | sha256sum --check
tar -xzf "$ARCHIVE" nfpm
rm -f "$ARCHIVE"
