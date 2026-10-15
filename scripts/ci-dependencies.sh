#!/usr/bin/env bash
# Disposable CI workers only. Tests use the Compose parser, never the daemon.
set -Eeuo pipefail
[[ $(uname -m) == x86_64 ]] || { echo 'CI Compose pin requires x86_64' >&2; exit 1; }
apt-get update
apt-get install -y --no-install-recommends curl ca-certificates openssl python3-venv
if ! command -v docker >/dev/null; then
    apt-get install -y --no-install-recommends docker.io
fi
/usr/bin/python3 -m venv /opt/betterdocker-ci-python
/opt/betterdocker-ci-python/bin/python -m pip install --require-hashes -r requirements-test.txt
mkdir -p /usr/local/lib/docker/cli-plugins
curl --proto '=https' --proto-redir '=https' -fsSL https://github.com/docker/compose/releases/download/v5.6.0/docker-compose-linux-x86_64 -o /usr/local/lib/docker/cli-plugins/docker-compose
printf '%s  %s\n' 40343e21ca777173e69cff5dbafeb37c6f81f3b0d57d9e597f036e95eb63e76a /usr/local/lib/docker/cli-plugins/docker-compose | sha256sum --check --strict
chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version
# Privilege-drop fixtures need a real unprivileged reader and a root-controlled,
# publicly readable code tree. Do not run this CI setup on a PVE installation.
id betterdocker-parser >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin betterdocker-parser
BD_CI_SOURCE=/opt/betterdocker-ci-source
mkdir -p "$BD_CI_SOURCE"
for item in assets frontend backend scripts systemd packaging tests install.sh start.sh README.md API_DOCS.md CONTRIBUTING.md SECURITY.md RELEASE_NOTES.md requirements-test.txt LICENSE; do
    cp -a "$item" "$BD_CI_SOURCE/"
done
chown -R root:root "$BD_CI_SOURCE"
chmod -R a+rX,go-w "$BD_CI_SOURCE"
