# Security

> [!WARNING]
> Dockers for Proxmox VE is development software for small, non-critical installations. It is not recommended for commercial deployments or hostile multi-tenant hosting.

## Report a vulnerability

Email [security@gdlev.dev](mailto:security@gdlev.dev). Include the affected version or commit, PVE version, runtime mode, required permissions, and the observed impact. Use a minimal reproduction on a disposable installation if one is available.

Keep vulnerability details out of public issues. Do not send passwords, API tokens, private signing keys, `.env` files, backups, or unredacted logs. For sensitive attachments, ask for a private transfer method first.

The maintainer assesses the affected permissions, versions, and runtime modes, then confirms the issue and prepares a fix with a regression check. Disclosure follows coordination with the reporter. This project has no guaranteed response time or support SLA. If email delivery fails, request a working private contact in a public issue without including the vulnerability details.

## What a DCT isolates

Docker runs directly on the PVE node. DCTs share its kernel. A DCT is not a VM or an unprivileged LXC, and the extension does not guarantee that container escape is impossible.

| Runtime | Privileges and isolation |
| --- | --- |
| Rootful | The Docker daemon has host-root authority. A container runs as the image's configured user, often container root. Hardened reduces capabilities and applies an enforcing AppArmor profile, but does not remove the daemon's authority. |
| Rootless | One `betterdocker` runtime account and its subordinate UID/GID ranges are shared by all DCTs on a node. Container root maps into that user namespace. This is not a separate runtime account or user namespace per DCT. Container AppArmor profiles are unavailable in the tested setup. |

Rootless private networks enforce assigned IPv4 and MAC identities and block direct bridge traffic between different DCTs. These rules remain active when optional guest firewall filtering is disabled. Services within one DCT share a network trust domain. DCTs can communicate through published ports, subject to enabled guest firewall policy.

Published IPv6 uses a host TCP/UDP adapter in rootless mode, so its ports must be free on the host. Private Docker bridges do not support IPv6. In both runtimes, endpoint addresses restrict host access to published application ports and acquisition control traffic. Wildcard node listeners are not admitted as application sockets. DHCP and router advertisements require a trusted LAN; address validation does not authenticate the server or router.

Keep PVE, Docker, the kernel, and application images updated. Rootless reduces host-root exposure, but all DCTs still depend on the shared runtime and kernel.

## Guest access and administrator access

PVE guest and pool ACLs authorize DCT operations. A guest ID or pool is an access-control boundary, not a separate Docker daemon. Restricted API tokens use the intersection of the user's and token's permissions.

Deployment editors can read imported source and environment variables. Console access can expose application secrets through shells and logs. Backup access includes secrets in the backup. Grant these privileges only to users who can be trusted with the application data.

Node administrators control runtime installation and conversion, security policy, aggregate budget exemptions, host networking and storage, and custom AppArmor profiles. Only `root@pam` can manage guest hooks. Docker sockets, the runtime account, host-root SSH, and host configuration remain administrator interfaces. Giving someone these interfaces bypasses guest ACL isolation.

Dockers for Proxmox VE must be installed on every cluster node to protect the shared guest ID namespace. Removal is blocked while any DCT metadata or disk ID reservation remains anywhere in the cluster. Uninstallation retains Docker, runtime configuration and required host maintenance. It does not erase external data or revoke unrelated Docker access.

## Hardened policy

New DCTs created without node `Sys.Modify` use Hardened. Administrator-created DCTs can use Standard. Only a node administrator can change the policy of a stopped DCT.

Hardened requires no new privileges, Docker's default seccomp filtering, and `cap_drop: [ALL]` with additions restricted to the extension's allowlist. It rejects privileged containers, shared host namespaces, devices, custom runtimes, and settings that weaken these requirements. Rootful Hardened also requires the installed `betterdocker-default` AppArmor profile in enforce mode. Rootless has no container AppArmor profile.

