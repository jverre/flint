// Wire types (as returned by the daemon / control plane) and the normalised shapes pages use.
// Contract: flint-design/API-CONTRACT.md (daemon src/flint/daemon/server.py, cluster cluster/internal/server).

export type RawState = "Starting" | "Started" | "Running" | "Paused" | "Error" | "Dead" | "Migrating" | "Recovering" | "Lost" | string;

export interface RawVM {
  vm_id: string;
  state: RawState;
  cluster_state?: string;
  template_id?: string;
  backend_kind?: string;
  agent_healthy?: boolean;
  created_at?: number;
  boot_time_ms?: number | null;
  ready_time_ms?: number | null;
  timings?: Record<string, number>;
  log_lines?: string[];
  network_policy?: NetworkPolicy | null;
  timeout_seconds?: number | null;
  timeout_policy?: "kill" | "pause" | null;
  tenant?: string;
  availability?: string;
  host_id?: string;
  labels?: Record<string, string>;
  vmset_id?: string;
  role?: string;
  checkpoint_at?: number;
  error?: string;
  pid?: number;
}

export interface NetworkPolicy {
  allow?: Record<string, { transform?: { headers?: Record<string, string> }[] }[]>;
  [k: string]: unknown;
}

export type VMState = "running" | "paused" | "starting" | "error" | "stopped" | "migrating" | "recovering" | "lost";

export interface VM {
  id: string;
  shortId: string;
  /** labels.name in the cluster, else the short id. */
  name: string;
  hasName: boolean;
  tenant: string;
  template: string;
  state: VMState;
  host: string;
  backend: string;
  agentHealthy: boolean;
  createdAt: number | null; // unix seconds
  readyMs: number | null;
  timings: Record<string, number>;
  availability: string;
  labels: Record<string, string>;
  vmset: string;
  timeoutSeconds: number | null;
  timeoutPolicy: "kill" | "pause" | null;
  checkpointAt: number | null;
  error: string;
  networkPolicy: NetworkPolicy | null; // redacted by the server
  logLines: string[];
  raw: RawVM;
}

export interface Health {
  status: string;
  role?: string; // "control-plane" in the cluster
  backend_kind?: string;
  golden_snapshot_ready?: boolean;
  hosts?: number;
  hosts_ready?: number;
  leader?: boolean;
  replica?: string;
  storage_backend?: string;
  storage_healthy?: boolean;
  cluster_host_id?: string;
  network_policy?: boolean; // false: this daemon's backend can't apply one
}

export interface Platform {
  mode: "daemon" | "cluster";
  health: Health;
  /** cluster: probed via GET /v1/hosts (200 = admin). Daemon is always "local". */
  access: "local" | "admin" | "service";
  mock: boolean;
}

export interface Template {
  template_id: string;
  name: string;
  status: string; // building | ready | failed | error
  created_at?: number;
  tenant?: string;
  image?: string;
  digest?: string;
  build_host?: string;
  error?: string;
  rootfs_size_mb?: number;
  artifacts?: Record<string, { status: string; rootfs_size_mb?: number }>;
  host_classes?: string[];
}

export interface HostCapacity { max_vms: number; vcpus: number; mem_total_mb: number; mem_available_mb: number; disk_free_mb: number; netns_free: number }
export interface Host {
  id: string; name: string; zone: string; labels: Record<string, string>; backend: string; host_class: string;
  state: "joining" | "ready" | "unreachable" | "isolated" | "down" | "draining" | "drained" | string;
  cordoned: boolean; capacity: HostCapacity; cached_snapshots: string[];
  templates: { id: string; status: string; snapshot_id?: string; error?: string }[];
  version: string; last_heartbeat_ms: number; state_since_ms: number; max_vms_override: number; created_at_ms: number; active_vms: number;
}

export interface MetricSample { ts: number; cpu_percent: number; rss_bytes: number }

export interface Quota { tenant: string; max_vms: number; vms: number }
export interface Token { id: string; kind: "admin" | "service"; tenants: string[]; name: string; created_at: number }

export interface FlintEvent { type: string; ts: number; vm_id?: string; host_id?: string; tenant?: string; vm?: RawVM; from?: string; to?: string; [k: string]: unknown }

export interface ProcessInfo { pid: number; cmd?: string[] | string; tag?: string; pty?: boolean; running?: boolean; exit_code?: number | null; [k: string]: unknown }
export interface FileEntry { name: string; size: number; is_dir: boolean; mode: string }
