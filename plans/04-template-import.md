# Templates from images, not Dockerfiles

Goal: replace server-side Dockerfile builds with **importing a prebuilt OCI
image**, done by an admin. Templates stay; Flint stops being a build system.

## Why

Templates are useful: a custom root disk plus a golden snapshot that VMs boot
from quickly. Building them inside Flint isn't:

- The people who define environments are the builder's platform team. They
  already have Docker, CI and a registry. Flint compiling their Dockerfile is a
  second, worse build system.
- `POST /templates/build` runs arbitrary build steps, so it needs a jailed
  builder VM running `dockerd` and BuildKit, a builder appliance made with host
  Docker, and build-job supervision. That machinery only pays off if untrusted
  tenants submit Dockerfiles, and tenants aren't the audience.
- Per-user environments are better served by runtime state: install in the VM,
  then `checkpoint` or `fork`.
- A raw Dockerfile (`Template.from_dockerfile`, or the REST API) doesn't get
  `flintd` appended, so its VMs aren't usable. Only the SDK's builder steps get
  it. Import fixes this by construction.

## What stays

Everything after "here is a root disk" is reused as is:

- Build-job supervision in `_build_job.py`: admission, journal, cgroup,
  deadline, atomic publish swap, recovery. It is generic apart from the
  `dockerfile` argument and Docker cleanup.
- The disposable jailed VM in `_builder.py` (`_vm`, the recovery marker, wipe)
  and its bounded artifact stream (`_BoundedReader`, exact-size decompression).
- Golden snapshots: `create_golden_snapshot` (Firecracker) and
  `ch_create_golden_snapshot` (Cloud Hypervisor), already confined as tenant
  input.
- The template registry, cluster publish and restore through snapd, the
  scheduler, VM creation and VM sets, and template tenancy (`""` = shared).
- List, get and delete on every surface.

## Design

### API

`POST /templates` (admin only, daemon and cluster):

```json
{"name": "python", "image": "docker.io/library/python:3.12-slim",
 "rootfs_size_mb": 2048, "tenant": "acme"}
```

- `image` must be a full reference. Flint resolves a tag to a digest once, at
  import, and records both. A template never changes because a tag moved.
- `tenant` is optional: absent means shared, as today.
- The response is `{template_id, status: "building"}`. Clients poll
  `GET /templates/{id}` as today, and the template now also reports
  `{image, digest}`.
- Creating and deleting templates is admin only; listing and reading stay
  open. `_ADMIN_ONLY` in `daemon/auth.py` matches paths, not methods, so it
  needs a method-aware check. The cluster uses `admin(...)` on the route in
  `cluster/internal/server/api.go`. Today any service token or SSO session can
  build.
- `POST /templates/build` is removed. It returns 410 with a message pointing at
  `POST /templates`, so old SDKs fail clearly.
- Linux `amd64` images only. A multi-arch index resolves to `linux/amd64`, and
  anything else is rejected.

### Conversion (no image bytes are parsed on the host)

Image layers are untrusted input: paths, symlinks, hardlinks, sizes, setuid
bits. Unpacking them on the host as root is unsafe, and tarball input to
`mkfs.ext4 -d` needs e2fsprogs 1.47.1, which Ubuntu 24.04 doesn't ship. So the
conversion runs inside the **existing disposable builder VM**, which already
has public-only egress, no host or storage reach, a private disk and a wipe on
exit:

1. Boot the builder VM (same jail, netns and firewall as today).
2. Inside it: `crane export <ref>@<digest> - | tar -x -C /export`. This pulls
   and flattens the layers (respecting whiteouts) without running anything from
   the image.
3. Inject Flint's guest files into `/export` (see below).
4. `mkfs.ext4 -d /export` at `rootfs_size_mb`, then `gzip`, then stream out
   through the existing bounded reader. If the image is bigger than the disk,
   `mkfs` fails and the import reports it.
5. On the host: write `rootfs.ext4`, take the golden snapshot in the usual
   jailed VM, then publish (registry, then snapd in a cluster).

Code from the image only ever runs inside a VM. Compared with today it never
runs during the build at all: there is no `RUN` and no package manager.

**Builder appliance:** Alpine with `crane`, `e2fsprogs`, `tar` and `gzip`. No
`dockerd` or `buildx`. Today it is made with host `docker build`. Instead,
prepare it in a builder VM booted from the default rootfs (`apk add` inside
it), then save the disk, keyed by content as today. This removes the last host
Docker dependency on Linux.

### Guest files: no requirements on the image's userland

Today `init-net.sh` relies on the image's `/bin/sh`, `mount`, `ip`, `grep` and
`sleep`. The Dockerfile path tries to `apt-get`/`apk add iproute2`, which runs
the image's package manager. Instead the import adds:

- `/usr/local/bin/flintd` (static Go binary, as today).
- `/usr/local/lib/flint/busybox` (static) for `init-net.sh`. The script's
  shebang and commands use it, so the network comes up on minimal or distroless
  images too.
- `/etc/init-net.sh`.

Two image dependencies remain:

- `flintd` runs the argv it is given (`process.go:113`), so commands that go
  through a shell need the image's `/bin/sh`.
- Volume mounts shell out to the image's `mount`/`umount`
  (`handlers_mount.go:52,74`). Switch them to the bundled busybox or to
  syscalls.

