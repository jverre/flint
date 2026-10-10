# Roadmap

## Positioning

Flint is an open-source VM orchestrator for teams **running their users'
untrusted code**. It sits behind the builder's backend and keeps every
tenant's VMs strictly isolated by default.

What Flint is *not*:

- **Not an identity system.** The builder's app owns its users. Flint takes a
  `tenant` label from a trusted caller and enforces isolation around it.
- **Not a single-host sandbox runtime** competing with Hypeman or
  microsandbox. The runtime we have becomes the host layer of an orchestrator.
- **Not an observability backend.** Flint emits tenant-tagged events and
  exports them; it keeps only a recent window in the product.

The security pitch is **workload isolation**: the attacker is the code inside
a VM, not the API caller. API auth stays deliberately small (see
[02-api-and-surfaces.md](02-api-and-surfaces.md#auth)).

## Landscape (October 2026)

- **E2B** (Apache-2.0, `e2b-dev/runtime`): closest prior art. Firecracker,
  snapshot-first, per-sandbox access tokens. Built for short-lived agent
  sandboxes; heavy to self-host; no Terraform for workloads.
- **Hypeman** (`kernel/hypeman`, MIT): multi-hypervisor, Docker-like CLI,
  single host, no tenancy model or Terraform.
- **flintlock / Liquid Metal**: gRPC microVM lifecycle per host, aimed at
  Cluster API, not app developers.
- **Incus**: the reference for project-level isolation (restricted projects,
  per-project networks/storage/limits, OpenFGA). No modern SDK or MCP.
- **Fly Machines / Modal / Cloudflare Sandbox / exe.dev / boat**: closed
  platforms; useful for DX and runtime choices (Firecracker for short-lived,
  Cloud Hypervisor/QEMU for long-lived VMs that need hotplug, nested virt,
  live migration).
- **Terraform**: no maintained provider for microVM workloads exists.

Nobody combines strict per-tenant isolation, a typed SDK, a real Terraform
provider and built-in tenant-aware observability in open source.

## Components

| # | Component | Status |
|---|-----------|--------|
| 1 | **Host agent**: backends (Firecracker, Cloud Hypervisor, macOS VZ), golden snapshot + pool, lifecycle, volumes | Exists (current daemon) |
| 2 | **Guest agent** (`flintd`): exec, PTY, files, processes | Exists |
| 3 | **Isolation layer**: jailing, per-tenant networks, egress, secrets, storage wipe, quotas | Partial, see [01](01-tenant-isolation.md) |
| 4 | **Control plane API**: OpenAPI source of truth, `metadata/spec/status`, `tenant`, auth; later multi-host + scheduler | Partial (FastAPI, single host, no auth) |
| 5 | **Event pipeline**: persisted timeline + audit log, tenant-tagged, OTLP/Prometheus/webhook export | Partial (in-memory bus) |
| 6 | **Surfaces**: Python SDK, CLI, TUI, TypeScript SDK, MCP, Terraform, web dashboard | Python SDK/CLI/TUI exist |
| 7 | **Day-2 ops**: rolling updates with health gates, host cordon/drain, live migration, backups | Not started |

## Phases

### Phase 1: isolated single host

Make one host safe to run many tenants' untrusted code. Details in
[01-tenant-isolation.md](01-tenant-isolation.md).

- Service credential on the daemon; Unix socket by default.
- `tenant` on every VM, volume and template build.
- No VM↔VM traffic across tenants; VMs can't reach host services or the
  control plane.
- Secrets encrypted at rest, write-only, redacted on read.
- Disks, snapshots and volumes wiped on delete.
- Cloud Hypervisor backend jailed to the same standard as Firecracker.
- Written threat model.

**Done when** an end-to-end test with two tenants proves every boundary in the
threat model, on both Linux backends.

### Phase 2: one API, many surfaces

Details in [02-api-and-surfaces.md](02-api-and-surfaces.md).

- Freeze the OpenAPI spec; resources follow `metadata/spec/status`.
- Persist events as a per-VM timeline and an append-only audit log.
- Per-VM, short-lived direct-access tokens (terminal, files, exposed ports).
- Generate the TypeScript SDK and an MCP server from the spec.
- OTLP + Prometheus export; trace context passed into `exec`.

### Phase 3: cluster

- Split the daemon: host agent (current code) + control plane (Postgres,
  scheduler, reconcilers). Control plane likely in Go (Terraform provider
  must be Go anyway; single static binary).
- Per-tenant networks spanning hosts (WireGuard overlay).
- Terraform provider (workloads) + admin provider.
- Web dashboard: fleet → tenant → VM → hosts.

### Phase 4: day-2 operations

- Rollouts in waves with health gates, pause, rollback.
- Host cordon/drain; live migration on Cloud Hypervisor.
- Backup policies with restore drills.
- MCP write tools gated by human approval.

## Where to start

Phase 1, in this order:

1. Network isolation between tenants (biggest real gap, see 01).
2. Secrets at rest and redaction (smallest change, most embarrassing gap).
3. Service credential + `tenant` field.
4. Wipe on delete; CH jailing.
5. Two-tenant e2e test covering every boundary.

## Non-goals for now

- Kubernetes integration or container orchestration.
- Lazy-loaded images and multi-cloud GPU scheduling (Modal-style).
- Windows guests.
- End-user identity, SSO for end users, Zanzibar-style policy engines.

## Open decisions

1. **License**: Apache-2.0 (adoption) vs AGPL (protection from hosted clones).
   Isolation and security features stay in the open core either way.
2. **Default Linux hypervisor**: Cloud Hypervisor (long-lived, live migration)
   vs Firecracker (density, fastest starts).
3. **Control plane language**: keep Python for the host agent; Go for the
   control plane is the current leaning.
4. **Hard vs soft tenancy across hosts**: shared hosts with per-tenant
   networks, or optional dedicated hosts per tenant.
