# Plans

Working plans for turning Flint from a single-host microVM runtime into the
VM orchestrator described in the [README](../README.md).

| Plan | What it covers |
|------|----------------|
| [00-roadmap.md](00-roadmap.md) | Positioning, components, phases, where to start, open decisions |
| [01-tenant-isolation.md](01-tenant-isolation.md) | Threat model and the isolation work for a single host (phase 1) |
| [02-api-and-surfaces.md](02-api-and-surfaces.md) | Resource model, auth, events/observability, SDKs, MCP, Terraform, dashboard |
| [03-cluster.md](03-cluster.md) | Multi-host control plane, host agent, scheduling, cloud-agnostic Terraform deployment (phase 3) |
| [04-template-import.md](04-template-import.md) | Templates from admin-imported container images instead of Dockerfile builds (implemented) |

These are living documents: update them when a decision is made or a phase
ships, and delete sections that stop being true.