`flintd` could fall back to the bundled busybox for a shell when the image has
none (*open question 3*). The injected files overwrite whatever the image has
at those paths.

### Re-imports and cluster caches

Importing to an existing id replaces it with the same atomic swap as today: a
failed import keeps the old template bootable. One existing bug has to be fixed
here. A rebuild only forgets the template on the build host
(`server.py:851`, `api.go:565`), so other hosts keep serving their old copy and
`hostHasTemplate` skips the fetch (`vmops.go:122`). Key host copies on the
template's current `digest` and snapshot id, and refetch when they differ.

### Surfaces

- **SDK:** `Template.import_image(name, image, rootfs_size_mb=..., tenant=None)`
  blocks until ready and returns `TemplateInfo` with `image` and `digest`.
  Remove the builder (`from_*_image`, `apt_install`, `pip_install`,
  `npm_install`, `run_cmd`, `copy`, `set_workdir`, `set_envs`, `git_clone`,
  `from_dockerfile`, `build`). This is a breaking change.
- **CLI:** `flint templates import|list|delete`. Admins are the audience and
  there is no template CLI today. The web UI already tells users to run a
  `flint build` that doesn't exist.
- **Web UI:** the build dialog becomes an import dialog (name, image, size,
  tenant), shown only to admins (`useIsAdmin`). Fix the tenant hint: an empty
  tenant currently resolves to `"default"`, not shared.
- **TUI:** no change (list, launch, inspect).

## What gets deleted

| Item | Where |
|------|-------|
| Dockerfile generation, `_extract_rootfs`, worker `build_template` | `src/flint/core/_template_build.py` (keeps the `flintd` and `init-net.sh` helpers) |
| `dockerd`/BuildKit build, host-Docker appliance | `src/flint/core/_builder.py` `build_rootfs`, `_appliance` |
| Duplicated CH build body | `linux_cloud_hypervisor.py:570-633`; both backends call one shared import worker |
| `docker rm` and `.builder` cleanup | `_build_job.py:120-124` |
| SDK builder | `src/flint/template.py` |
| `POST /templates/build` | `daemon/server.py:798-883`, `daemon/runtime.py:193-244`, `cluster/.../api.go:500-573` |
| `dockerfile` column | `cluster/internal/db/db.go` (migration drops it), `models.go` |
| Build dialog and Dockerfile example | `web/src/pages/Templates.tsx`, `web/src/api/templates.ts`, `api/mock.ts` |
| Dockerfile docs | `docs/sdk/templates.mdx`, `docs/api-reference/templates.mdx`, `docs/architecture/golden-snapshots.mdx:44-54` |

## Threat model changes

Update `docs/architecture/security.mdx` (Template builds row, :70 and :79) and
`plans/01-tenant-isolation.md:40`:

- Template imports are admin only. Tenants can't create templates.
- Image bytes are only parsed inside a disposable jailed builder VM with
  public-only egress. The host never unpacks, mounts or runs image content.
- The host only receives a size-bounded ext4 image, then boots it in a jail
  for the golden snapshot (unchanged).
- Host Docker is no longer used on Linux.

## Tests

Following [the testing rules](../.claude/rules/testing.md), rewrite the
existing flows instead of adding files:

- `test_template_build_and_run` becomes **`test_template_import_and_boot`**,
  on both backends in the daemon CI job:
  - import a public image and boot it;
  - `exec` a binary only that image has;
  - a template's digest stays the same after the tag moves (import by digest);
  - a failed import (missing image, image too large for `rootfs_size_mb`)
    reports its error and leaves the previous template bootable;
  - a service token gets 403.

  Keep the builder-isolation probes (host listener and metadata unreachable)
  by running them from the builder during an import.
- A distroless image boots and its network comes up, which proves the bundled
  busybox.
- `tests/cluster/test_cluster.py::test_template_built_on_one_host_boots_on_another`
  imports instead of building. Extend it to re-import, then boot on the
  *other* host and check it got the new digest (the cache bug).
- Keep `tests/maintenance/...::test_build_deadline_and_admission_...` and the
  Go tenancy tests in `templates_test.go`. Drop their `dockerfile` fixtures.
- CI: the daemon jobs no longer need Docker on the runner for templates.

## Order of work

1. **Import path, daemon:** appliance without host Docker, the in-VM
   conversion, guest-file injection with busybox, `POST /templates` (admin),
   410 on the old route, SDK `import_image`, rewritten e2e test. Delete the
   Dockerfile path in the same PR, so there is never a second build system.
2. **Cluster:** admin route, placement restricted to Linux hosts, digest-keyed
   host caches, schema migration, cluster test.
3. **Surfaces:** CLI `flint templates`, web import dialog, docs, threat model.

Templates built from Dockerfiles before this change keep working: their
artifacts on disk and in snapd are unchanged.

## Open questions

1. **Private registries.** v1 imports public images only. Next would be a
   per-import registry credential: write-only, used inside the builder VM, and
   never stored.
2. **Pinning the appliance's `crane`.** Use `apk` in the appliance VM, or a
   pinned static release with a checksum. The checksum is better for
   reproducibility.
3. **Images without `/bin/sh`.** Should `flintd` fall back to the bundled
   busybox for `exec`, or reject such images at import?
4. **A VM as a template.** "Save this VM as a template" (from a checkpoint) is
   the user-facing way to customise an environment. It's a follow-up, not part
   of this plan.
