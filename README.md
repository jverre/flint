<p align="center">
  <h1 align="center">Flint</h1>
  <p align="center">An open-source VM orchestrator for running your users' untrusted code, with strict per-tenant isolation by default</p>
</p>

---

**Flint** is an open-source VM orchestrator for teams **running their users' untrusted code**. It sits behind your backend and keeps every user's VMs strictly isolated from everyone else's by default.

You drive it through a typed SDK, a CLI, Terraform, an MCP server for AI agents, and a web dashboard. All of them come from one small API that your backend calls with a service credential.

Flint doesn't try to manage your users. It focuses on what an orchestrator should guarantee: the boundaries around the VMs. Each tenant is an isolation group with:

- its own network, with no traffic across tenants;
- its own storage, wiped on delete;
- quotas and limits.

Every VM is jailed from the host and from the control plane. Outbound traffic is allowlisted, and credentials are injected by a proxy, so the VM never sees them. When users or agents need a direct connection (terminal, files, exposed ports), your backend gets a short-lived token limited to that one VM and hands it over.

Under the hood, the hypervisor is swappable: Cloud Hypervisor, Firecracker or macOS Virtualization.framework. Snapshots make VMs fast to start, fork and pause.

Observability is built in, and every event is tagged with its tenant:

- a timeline for every VM;
- exec and outbound-network events;
- an audit trail;
- traces that continue from the caller's code into the VM.

Your app can show each user exactly their own data, and export everything to standard tools.

Flint starts as a single command on a laptop and grows into a multi-host cluster, with a fleet-wide admin view and safe rollouts for the team running it.

https://github.com/user-attachments/assets/5fdbf10e-7e7a-4688-9414-5bde4d4ed428