Hardened does not require a non-root image user or a read-only root filesystem by default. Use an application-specific user and drop unneeded capabilities. The optional administrator policy `/etc/betterdocker/filesystem-policy.json` can require read-only root filesystems and bounded writable mounts for Hardened rootless DCTs. See [the filesystem policy](API_DOCS.md#additional-disks-and-recovery).

Start and recovery check the applied container policy and current firewall generation. Changing policy preserves writable files and volumes, but recreates stopped containers. Tmpfs contents are temporary.

## Resource limits

CPU, RAM, and PID caps are enforced per container. Aggregate DCT budgets sum declared limits across replicas and reject configurations exceeding their ceilings. These budgets do not reserve node capacity or guarantee application availability. Memory reservations and CPU shares are not hard ceilings.

A managed data disk limits data stored on that disk. It does not cap images, Docker logs, snapshot copies, backup staging, retained writable history, or other mounts. Rootful writable-layer quotas require `overlay2` on XFS with project quotas. Rootless writable-layer quotas are unsupported.

Log rotation is per container. Retention budgets and free-space reservations limit extension-managed operations, but other host processes can consume the same storage. Tmpfs uses memory. Keep node capacity monitoring and backups in place. See [resource budgets](API_DOCS.md#project-resource-budgets) and [storage admission](API_DOCS.md#peak-space-checks).

## Release security

Release assets are signed with the maintainer key in `packaging/release-key.pem`. Unsigned CI artifacts and development checkouts are not signed installation releases.

Before publishing, complete the required release checks, clean signed installation acceptance, the supported environment matrix, and an independent manual review. Publish only the tested artifacts with signed metadata and a public-key fingerprint confirmed through an independent channel. See [release preparation](CONTRIBUTING.md) and [release notes](RELEASE_NOTES.md).

### Verify a release

Run in a new empty directory on the PVE node as root. Install `curl`, `python3`, `openssl` and `ca-certificates` first if absent. Confirm this public-key fingerprint directly with the maintainer through an independently trusted channel before using it. Downloading a key and fingerprint together does not establish trust.

```bash
(
set -euo pipefail
bd_release_url=https://github.com/GDLev/Proxmox-BetterDockers/releases/download/v0.20.0
for file in install.sh RELEASE.json RELEASE.sig release-key.pem; do
    curl --proto '=https' --proto-redir '=https' --fail --location --max-filesize 4194304 \
        "$bd_release_url/$file" --output "$file"
done
bd_pin=8f1983fa57cf5e003b0cd1e8885472c20a8f5fd7f3b376b6d154714a97fc77ff
test "$(openssl pkey -pubin -in release-key.pem -outform DER | sha256sum | cut -d ' ' -f1)" = "$bd_pin"
openssl dgst -sha256 -verify release-key.pem -signature RELEASE.sig RELEASE.json
python3 - <<'VERIFY'
import hashlib, json, pathlib
metadata = json.loads(pathlib.Path('RELEASE.json').read_text())
actual = hashlib.sha256(pathlib.Path('install.sh').read_bytes()).hexdigest()
if metadata['version'] != '0.20.0' or metadata['artifacts']['install.sh'] != actual:
    raise SystemExit('Installer checksum or version mismatch')
VERIFY
install -d -m 0755 /etc/betterdocker
install -m 0644 release-key.pem /etc/betterdocker/release-key.pem
printf '%s\n' "$bd_pin" > /etc/betterdocker/release-key.sha256
chmod 0644 /etc/betterdocker/release-key.sha256
)
```

Stop if any command fails. Do not run the installer after a failed verification. Then follow the [installation instructions](README.md#installation).

For a confirmed release vulnerability, identify the affected versions, publish an advisory with the impact and mitigation, and withdraw affected downloads and installation instructions. Prepare and test a replacement release under a new version. Do not overwrite a published version with different bytes. Keep the advisory and affected hashes available so administrators can identify their installations.

If the signing key is compromised, stop distribution and announce revocation through the independent contact channel. Establish and confirm a replacement key before resuming distribution. A new key downloaded from the same server alone does not restore trust.
