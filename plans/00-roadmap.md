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
| 1 | **Host agent**: backends (Firecracker, Cloud Hypervisor, macOS VZ), golden snapshot + pool, lifecycle, volumes | Exists (the daemon; `flint agent` runs it as a cluster host) |
| 2 | **Guest agent** (`flintd`): exec, PTY, files, processes | Exists |
| 3 | **Isolation layer**: jailing, per-tenant networks, egress, secrets, storage wipe, quotas | Single host done except IO/network rate limits and same-tenant opt-in, see [01](01-tenant-isolation.md) |
| 4 | **Control plane API**: OpenAPI source of truth, `metadata/spec/status`, `tenant`, auth; later multi-host + scheduler | Cluster control plane, scheduler, admin/service tokens and `tenant` exist ([03](03-cluster.md)). No checked-in OpenAPI spec; resources are flat, not `metadata/spec/status` (only VM sets are declarative). The single daemon has a trusted Unix socket, admin/service tokens on TCP, and a `tenant` on every VM, volume and template |
| 5 | **Event pipeline**: persisted timeline + audit log, tenant-tagged, OTLP/Prometheus/webhook export | Partial: the cluster persists tenant-tagged events (`/v1/events`), without `actor` and not as an audit log. The single daemon's events are tenant-tagged and scoped, but in memory only. No exec events, no export |
| 6 | **Surfaces**: Python SDK, CLI, TUI, TypeScript SDK, MCP, Terraform, web dashboard | Python SDK/CLI/TUI exist; Terraform deploys clusters. Web UI at `/ui` on the daemon and the control plane (#31). No TypeScript SDK, MCP server or workload/admin Terraform provider |
| 7 | **Day-2 ops**: rolling updates with health gates, host cordon/drain, live migration, backups | Cordon, drain, migration (snapshot-based) and VM-set rolling updates exist ([03](03-cluster.md)); no health gates, backups or live migration |

## Phases

### Phase 1: isolated single host

Make one host safe to run many tenants' untrusted code. Details in
[01-tenant-isolation.md](01-tenant-isolation.md); the published threat
model is [docs/architecture/security.mdx](../docs/architecture/security.mdx).

- Service credential on the daemon; Unix socket by default. *Done (#41):
  trusted socket, admin/service tokens (hashed) on TCP, `flint token`,
  `flint ui`.*
- `tenant` on every VM, volume and template build. *Done (#41), with
  tenant-scoped tokens, events and lookups.*
- No VM↔VM traffic across tenants; VMs can't reach host services or the
  control plane. *Done: every VM is isolated from every other, tested.*
- Secrets encrypted at rest, write-only, redacted on read. *Done (#40).*
- Disks, snapshots and volumes wiped on delete. *Done (#42), with
  `vm.wiped`.*
- Cloud Hypervisor backend jailed to the same standard as Firecracker.
  *Done.*
- Per-tenant quotas. *Done on the single daemon (#43).*
- Written threat model. *Done (#44).*
- Still open: per-VM disk IO and network rate limits; letting a tenant's
  own VMs talk to each other; moving the firewall from iptables to
  nftables; encrypting policies in the cluster's database.

**Done when** an end-to-end test with two tenants proves every boundary in the
threat model, on both Linux backends. *Done: `test_two_tenants_are_isolated`
(#44).*

### Phase 2: one API, many surfaces

Details in [02-api-and-surfaces.md](02-api-and-surfaces.md).

- Freeze the OpenAPI spec; resources follow `metadata/spec/status`.
  *Not started.*
- Persist events as a per-VM timeline and an append-only audit log.
  *Partial: the cluster persists tenant-tagged events; no actor or audit log.*
- Per-VM, short-lived direct-access tokens (terminal, files, exposed ports).
  *Not started.*
- Generate the TypeScript SDK and an MCP server from the spec. *Not started.*
- OTLP + Prometheus export; trace context passed into `exec`. *Not started.*

### Phase 3: cluster

Details in [03-cluster.md](03-cluster.md).

- Split the daemon: host agent (current code, Python) + control plane
  (`flint-server`, Go: Postgres, scheduler, host registry, CA, gateway).
  *Done (#27), plus serialised VM operations and idempotent retries (#29).*
- Portable snapshots (`flint-snapd`): template, pause, checkpoint and fork
  are one chunked artifact in object storage, restorable on any host.
  *Done; restore is eager, not lazy.*
- High availability: per-VM availability classes, fencing, recovery,
  `VMSet` replicas spread across zones, drain by migration. *Done.*
- Cloud-agnostic Terraform deployment: cloud-init/systemd package, then thin
  per-cloud modules. *Done for `byo`; the AWS example has never been applied.*
- Per-tenant networks spanning hosts (WireGuard overlay), later.
  *Not started.*
- Terraform provider (workloads) + admin provider. *Not started.*
- Web dashboard: fleet → tenant → VM → hosts. *Done (#31): VMs, VM
  detail (metrics, shell, logs, files), templates, hosts, tenants and a load
  test page, served at `/ui`. No rollouts or audit views.*

### Phase 4: day-2 operations

- Rollouts in waves with health gates, pause, rollback. *Partial: VM-set
  rolling updates with `max_unavailable`; the only gate is "members
  running"; no waves, pause or rollback.*
- Host cordon/drain; live migration on Cloud Hypervisor. *Cordon and
  drain (by snapshot migration) done; live migration not started.*
- Backup policies with restore drills. *Not started.*
- MCP write tools gated by human approval. *Not started.*

## Where to start

Phase 1 is done apart from the open items listed under it; rate limits are
the next isolation gap. Phase 2 starts with the OpenAPI spec and the
persisted audit log.

## Non-goals for now

- Kubernetes integration or container orchestration.
- Lazy-loaded images and multi-cloud GPU scheduling (Modal-style).
- Windows guests.
- End-user identity, SSO for end users, Zanzibar-style policy engines.

## Open decisions

1. **License**: decided: Apache-2.0, chosen for adoption over AGPL's
   protection from hosted clones (see [LICENSE](../LICENSE)). Isolation and
   security features stay in the open core.
2. **Default Linux hypervisor**: Cloud Hypervisor (long-lived, live migration)
   vs Firecracker (density, fastest starts).
3. **Control plane language**: decided: Go for the control plane, Python
   stays for the host agent (see [03](03-cluster.md)).
4. **Hard vs soft tenancy across hosts**: shared hosts with per-tenant
   networks, or optional dedicated hosts per tenant.
