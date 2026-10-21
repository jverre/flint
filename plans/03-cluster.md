# Cluster (phase 3)

Goal: run Flint across many KVM hosts behind one API, **highly available**,
deployable on any cloud or bare metal with Terraform. The SDK keeps working
unchanged: it points at the control plane instead of a local daemon.

Decisions taken (October 2026):

- Control plane in **Go**; the host agent stays Python, with a Go sidecar
  for the snapshot data path.
- Terraform is **cloud-agnostic first**: one cloud-init/systemd package, thin
  per-cloud modules on top later.
- The minimum of phase 1 needed to put Flint on a network (host auth, tenant
  field) is part of this phase; the rest of phase 1 continues in parallel.
- **High availability is a goal, not a later phase.** No single host or
  control-plane replica may be a point of failure for the API, for
  templates, or for a VM's ability to be forked, resumed or recovered.

## Status (October 2026)

M0–M5 are built; M6 is not. Code:

| Piece | Where |
|-------|-------|
| `flint-cluster server` (control plane) | `cluster/internal/server`, `cluster/internal/db`, `cluster/internal/pki` |
| `flint-cluster snapd` (snapshot store, peers, fence, storage heartbeat, cert renewal) | `cluster/internal/snapd`, `cluster/internal/snapstore` |
| `flint-cluster join` | `cluster/internal/join` |
| Host agent mode (`flint agent`) | `src/flint/cluster/`, cluster hooks in `src/flint/core/backends/` |
| Node package, Terraform | `deploy/`, `terraform/modules/{flint-pki,flint-cloudinit}`, `terraform/examples/{byo,aws}` |
| Tests | `tests/cluster/` (e2e on real hosts), `cluster/**/_test.go` (chunk store, scheduler) |

Shipped as one Go binary, `flint-cluster`, with `server`, `snapd` and `join`
subcommands. Differences from the plan below:

- **Drain and migrate use snapshot migration on both backends** (pause,
  restore elsewhere, about a second of downtime). Cloud Hypervisor live
  migration isn't wired up.
- **Terraform bootstrap**: `flint-pki` generates both CAs and a reusable
  join secret up front, so hosts in an autoscaling group join on boot. Join
  tokens can carry the CA certificate instead of its fingerprint.
- **Fencing has three layers**: snapd's fence (persisted, so a restarted
  snapd still enforces it); the agent kills leased VMs whose lease lapsed
  while it was down (covers agent and snapd dying together); the watchdog
  (`FLINT_WATCHDOG_DEVICE`) covers a hung snapd. The watchdog path isn't
  exercised in tests: it would reset the test machine.
- **Tenant quotas**: VM count only (`PUT /v1/tenants/{t}/quota`), checked on
  create, fork and VM-set members.
