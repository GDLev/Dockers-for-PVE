#!/usr/bin/env bash
# Build the source archive. Authenticate it separately with sign-release.py.
set -Eeuo pipefail
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT_DIR=${1:-"$SOURCE_DIR/dist"}
RELEASE_VERSION=$(python3 "$SOURCE_DIR/scripts/dctlib/version.py")
RELEASE_EPOCH=${SOURCE_DATE_EPOCH:-$(git -C "$SOURCE_DIR" log -1 --format=%ct 2>/dev/null || printf '0')}
BUILD_DIR=$(mktemp -d)
trap 'rm -rf -- "$BUILD_DIR"' EXIT
mkdir -p "$OUTPUT_DIR" "$BUILD_DIR/betterdockers-$RELEASE_VERSION"
for path in assets frontend backend scripts systemd packaging install.sh start.sh README.md API_DOCS.md CONTRIBUTING.md SECURITY.md RELEASE_NOTES.md requirements-test.txt; do
    cp -a "$SOURCE_DIR/$path" "$BUILD_DIR/betterdockers-$RELEASE_VERSION/"
done
find "$BUILD_DIR" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$BUILD_DIR" -name .DS_Store -delete
find "$BUILD_DIR" -name '*.pyc' -delete
# Runner checkout modes are not installation permissions.
find "$BUILD_DIR" -type d -exec chmod 0755 {} +
find "$BUILD_DIR" -type f -perm /111 -exec chmod 0755 {} +
find "$BUILD_DIR" -type f ! -perm /111 -exec chmod 0644 {} +
tar --sort=name --mtime="@$RELEASE_EPOCH" --owner=0 --group=0 --numeric-owner \
    -czf "$OUTPUT_DIR/betterdockers-$RELEASE_VERSION.tar.gz" -C "$BUILD_DIR" "betterdockers-$RELEASE_VERSION"
(cd "$OUTPUT_DIR" && sha256sum "betterdockers-$RELEASE_VERSION.tar.gz" > SOURCE_SHA256SUMS)
