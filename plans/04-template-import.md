# Templates from images, not Dockerfiles

**Status: implemented.** Admins import a prebuilt container image; Flint no
longer builds Dockerfiles. Templates stay; Flint stopped being a build system.

## Why

- Templates are useful: a custom root disk plus a golden snapshot that VMs boot
  from quickly. Building them inside Flint wasn't. The people who define
  environments (the builder's platform team) already have Docker, CI and a
  registry.
- `POST /templates/build` ran arbitrary build steps, so it needed `dockerd` and
  BuildKit in a builder VM and host Docker for the builder image. That only paid
  off if untrusted tenants submitted Dockerfiles, and tenants aren't the
  audience.
- A raw Dockerfile didn't get `flintd`, so its VMs weren't usable.
- Per-user environments are better served by runtime state: install in the VM,
  then `checkpoint` or `fork`.

## What shipped

**API.** `POST /templates {name, image, rootfs_size_mb=2048, tenant?,
registry_auth?}`, admin only on the daemon (`_ADMIN_WRITES` in
`daemon/auth.py`, method-aware) and the cluster (`admin(...)`). Deleting is admin
only too; reading stays open. `POST /templates/build` returns 410 with
directions. No `tenant` means every tenant may boot it. An import can't change
a template's owner (409). Templates report `image` and the resolved `digest`.

**Conversion.** Image bytes are only parsed inside the existing disposable,
jailed builder VM:

1. `flint-import` (`guest/flint-import`, go-containerregistry, pinned by
   `go.sum`, built like `flintd`) resolves the tag to a `linux/amd64` digest,
   rejects images whose compressed layers exceed the disk, writes the image's
   `ENV` as shell exports and streams the flattened layers (whiteouts applied)
   into GNU tar. Rate-limited pulls (429) are retried with backoff.
2. Flint's guest files are added under a `chroot` with Flint's static busybox
   (`assets/template-inject.sh`), so the image's symlinks resolve inside the
   image. Nothing from the image runs in the builder.
3. `mkfs.ext4 -d`, gzip, and the existing bounded stream to the host. The host
   then takes the golden snapshot in a jailed VM, as before.

**Builder appliance.** The trusted default rootfs plus Alpine's signed
`e2fsprogs tar busybox-static`, added by `apk` inside a builder VM and cached by
the rootfs's identity. No host Docker on Linux.

**Guest contract.** Imports add `/usr/local/bin/flintd`, `/etc/init-net.sh`,
`/etc/flint/image.env` and `/usr/local/lib/flint/busybox` with its applets in
`/usr/local/lib/flint/bin` (last on `PATH`, so the image's tools win). Boot
steps call busybox directly. If the image has no `/bin/sh`, busybox becomes it,
so distroless images run `commands.run`.

**Re-imports and cluster caches.** A failed import keeps the previous template
bootable. In a cluster, only the import host's report finishes an import and
publishes a snapshot; finishing deletes the template's older snapshots, and VM
creation always names the current snapshot, so a host holding an older copy
replaces it. Previously a rebuilt template kept booting stale copies on other
hosts.

**Surfaces.** SDK `Template.import_image / get / list / delete` (the fluent
builder and `TemplateInfo` are gone); CLI `flint templates import|list|delete`
(password prompted or read from stdin, never in argv); web import dialog for
admins.

## Decisions

1. **Private registries:** supported per import. Credentials travel to the
   builder's importer only, which deletes the file once read; they are never
   stored, logged or put in the job journal. Re-importing needs them again.
2. **Pinning the puller:** our own small importer pinned by `go.sum`, not a
   downloaded `crane`.
3. **Images without `/bin/sh`:** busybox stands in, rather than rejecting them.
4. **"Save a VM as a template":** out of scope; checkpoint and fork cover
   per-user state for now.

## Tests

- `test_template_import_and_boot` (both backends): a missing image fails with a
  clear reason, a real import boots and has the image's binaries and `ENV`, a
  re-import too big for its disk fails clearly and keeps the old template
  bootable at the same digest.
- `test_template_from_a_distroless_image_boots`: no shell or tools in the image,
  yet the network comes up and commands run.
- `test_service_token_cant_administer`: service tokens read templates but can't
  import or delete them.
- Cluster: `test_template_imported_on_one_host_boots_on_another` re-imports
  while the second host holds the old copy and checks it boots the new image.
  Go tests cover admin-only routes, ownership and request validation.
- `tests/unit/test_template_import.py`: image references that could reach a
  shell are refused.
