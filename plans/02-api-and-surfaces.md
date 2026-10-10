# API and surfaces (phase 2+)

Goal: one API that every surface is generated from, so the SDKs, CLI, MCP,
Terraform and dashboard never drift.

## Users

| User | Wants | Main surfaces |
|------|-------|---------------|
| **Builder's backend** | Create/exec/destroy per tenant, reliably | TS/Python SDK, REST |
| **AI agents** | Fast create/fork, typed tools, narrow tokens | MCP, SDK |
| **Senior devs** | `ssh`/`exec`/logs that just work, traces into the VM | SDK, CLI, dashboard |
| **DevOps running Flint** | Fleet view, safe rollouts, drain hosts, alerts, everything in Terraform | Dashboard, Terraform, CLI |

## Resource model

Every resource has the same shape so surfaces can be generic:

```yaml
metadata: { id, tenant, labels, created_at, generation }
spec:     { ...desired state... }
status:   { phase, conditions, observed_generation, ...actual state... }
```

Resources: `VM`, `Template`, `Image`, `Volume`, `Snapshot`, `ExposedPort`,
`EgressPolicy`, `TenantQuota`; later `Host`, `Rollout`, `BackupPolicy`,
`TelemetryExport`, `Webhook`.

Both styles of API:

- **Imperative** (`create → exec → fork → delete`) for agents and SDKs.
- **Declarative** (`PUT spec`, reconcile, `watch`) for Terraform and GitOps.

## Auth

Flint does not model end users. Three kinds of credential:

| Credential | Held by | Can do |
|------------|---------|--------|
| `admin` | Operators | Everything, incl. hosts and config |
| `service` | Builder's backend | Manage VMs for any tenant (optionally pinned to a tenant list) |
| **VM access token** | End user / agent, minted by the backend via Flint | Direct connection to **one VM**: chosen actions (`exec`, `pty`, `files`, `port:<n>`), short TTL |

VM access tokens let heavy traffic (terminal, file transfer, streaming exec,
exposed ports) go straight to Flint without proxying through the builder's
backend. Make them attenuable (macaroon-style, see `superfly/macaroon`) so a
holder can narrow them further before handing them to an agent. Exposed ports
require a token unless explicitly public.

## Events and observability

Single event model: every state change and action is an event with `tenant`,
`vm_id`, `actor` (credential id), `reason`, timestamp.

Per VM:

1. **Lifecycle timeline** with per-phase timings (extend existing `timings`).
2. **Exec events**: command, exit code, duration, output tail.
3. **Logs**: serial console, processes, guest agent.
4. **Metrics** from the VMM (host side), optionally guest side.
5. **Egress log**: allowed/denied connections with domain.
6. **Traces**: SDK → API → host → guest. Pass `TRACEPARENT`/`OTEL_*` into
   `flintd` exec so code in the VM continues the caller's trace.

Storage and export:

- Persist events to an append-only table; audit log is the subset of events
  caused by API calls, hash-chained.
- Recent window in the product (ring buffer); everything else exported:
  OTLP (traces, logs), Prometheus (metrics), webhooks.
- Every query is scoped by tenant so the builder can show each user their
  own logs. Logs readable separately from exec (`logs.read` ≠ `exec`).
- Aggregate before fan-out: dashboards subscribe to per-second rollups, not
  one message per VM.

## Surfaces

### OpenAPI spec (source of truth)

Export FastAPI's spec, review it by hand, check it in, and fail CI on
unreviewed changes. Everything below is generated or validated against it.

### SDKs

- **Python**: exists; keep the E2B-style `Sandbox` ergonomics, move the
  transport onto the generated client.
- **TypeScript** (next): generated client + thin hand-written layer.

```ts
const vm = await flint.vms.create({ tenant: "user_123", template: "node", ttl: "2h" });
const run = await vm.exec("npm test", { stream: true });
const token = await vm.accessToken({ actions: ["pty"], ttl: "10m" });
```

### CLI

Keep current commands; add selectors (`--tenant`, `--label`), `-o json`,
`--dry-run`, `flint logs -f`, `flint events`, `flint top`.

### MCP server

Generated from the spec, authenticated with a (narrowed) token.

- Read tools: `list_vms`, `describe_vm`, `get_events`, `get_logs`,
  `get_metrics`.
- Write tools: `create_vm`, `exec`, `snapshot`, `restart`, separate permission.
- Destructive/bulk tools (`delete`, bulk exec, drain): require human approval
  (phase 4).
- Every MCP action is an audit event with the delegating credential.

### Terraform

Two providers (Coder pattern), phase 3:

- `flint`: workloads: `flint_vm`, `flint_template`, `flint_volume`,
  `flint_egress_policy`.
- `flint-admin`: `flint_tenant_quota`, `flint_service_credential`,
  `flint_telemetry_export`, `flint_webhook`.

### Dashboards

- **TUI** (exists): keep as the local/operator view; add tenant filter.
- **Web dashboard** (phase 3): fleet → tenant → VM → hosts → rollouts →
  audit. VM page = timeline + logs + metrics + terminal.
- Parked idea: a live fleet visualisation (each VM a cell in a grid/cube,
  coloured by state, version drift or egress denials; click → VM timeline),
  inspired by Modal's million-sandbox demo. Requires the rollup stream above.

## Order of work

1. Persist events with `tenant` + `actor`; timeline endpoint.
2. Check in the OpenAPI spec with `metadata/spec/status`.
3. VM access tokens; direct terminal/files/ports.
4. TypeScript SDK.
5. MCP server (read tools, then write tools).
6. OTLP/Prometheus export; trace propagation into exec.
