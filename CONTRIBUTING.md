# Working on BetterDockers

Keep each change focused on one feature or fix. Include the relevant tests and say how you checked it.

## Where to look

| Area | Source |
| --- | --- |
| PVE API permissions and native hooks | `backend/PVE/API2/BetterDocker.pm` |
| HTTP endpoints | `backend/PVE/API2/BetterDocker/`, grouped by feature |
| Guest CLI | `backend/PVE/CLI/dct.pm`, `scripts/dct.py` |
| Guest operations and command dispatch | `scripts/dctlib/projects.py` |
| Deployments and permissions | `scripts/dctlib/deployment.py`, `policy.py` |
| Networking, disks, settings and snapshots | Matching files in `scripts/dctlib/` |
| Node startup and guest hooks | `scripts/dctlib/lifecycle.py` |
| Runtime, firewall, backup and cluster transfers | Named helpers in `scripts/` and `backend/PVE/BetterDocker/` |
| PVE panel | `frontend/`, grouped by feature |
| Installation | `start.sh`, `install.sh`, `scripts/installer.py` |
| Debian package | `packaging/build-deb.sh` |

`provision.py` is the command entry point used by PVE and systemd. The Python modules share project paths and host operations through `dctlib.projects`; they use the selected runtime through `scripts/runtime.py`.

The panel uses PVE's ExtJS environment. `frontend/sources.json` sets the source order. The installer builds one `betterdocker.js` file, so the browser still loads a single script. Edit the source files, not the installed bundle. No npm dependencies are needed.

## Checks

From the repository root, with Python 3.12 or newer and Node.js installed:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-test.txt
python3 -m unittest discover -s tests
python3 scripts/build_frontend.py | node --check
node tests/test_frontend.js
bash -n install.sh start.sh scripts/docker-install.sh packaging/build-deb.sh
```

Tests named `live_*.py` use a real PVE cluster and can change guests or host configuration. Read the test before running it and use a disposable installation. Ordinary unit tests do not connect to a cluster.

Check install, repair and removal patch compatibility against a supported PVE node or a saved filesystem:

```sh
python3 tests/check_native.py /
```

This check reads native files, applies integration patches in memory twice to exercise repair, and removes them in memory. It does not install packages or restart services. Run it on both PVE 9.1 and 9.2 after package updates. Full installer lifecycle tests remain in `tests/live_installation.py` and `tests/live_terminal_installer.py`; these change the host and require a disposable node.
