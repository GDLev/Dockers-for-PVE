#!/usr/bin/env bash
# Disposable CI workers only. Tests use the Compose parser, never the daemon.
set -Eeuo pipefail
[[ $(uname -m) == x86_64 ]] || { echo 'CI Compose pin requires x86_64' >&2; exit 1; }
apt-get update
apt-get install -y --no-install-recommends docker.io curl ca-certificates openssl python3-venv
/usr/bin/python3 -m venv /opt/betterdocker-ci-python
/opt/betterdocker-ci-python/bin/python -m pip install --require-hashes -r requirements-test.txt
mkdir -p /usr/local/lib/docker/cli-plugins
curl --proto '=https' --proto-redir '=https' -fsSL https://github.com/docker/compose/releases/download/v2.39.4/docker-compose-linux-x86_64 -o /usr/local/lib/docker/cli-plugins/docker-compose
printf '%s  %s\n' 7af95166a730b87e172d4fc9aefea8725d3c6c7327d59149267b452114ddb7d4 /usr/local/lib/docker/cli-plugins/docker-compose | sha256sum --check --strict
chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version
