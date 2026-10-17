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

Current state reflects the code at `83cf5a0`. "Verify" means we believe it but
have no test proving it.

| Boundary | Requirement | Current state |
|----------|-------------|---------------|
| **VM ↔ host** | VMM runs jailed: chroot, unprivileged uid, seccomp, cgroups, minimal devices | Done for both: Firecracker uses the jailer; Cloud Hypervisor runs in Flint's equivalent jail (`_ch_jail.py`, TAP passed as an fd). Golden-snapshot boots of tenant templates are jailed on both. Covered by `test_vmm_runs_unprivileged_in_its_jail` |
| **VM ↔ VM (cross-tenant)** | No packets between tenants' VMs | Done: bridge port isolation + per-netns ingress firewall; host drops bridge hairpin. Covered by `test_sandbox_cannot_reach_other_sandboxes_on_bridge` |
| **VM ↔ VM (same tenant)** | Allowed only when the tenant opts in | Not modelled |
| **VM ↔ host services** | VMs can reach only explicitly exposed host services | Done: host `FLINT-INPUT` drops everything from `br-flint` except the r2nfs port; netns egress policy blocks private ranges. Covered by `test_sandbox_cannot_reach_host_services`. r2nfs authorizes exports by veth IP, which is now unique and unspoofable (per-netns NAT + port isolation) |
| **VM ↔ another VM's proxy** | A VM can never use another VM's credential proxy | Done: proxy listens on `172.16.0.1` only, and the netns drops unsolicited veth ingress |
| **VM ↔ control plane** | Guest can't reach daemon API; guest agent only reachable from its own netns | Daemon binds `127.0.0.1:9100` (good). `flintd` listens on `:5000` inside the guest; reachable only through the netns, verify |
| **Egress** | Default deny or allowlist; always block host, link-local (`169.254.0.0/16`, cloud metadata), RFC1918 and the bridge subnet | Host, link-local, RFC1918, CGNAT, loopback and multicast blocked in every netns and again on the host (storage endpoint excepted). Still no per-domain allowlist for non-HTTP |
| **Secrets** | Injected by proxy, never in the guest, encrypted at rest, never returned by the API | Injection works. **Stored as plaintext JSON** in SQLite (`network_policy_json`) and **returned verbatim** by `GET /vms/{id}/network-policy` |
| **Storage** | No shared writable layers across tenants; rootfs, snapshots, pause state and volumes wiped on delete | Copy-on-write per VM. Delete is `rmtree`/`unlink` (no wipe, no proof). Volumes have no tenant |
| **Resources** | Per-VM CPU, memory, disk IO, network limits; per-tenant quotas (VM count, vCPU, RAM, disk) | Per-VM cgroups (Firecracker). No IO/network limits, no tenant quotas |
| **Telemetry** | Every event, log line and metric tagged with tenant; queries scoped by tenant | Events/logs have no tenant |

## Work items

### 1. Network isolation

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

- Encrypt `network_policy_json` at rest (key from config / KMS later).
- `GET /network-policy` returns rules with header values redacted.
- Never log policy bodies.
- Credential values become write-only in the SDK (`set_credentials` stays,
  no getter for values).

### 3. Service credential and tenant field

- Daemon listens on a Unix socket by default; TCP requires a token.
- Two credentials: `service` (create/manage VMs for any tenant) and `admin`
  (config, host operations). Stored hashed.
- `tenant` required on `POST /vms`, volumes and template builds when
  multi-tenant mode is on; defaults to `default` in `flint start --dev`.
- `GET /vms?tenant=` and all events carry `tenant`.

### 4. Storage

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

- Per-VM IO and network rate limits (both VMMs support rate limiters).
- Per-tenant quota config: max VMs, vCPU, RAM, disk; enforced at create.

### 7. Threat model doc

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
