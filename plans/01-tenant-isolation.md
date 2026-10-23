# Tenant isolation (phase 1)

Goal: one Linux host can safely run VMs for many tenants, where every VM runs
untrusted code. A tenant is an **isolation group**, not an identity: the
builder's backend passes `tenant: "<id>"` and Flint enforces the boundaries.

## Threat model

**Attacker**: arbitrary code running as root inside a VM, possibly an AI
agent acting on its own.

**Trusted**: the host, the daemon, and the builder's backend (holds the
service credential).

**Assets to protect**: other tenants' VMs, data, network traffic, secrets
and telemetry; the host; the control plane; fair share of host resources.

Out of scope for phase 1: hypervisor 0-days beyond what jailing/seccomp
mitigate, side channels between VMs on the same core, a malicious host operator.

## Boundaries

Current state reflects the Phase 1 stack (#40–#44, October 23, 2026).
`test_two_tenants_are_isolated` (in `tests/test_tenants.py`) checks the
boundaries end to end on both Linux backends. The published version of this
table is `docs/architecture/security.mdx`.

| Boundary | Requirement | Current state |
|----------|-------------|---------------|
| **VM ↔ host** | VMM runs jailed: chroot, unprivileged uid, seccomp, cgroups, minimal devices | Done for both: Firecracker uses the jailer; Cloud Hypervisor runs in Flint's equivalent jail (`_ch_jail.py`, TAP passed as an fd). Golden-snapshot boots of tenant templates are jailed on both. Covered by `test_vmm_runs_unprivileged_in_its_jail` |
| **VM ↔ VM (cross-tenant)** | No packets between tenants' VMs | Done: bridge port isolation + per-netns ingress firewall; host drops bridge hairpin. Covered by `test_sandbox_cannot_reach_other_sandboxes_on_bridge` |
| **VM ↔ VM (same tenant)** | Allowed only when the tenant opts in | Blocked, with no way to opt in yet (stricter than required). Covered by the two-tenant test |
| **VM ↔ host services** | VMs can reach only explicitly exposed host services | Done: host `FLINT-INPUT` drops everything from `br-flint` except the r2nfs port; netns egress policy blocks private ranges. Covered by `test_sandbox_cannot_reach_host_services`. r2nfs authorizes exports by veth IP, which is now unique and unspoofable (per-netns NAT + port isolation) |
| **VM ↔ another VM's proxy** | A VM can never use another VM's credential proxy | Done: proxy listens on `172.16.0.1` only, and the netns drops unsolicited veth ingress |
| **VM ↔ control plane** | Guest can't reach daemon API; guest agent only reachable from its own netns | Daemon listens on a Unix socket (its user and the `flint` group) and on `127.0.0.1:9100`, where every call needs an API token (#41). `flintd` is reachable only through the VM's netns: the bridge sweep in the two-tenant test finds no other namespace |
| **Egress** | Default deny or allowlist; always block host, link-local (`169.254.0.0/16`, cloud metadata), RFC1918 and the bridge subnet | Host, link-local, RFC1918, CGNAT, loopback and multicast blocked in every netns and again on the host (storage endpoint excepted). Still no per-domain allowlist for non-HTTP |
| **Secrets** | Injected by proxy, never in the guest, encrypted at rest, never returned by the API | Done on the single daemon (#40): sealed with Fernet (`secret.key` or `FLINT_SECRET_KEY`), redacted on every read, write-only (a redacted value sent back keeps the credential), dropped when the VM dies; the proxy's rules file lives in a 0700 tmpfs dir. The cluster's VM `spec` still holds policies in plaintext |
| **Storage** | No shared writable layers across tenants; rootfs, snapshots, pause state and volumes wiped on delete | Done (#42): private files are discarded (hole punched, or zeroed) before removal, `vm.wiped` reports it; r2nfs wipes a deleted image's overlay and collects its unshared chunks. Volumes have a tenant and mount only into its VMs (#41). Not a forensic erase; remote-store chunks stay. The cluster doesn't serve volumes yet (`501`) |
| **Resources** | Per-VM CPU, memory, disk IO, network limits; per-tenant quotas (VM count, vCPU, RAM, disk) | Per-VM cgroups and fixed VM sizes on both VMMs. The single daemon enforces per-tenant VM, vCPU, memory and volume-GiB quotas (#43); the cluster only a VM count. **No IO/network rate limits** |
| **Template builds** | A tenant's build can't reach the host, other tenants or private networks | **Not isolated**: `docker build` runs the Dockerfile on the host's Docker with the host's network (metadata included) and a shared BuildKit cache. Needs builds inside a VM, or `--network` restricted plus a builder per tenant |
| **Telemetry** | Every event, log line and metric tagged with tenant; queries scoped by tenant | Every single-daemon event carries `tenant` and `/events` sends a token only its tenants' (#41); per-VM logs, metrics and terminals are scoped by the VM's tenant. Events are still in memory only. The cluster stores events with `tenant` and filters `/v1/events?tenant=` |

## Work items

### 1. Network isolation

*Done except: same-tenant opt-in, the move to nftables, and r2nfs access
by something stronger than the source address.*

- Model networks per tenant: either a bridge per tenant, or keep one bridge
  and enforce isolation with nftables (per-veth ingress rules keyed on a
  tenant mark). Prefer **nftables on a single bridge + port isolation**
  (`bridge link set ... isolated on`): simpler, scales to many tenants, and
  same-tenant traffic can be opened later with explicit rules.
- Default egress rules inside each netns: drop to `10.0.0.0/24` (except the
  gateway for NAT), `169.254.0.0/16`, RFC1918, host addresses.
- Make the per-VM proxy bind only to the TAP side of the netns.
- `r2nfs`: export per VM with access tied to the VM's source address, or move
  volume access off the shared network (virtio-fs / block device).
- Move iptables calls to nftables while we're here; one ruleset per netns.

### 2. Secrets

*Done (#40) on the single daemon; the cluster spec is still plaintext.*

- Encrypt `network_policy_json` at rest (key from config / KMS later).
- `GET /network-policy` returns rules with header values redacted.
- Never log policy bodies.
- Credential values become write-only in the SDK (`set_credentials` stays,
  no getter for values).

### 3. Service credential and tenant field

*Done (#41). There is no separate multi-tenant mode: a missing `tenant`
means the token's only tenant, else `default`.*

- Daemon listens on a Unix socket by default; TCP requires a token.
- Two credentials: `service` (create/manage VMs for any tenant) and `admin`
  (config, host operations). Stored hashed.
- `tenant` required on `POST /vms`, volumes and template builds when
  multi-tenant mode is on; defaults to `default` in `flint start --dev`.
- `GET /vms?tenant=` and all events carry `tenant`.

### 4. Storage

*Done (#42): discard (hole punch, else zeros) rather than crypto-erase.*

- On delete: discard/zero rootfs and snapshot files before unlink (or
  per-VM dm-crypt/LUKS keys destroyed on delete: crypto-erase is cheaper and
  stronger; evaluate).
- Volumes gain `tenant`; attaching a volume to another tenant's VM is refused.
- Emit a `vm.wiped` event as proof.

### 5. Cloud Hypervisor jailing

Done: chroot per VM in the jailer's layout, unprivileged uid with
`no_new_privs`, own cgroup v2, seccomp, `/dev/kvm` + `/dev/urandom` only, TAP
passed as an fd. Cgroup *limits* are part of item 6.

### 6. Resource limits and quotas

*Quotas done (#43); rate limits not started.*

- Per-VM IO and network rate limits (both VMMs support rate limiters).
- Per-tenant quota config: max VMs, vCPU, RAM, disk; enforced at create.

### 7. Threat model doc

*Done (#44): `docs/architecture/security.mdx`.*

- Publish `docs/architecture/security.mdx` from this table once items 1–5
  land, with what is and isn't covered.

## Verification

Follow `.claude/rules/testing.md`: extend existing e2e flows rather than adding
regression tests. Add one **two-tenant isolation** flow that runs on both
Linux backends:

1. Create VM A (tenant `a`) and VM B (tenant `b`), both with internet.
2. From A: B's veth IP, B's proxy port, the bridge subnet, `10.0.0.1` (except
   allowed services), `169.254.169.254` and RFC1918 ranges are unreachable;
   public internet works.
3. Same-tenant pair: unreachable by default.
4. Set credentials on A; they're injected on A's egress, absent inside A,
   redacted in `GET /network-policy`, and not usable from B.
5. Delete A; its files are gone and `vm.wiped` was emitted.
6. Create with tenant `a` over quota → refused with a clear error.

A run where the VM fixtures skip is not a pass.
