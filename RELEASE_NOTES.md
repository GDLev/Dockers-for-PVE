# Release notes

## 0.20.1 (prerelease)

- Add **Migrate** to the installer for conversion between rootful and rootless Docker, with prerequisite checks, explicit confirmation and recovery commands.
- Show the migration journal before guest operations so it remains available if conversion fails.
- Use the Dockers for PVE name in the installer and document migration through its menu.

Back up and stop DCTs before migration. Guests remain stopped after conversion. This is a prerelease for small, non-critical installations.

## 0.20.0 (prerelease)

The first Dockers for Proxmox VE prerelease integrates Compose projects into PVE as DCT guests, with rootful or rootless Docker, guest permissions, resource controls, networking, storage, snapshots, PBS backup/restore and cluster operations.

This is a prerelease for small, non-critical installations. Read the [requirements and security notes](README.md) and [security model](SECURITY.md) before installation. It is not recommended for production or hostile multi-tenant workloads.
