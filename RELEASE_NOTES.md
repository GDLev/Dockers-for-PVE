# Release notes

## 0.20.2 (prerelease)

- Configure the pinned release signing key automatically on first installation. The README installation command now works without a separate trust-setup step.
- Preserve existing trust files and stop with a readable error when they are incomplete, unsafe or inconsistent.

Release signatures and artifact checksums remain mandatory. The first installer download relies on GitHub HTTPS. Manual independent key verification remains available.

## 0.20.1 (prerelease)

- Add **Migrate** to the installer for conversion between rootful and rootless Docker, with prerequisite checks, explicit confirmation and recovery commands.
- Show the migration journal before guest operations so it remains available if conversion fails.
- Use the Dockers for PVE name in the installer and document migration through its menu.

Back up and stop DCTs before migration. Guests remain stopped after conversion. This is a prerelease for small, non-critical installations.

## 0.20.0 (prerelease)

The first Dockers for Proxmox VE prerelease integrates Compose projects into PVE as DCT guests, with rootful or rootless Docker, guest permissions, resource controls, networking, storage, snapshots, PBS backup/restore and cluster operations.

This is a prerelease for small, non-critical installations. Read the [requirements and security notes](README.md) and [security model](SECURITY.md) before installation. It is not recommended for production or hostile multi-tenant workloads.