- **VM operations are serialised across replicas** (#29): per-VM operation
  locks, takeover of operations a crashed replica left half-done, and
  idempotent retries via request keys.

Not done yet:

- M6: lazy restore (`userfaultfd`, chunk-backed disk), block-level dirty
  tracking, cloud-identity join, WireGuard overlay.
- Volumes and per-daemon `/config` through the control plane (they answer
  `501`); external fencing through cloud APIs; agent/server version-skew
  enforcement (agents report their version; nothing checks it).
- The AWS example validates against the provider schema but hasn't been
  applied.
- Planned in M3/M4 but not built: template distribution policies
  (`prefetch`, warm pool), fork `min_count`, the `cluster.capacity_low`
  event and scale-up webhook, and drain honouring a VM set's
  `max_unavailable`.
- The gateway reads the database on every request; the cached VM→host map
  that keeps it serving during a Postgres outage isn't built.
- Peer chunk fetches are authorised by host certificate only: any host can
  fetch any chunk whose hash it knows, not just chunks of snapshots
  assigned to it (Verification 8).

## What HA means for a VM

A VM is a running process on one host; its memory can't survive that host
crashing unless it was copied somewhere first. So HA is defined per VM by
how much state may be lost, and every mechanism below exists to make the
answer "nothing on disk, at most the last checkpoint in memory".

| `availability` | Host crashes | Host drained (planned) | Typical use |
|----------------|--------------|------------------------|-------------|
| `ephemeral` (default) | VM `lost`, event emitted, caller recreates | Snapshot-migrated to another host | Short-lived agent sandboxes |
| `restartable` | Recreated on another host from its template; workspace volume reattached | Snapshot-migrated | Stateless workers, members of a `VMSet` |
| `durable` | Restored on another host from its **last checkpoint** (memory + disk); workspace has no loss | Snapshot-migrated | Long-lived dev boxes, agents that run for hours |

- Workspaces already live in R2/S3 (r2nfs, S3 Files), so files written to
  the workspace survive a host crash in every class.
- `durable` checkpoints run on an interval (`checkpoint_interval`, default
  5 min) and on pause. RPO for in-memory state = that interval.
- Zero-loss memory (continuous replication, Remus-style) is a non-goal.
  Planned moves have no loss: live migration on Cloud Hypervisor, snapshot
  migration (pause → move → resume, seconds of downtime) on Firecracker.

## Prior art and what we take from it

| System | Model | What we take |
|--------|-------|--------------|
| **E2B** | API decides *where*, orchestrator per node owns *how*. Templates, pauses and forks are all the same artifact: memory + disk diffs in object storage, served lazily (`userfaultfd` for memory, CoW overlay for disk). Resume prefers the node with it cached. Fork checkpoints in place, up to 100 children | **Snapshots are the unit of mobility.** One artifact format for template, pause, checkpoint and fork; any host can restore it |
| **Fly.io** (`flyd` + `flaps` + Corrosion) | Each worker is the source of truth for its VMs. `flaps` is a stateless broker: asks workers for capacity, ranks, places synchronously, fails fast. Recovers from worker loss with standby machines prepared in advance | Host agent stays authoritative for running VMs. Synchronous placement, spread not bin-pack. Warm standbys for `VMSet` |
| **ClickHouse** | Replicated tables across replicas + Keeper (Raft) for coordination, 3 dedicated nodes. Cluster discovery: nodes self-register | Self-registration. Don't operate our own Raft in v1 |
| **Incus** | Embedded Raft (dqlite): 3 voters + 2 standbys. Instance evacuation on member loss; live migration for VMs | Join token UX. Evacuate = our drain |
| **k3s** | Same binary, SQLite for one node, Postgres/etcd for HA | One store interface, Postgres for HA |
| **Kubernetes** | ReplicaSet + anti-affinity + topology spread; node lease → taint → evict; fencing is the operator's problem | `VMSet` with spread constraints; host leases; explicit fencing |
| **Nomad/Consul, Teleport** | Join string rendered into cloud-init; short-lived agent certs from a CA | Same |

## Architecture

```
               SDK / CLI / TUI / Terraform provider
                             │  HTTPS, service token
                    ┌────────▼─────────┐
                    │  load balancer   │
                    └──┬────────────┬──┘
           ┌───────────▼──┐      ┌──▼───────────┐
           │ flint-server │ ...  │ flint-server │   Go, stateless, ≥2 replicas
           │ API·scheduler│      │ (reconcilers │   across zones; one leader
           │ gateway · CA │      │  via leader  │   for reconcilers
           └──────┬───────┘      │  lease)      │
                  │              └──────┬───────┘
                  ├──── Postgres (HA: managed, or Patroni)
                  │              │
                  │ mTLS         └──── Object store (S3 API): snapshot chunks
   ┌──────────────┼───────────────────────────┐
┌──▼───────────────────┐   ┌──────────────────▼───┐
│ host (zone a)        │   │ host (zone b)        │
│ flint-agent (Python) │   │ flint-agent          │
│  VM lifecycle, netns │   │                      │
│ flint-snapd (Go)     │◄─►│ flint-snapd          │  peer chunk fetch
│  snapshot store,     │   │                      │
│  cache, page server  │   │                      │
└──────────────────────┘   └──────────────────────┘
```

### Who owns what

| State | Owner | Notes |
|-------|-------|-------|
| A running VM's state | **Host agent** (SQLite) | Survives control-plane outages |
| VM spec, availability class, VM → host, lease | Control plane (Postgres) | Desired state + cache of host truth |
| Snapshots (template, checkpoint, pause, fork) | **Object store**, cached on hosts | Immutable, content-addressed; no host owns one |
| Hosts, zones, capacity, host class, cache contents | Control plane | From registration + heartbeats |
| `VMSet`s, tenants, quotas, credentials, CA | Control plane | |
| Events | Hosts emit → control plane persists, fans out | |

Nothing a VM needs to be restored lives only on one host once it has been
checkpointed. That is the property that makes fork-anywhere,
resume-anywhere and recovery possible.

## New components

### 1. Snapshot store (`flint-snapd`, Go, on every host)

The core new data-path component. Go because it moves GB/s of chunks, and
`services/r2nfs` already sets the pattern for a Go host service.

- **Format**: a snapshot = manifest (backend, host class, parent snapshot,
  vmstate blob) + memory and disk split into fixed chunks (4 MiB),
  content-addressed by hash. Zero chunks are implicit.
- **Diffs for free**: a pause/checkpoint/fork only uploads chunks that
  differ from its parent (template or previous checkpoint). Firecracker's
  dirty-page tracking tells us which memory chunks changed; disk chunks are
  diffed against the parent manifest (later: block-level dirty tracking via
  a dm/ublk overlay).
- **Tiers**: local cache (LRU, size-capped, reported to the scheduler) →
  **peer hosts** (a fresh fork's chunks are on its origin host before they
  reach object storage) → object store.
- **Restore**: v1 eager (fetch all chunks, then restore with today's code).
  v2 lazy: a `userfaultfd` page server for Firecracker memory and a
  chunk-backed block device for disk, so restore starts before the fetch
  finishes (E2B's model). Cloud Hypervisor's on-demand restore: verify
  support per VMM version.
- **GC**: refcounted by manifests in Postgres; chunks deleted when no
  snapshot references them (and wiped, per phase 1 storage rules).

### 2. Control plane (`flint-server`, Go)

- **API**: same OpenAPI spec as the daemon for VM routes (Python SDK only
  changes its base URL). Adds `Host`, `JoinToken`, `Tenant`, `TenantQuota`,
  `Snapshot`, `VMSet`, and `POST /vms/{id}/fork`.
- **Gateway**: per-VM routes (`exec`, `files`, `processes`, `terminal`,
  `logs`) reverse-proxied to the owning host. The VM's ID and URL stay the
  same when it moves; the gateway re-resolves. Later, VM access tokens let
  SDKs connect to hosts directly.
- **Scheduler**: synchronous, no queue (Fly). Filter: backend, host class,
  free vCPU/RAM/**disk**/netns slots, cordoned, zone and spread constraints,
  tenant pinning. Score: snapshot chunks already cached locally, then
  peer-local, then least-loaded spread across zones. Reserve → create. On a
  host error, retry on the next candidate (bounded), then fail with a typed
  `capacity_exhausted` error naming the constraint that couldn't be met.
- **Reconcilers** (one leader via a Postgres advisory lock):
  host liveness and fencing, `durable`/`restartable` recovery, `VMSet`
  convergence, checkpoint scheduling, timeouts, snapshot GC, orphan cleanup.
- **CA**: short-lived host certs from join tokens.
- **Store**: Postgres for clusters (HA via managed Postgres or Patroni);
  SQLite for single-node `flint-server`.

### 3. Host agent (today's daemon, Python)

Agent mode (`flint agent --join <token>`); standalone `flint start` stays.

- mTLS-only API, registration, heartbeats with capacity and cache contents,
  event push (buffered across control-plane outages).
- Snapshot operations delegate to `flint-snapd`: checkpoint, pause and fork
  produce a snapshot ID; create/resume/fork-child take a snapshot ID.
- Holds a **lease** per `durable` VM, renewed through heartbeats (fencing,
  below).
- Parameterised bridge, subnet and state dir (two agents on one box for CI).

## The scenarios

### One type of VM on many nodes

Two separate needs:

1. **Many independent VMs from one template** (the sandbox case). Templates
   are snapshots, so any host of the right host class can boot them. A
   template carries a **distribution policy**: `prefetch: all | labels |
   n_hosts`, plus a per-host warm pool size. `flint-snapd` prefetches chunks;
   the agent keeps the netns and disk pool warm. Placement prefers warm
   hosts, but any host can take a create; it just pays a fetch.
2. **N replicas of a VM that should stay up** (services, worker pools).
   New resource `VMSet`:
   ```yaml
   spec:
     replicas: 6
     template: worker
     availability: restartable
     spread: { topology: zone, max_skew: 1 }   # also: host
     standby: 1        # pre-restored, paused replicas for fast failover (Fly)
     update: { strategy: rolling, max_unavailable: 1 }
   ```
   The reconciler keeps `replicas` running across zones, replaces any that
   are `lost`, and rolls template changes in waves (feeds phase 4 rollouts).

### Fork when the node is out of space

`POST /vms/{id}/fork {count: n}`:

1. The source host **checkpoints in place**: pause, diff snapshot to
   `flint-snapd`, resume the source. Source downtime ≈ snapshot time
   (dirty pages only), not upload time.
2. The scheduler places the `n` children like any create from that snapshot.
   The source host scores highest (chunks local), but if it lacks
   CPU/RAM/disk/netns slots, children go to other hosts. Placement never
   depends on the source host having room.
3. Remote children fetch chunks from the **source host peer-to-peer** while
   the upload to object storage continues in the background, so cross-host
   forks don't wait on the object store. With lazy restore (v2) they start
   before the fetch completes.
4. Fork succeeds partially only if requested (`min_count`); otherwise it is
   all-or-nothing with capacity reserved up front.

The same flow covers **resume when the original host is full or gone**: a
paused VM is a snapshot in the object store, so resume is placement plus
restore on any matching host, preferring the one that still has it cached.

### Host crash

1. Heartbeats stop; after the lease TTL (default 15 s) the host is
   `unreachable`; no new placements.
2. **Fencing** before any recovery: the control plane needs to know the old
   VMs can no longer write (a `durable` VM resurrected twice = split-brain
   on its workspace). Two mechanisms, either is enough:
   - **Self-fencing**: an agent that can't renew its leases kills its own
     `durable` and `restartable` VMs at TTL. `ephemeral` VMs keep running,
     so a control-plane outage doesn't kill sandboxes.
   - **External fencing** (optional per cloud module): stop the instance
     through the cloud API.
   The control plane waits TTL + margin (or a fencing confirmation), then
   marks the host `down`.
3. Recovery by class: `ephemeral` → `lost`; `restartable` → recreate from
   template elsewhere; `durable` → restore from last checkpoint elsewhere,
   same VM ID, `vm.recovered` event with the checkpoint timestamp.
4. A host that comes back after being marked `down` rejoins empty: its
   local VMs are deleted, never resumed.

### Control-plane or database failure

- `flint-server` replicas are stateless; the LB drops a dead one. The
  reconciler leader lease moves to another replica.
- Postgres HA is the operator's choice (managed service, or Patroni in the
  cloud modules). During a Postgres outage the API fails new creates; running
  VMs and their per-VM calls through the gateway keep working from the
  gateway's cached VM → host map.
- Object store outage: creates from cached snapshots work; checkpoints queue
  on the host's local disk and upload when it returns.

### Planned maintenance

`flint host drain`: stop placements, then migrate every VM (live migration
on Cloud Hypervisor, snapshot migration on Firecracker), respecting `VMSet`
`max_unavailable`. Host upgrades in Terraform are cordon → drain → replace.

## Networking

- Per-host networking is unchanged. Moving a VM keeps its identity; its
  guest-side addresses are per-netns and identical on every host, so the
  guest doesn't notice. Its veth/egress IP changes (host-local NAT).
- VMs still never talk to each other, so v1 needs no overlay. `VMSet`
  members that need to talk to each other is the WireGuard overlay item,
  later.
- Hosts ↔ control plane and host ↔ host (peer chunk fetch) on a private
  network, mTLS with CA-issued certs. VMs can reach neither.

## Auth (minimum pulled in from phase 1)

| Credential | Used by | Can |
|------------|---------|-----|
| `admin` token | Operators, Terraform admin provider | Everything incl. hosts, join tokens |
| `service` token | Builder's backend, SDK | VMs, VMSets, templates, volumes for any (or listed) tenants |
| Host join token | New host, once | Obtain a host cert. Cannot call the API |
| Host cert (mTLS) | Host ↔ control plane, host ↔ host | Register, heartbeat, events, chunk fetch for snapshots it's been told to restore |

`tenant` required on everything in cluster mode. Snapshots inherit their
VM's tenant; a host only serves a peer chunks for a snapshot the control
plane assigned to that peer.

## Terraform: cloud-agnostic first

1. **Node package** (`deploy/`): installer + systemd units for
   `flint-server`, `flint-agent`, `flint-snapd`, configured from one env
   file. Works on any Linux host with KVM, without Terraform.
2. **`terraform/modules/flint-cloudinit`**: pure Terraform, no cloud
   provider. Inputs: role, control-plane URL, join token, Postgres DSN,
   object-store endpoint/bucket/credentials, zone, labels. Output:
   `user_data`.
3. **Thin cloud modules** (later): instances with KVM across ≥2 zones, a
   private network, a LB for the control plane, HA Postgres, a bucket, an
   autoscaling group for hosts, optional external fencing. Fed into layer 2.

First reference: `terraform/examples/byo` (existing machines + Postgres +
S3 credentials). Then one cloud example.

Capacity: when placement fails with `capacity_exhausted`, the control plane
emits a `cluster.capacity_low` event and (optionally) calls a scale-up
webhook. The cloud modules wire this to their autoscaling group.

## Milestones

Ordered so each step is usable on its own and HA isn't bolted on at the end.

### M0: prerequisites (host side)

- Check in the OpenAPI spec (phase 2, item 2): the contract between the Go
  control plane and the Python agent.
- `tenant` field; host agent token auth + TLS; parameterised bridge, subnet,
  state dir.

### M1: portable snapshots on one host

- `flint-snapd`: chunk format, manifests, local cache, object-store upload,
  eager restore.
- Templates, pause and a new single-host `fork` all go through it. Prove on
  one host that pause → delete local files → resume from the object store
  works, on both backends.

### M2: control plane and placement

- `flint-server`: store, tokens, CA, join, heartbeats, `flint host ...`.
- Scheduler + gateway; cluster-wide events; multiple replicas behind a LB
  from day one.

### M3: mobility

- Cross-host create, resume and fork from snapshots; peer chunk fetch;
  cache-aware scoring; template distribution policies.
- Host class detection and per-class template builds.

### M4: availability

- Leases, self-fencing, `down` detection, recovery per availability class,
  periodic checkpoints for `durable`.
- `VMSet` with spread, standbys and rolling updates.
- Drain via snapshot migration; live migration on Cloud Hypervisor.

### M5: Terraform

- Node package, `flint-cloudinit`, `byo` example, one cloud example with
  ≥2 zones and autoscaling.

### M6: performance and later

- Lazy restore (`userfaultfd` page server, chunk-backed disk).
- Block-level dirty tracking for disk diffs.
- Cloud-identity join; WireGuard overlay for `VMSet` members.

## Verification

Per `.claude/rules/testing.md`, against real hosts, both backends:

1. **Join**: token works once; host `ready` with correct capacity.
2. **Spread**: N creates land on both hosts; the existing sandbox e2e suite
   passes unchanged against `flint-server`.
3. **Fork under pressure**: fill host A, fork a VM on A; children run on B
   with the parent's files and processes; the source kept running.
4. **Resume elsewhere**: pause on A, cordon A, resume → runs on B with
   memory state intact.
5. **Host crash**: kill host B (power off, not a clean stop). `ephemeral`
   → `lost`; `restartable` → running on A; `durable` → running on A from its
   last checkpoint, same ID; workspace files written before the crash are
   present. The old VMs never come back when B returns.
6. **Control-plane failover**: kill a `flint-server` replica mid-traffic;
   exec and PTY sessions through the other replica keep working; reconcilers
   resume.
7. **VMSet**: replicas spread across zones; killing a host restores the
   count; a template change rolls with `max_unavailable` respected.
8. **Isolation**: host certs can't call the service API; a host can't fetch
   chunks of a snapshot it wasn't assigned; VMs can't reach agents,
   `flint-snapd` or the control plane.

CI: two agents on one KVM runner via parameterised bridges/subnets (flows
1–6); nightly on the `byo` example across real machines (all flows).

## Decisions (defaults taken October 2026)

1. **Datastore**: Postgres for clusters, SQLite for a single node.
2. **Connection direction**: the control plane dials hosts over mTLS. The
   RPC layer stays transport-agnostic so hosts can dial out later.
3. **Default availability class**: `ephemeral`.
4. **Snapshot portability**: snapshots restore only on hosts of the same
   host class; pools are uniform.
5. **Fencing**: watchdog-backed self-fencing (below); external fencing is an
   optional extra per cloud module.
6. **Reference cloud**: decided when layer 2 of the Terraform work ships.
7. **Host agent language**: Python agent + Go `flint-snapd` for now.

## HA hardening (from Proxmox HA, vSphere HA, Pacemaker, Masakari)

- **Watchdog-backed self-fencing** (Proxmox): the agent arms a watchdog
  (`/dev/watchdog`, softdog by default) only while it holds leases for
  `durable`/`restartable` VMs, and pets it only after renewing them. A hung
  agent can't keep fenced VMs alive: the watchdog kills the VM processes (or
  resets the host where a hardware watchdog is configured) at lease TTL.
  Where no watchdog device exists, a separate supervisor process does the
  same with `kill -9`.
- **Second liveness channel** (vSphere datastore heartbeat): hosts also write
  a heartbeat object to the object store. A host missing from the network
  but still writing heartbeats is `isolated`, not `down`: no recovery until
  its leases have expired and it has self-fenced.
- **Local restart** (Masakari process/instance monitor): a VM whose VMM
  process dies on a healthy host is restarted on that host from its last
  checkpoint (`durable`) or template (`restartable`) without involving
  placement.
- **Isolation response per class** (vSphere): `ephemeral` VMs keep running
  when a host loses the control plane; `durable`/`restartable` are fenced at
  lease expiry.
