package model

import (
	"encoding/json"
	"errors"
	"strings"
)

// VM is a VM in the catalog.
type VM struct {
	ID           string            `json:"vm_id"`
	Tenant       string            `json:"tenant"`
	HostID       string            `json:"host_id"`
	State        VMState           `json:"cluster_state"`
	TemplateID   string            `json:"template_id"`
	Availability Availability      `json:"availability"`
	Spec         VMSpec            `json:"spec"`
	Labels       map[string]string `json:"labels"`
	VMSetID      string            `json:"vmset_id,omitempty"`
	Role         Role              `json:"role,omitempty"`
	CheckpointID string            `json:"checkpoint_id,omitempty"`
	CheckpointAt int64             `json:"checkpoint_at,omitempty"`
	PauseID      string            `json:"pause_id,omitempty"`
	Error        string            `json:"error,omitempty"`
	Info         map[string]any    `json:"-"` // last dict the host returned for it
	CreatedAt    int64             `json:"created_at_ms"`
	UpdatedAt    int64             `json:"updated_at_ms"`
}

// Resources is what a VM uses on its host.
type Resources struct {
	VCPUs    int   `json:"vcpus"`
	MemoryMB int64 `json:"memory_mb"`
	DiskMB   int64 `json:"disk_mb"`
}

// Known reports whether every resource has been reported.
func (r Resources) Known() bool { return r.VCPUs > 0 && r.MemoryMB > 0 && r.DiskMB > 0 }

// Resources is what the host last reported the VM uses (zero if unknown).
func (v *VM) Resources() Resources {
	var out Resources
	infoField(v.Info, "resources", &out)
	return out
}

// Maintenance is what a host reports about a VM's storage and networking,
// opaque to the control plane except to tell whether it can move live. The
// parts are passed back to hosts as they came.
type Maintenance struct {
	RootDisk json.RawMessage `json:"root_disk,omitempty"`
	Network  json.RawMessage `json:"network,omitempty"`
}

// SharedDisk reports whether the VM's root disk is on shared storage: such a
// VM moves by live migration.
func (m Maintenance) SharedDisk() bool { return !isNull(m.RootDisk) }

// Live reports whether the VM's disk and network can both follow it to
// another host without a pause.
func (m Maintenance) Live() bool { return m.SharedDisk() && !isNull(m.Network) }

// Maintenance is the VM's last reported maintenance state.
func (v *VM) Maintenance() Maintenance {
	var out Maintenance
	infoField(v.Info, "maintenance", &out)
	return out
}

func infoField(info map[string]any, key string, out any) {
	if raw, ok := info[key]; ok && raw != nil {
		if b, err := json.Marshal(raw); err == nil {
			_ = json.Unmarshal(b, out)
		}
	}
}

// Capacity is what a host reports in its heartbeat.
type Capacity struct {
	VMVCPUs         int   `json:"vm_vcpus"`
	VMMemoryMB      int64 `json:"vm_memory_mb"`
	ReserveVCPUs    int   `json:"reserve_vcpus"`
	ReserveMemoryMB int64 `json:"reserve_memory_mb"`
	ReserveDiskMB   int64 `json:"reserve_disk_mb"`
	MaxVMs          int   `json:"max_vms"`
	VCPUs           int   `json:"vcpus"`
	MemTotalMB      int64 `json:"mem_total_mb"`
	MemAvailableMB  int64 `json:"mem_available_mb"`
	DiskFreeMB      int64 `json:"disk_free_mb"`
	NetnsFree       int   `json:"netns_free"`
}

// HostTemplate is a template a host has locally.
type HostTemplate struct {
	ID         string         `json:"id"`
	Status     TemplateStatus `json:"status"`
	SnapshotID string         `json:"snapshot_id,omitempty"`
	Error      string         `json:"error,omitempty"`
}

// Host is a host in the registry.
type Host struct {
	HandoverToken string            `json:"-"`
	HandoverUntil int64             `json:"handover_until_ms"`
	PatchState    map[string]any    `json:"patch_state"`
	ID            string            `json:"id"`
	Name          string            `json:"name"`
	AgentURL      string            `json:"agent_url"`
	SnapdURL      string            `json:"snapd_url"`
	Zone          string            `json:"zone"`
	Labels        map[string]string `json:"labels"`
	Backend       string            `json:"backend"`
	HostClass     string            `json:"host_class"`
	State         HostState         `json:"state"`
	Cordoned      bool              `json:"cordoned"`
	Capacity      Capacity          `json:"capacity"`
	Cached        []string          `json:"cached_snapshots"`
	Templates     []HostTemplate    `json:"templates"`
	Version       string            `json:"version"`
	LastHeartbeat int64             `json:"last_heartbeat_ms"`
	StateSince    int64             `json:"state_since_ms"`
	// MaxVMs, if set by an operator, caps VMs below what the host reports.
	MaxVMs    int   `json:"max_vms_override"`
	CreatedAt int64 `json:"created_at_ms"`
}

// EffectiveMaxVMs is the host's VM cap: the operator's, if lower than what it reports.
func (h *Host) EffectiveMaxVMs() int {
	if h.MaxVMs > 0 && (h.Capacity.MaxVMs == 0 || h.MaxVMs < h.Capacity.MaxVMs) {
		return h.MaxVMs
	}
	return h.Capacity.MaxVMs
}

// HasTemplate reports whether the host has the template ready locally.
func (h *Host) HasTemplate(id string) bool {
	for _, t := range h.Templates {
		if t.ID == id && t.Status == TemplateReady {
			return true
		}
	}
	return false
}

// Caches reports whether the host has a snapshot's chunks locally.
func (h *Host) Caches(snapshotID string) bool {
	for _, id := range h.Cached {
		if id == snapshotID {
			return true
		}
	}
	return false
}

// InHandover reports whether the host's agent is being upgraded (snapd
// holds its lease and nothing may change its VMs).
func (h *Host) InHandover(now int64) bool { return h.HandoverToken != "" && h.HandoverUntil > now }

// Snapshot is the catalog entry for a snapshot in the object store.
type Snapshot struct {
	ID         string       `json:"id"`
	Kind       SnapshotKind `json:"kind"`
	VMID       string       `json:"vm_id,omitempty"`
	TemplateID string       `json:"template_id,omitempty"`
	Tenant     string       `json:"tenant,omitempty"`
	Backend    string       `json:"backend"`
	HostClass  string       `json:"host_class"`
	SourceHost string       `json:"source_host,omitempty"`
	Uploaded   bool         `json:"uploaded"`
	Bytes      int64        `json:"bytes"`
	CreatedAt  int64        `json:"created_at_ms"`
}

// Template is a cluster template; its artifacts are per-host-class snapshots.
type Template struct {
	ID         string         `json:"template_id"`
	Name       string         `json:"name"`
	Tenant     string         `json:"tenant,omitempty"`
	Dockerfile string         `json:"-"`
	RootfsMB   int            `json:"rootfs_size_mb"`
	Status     TemplateStatus `json:"status"`
	BuildHost  string         `json:"build_host,omitempty"`
	Error      string         `json:"error,omitempty"`
	CreatedAt  int64          `json:"created_at_ms"`
	UpdatedAt  int64          `json:"updated_at_ms"`
}

// VMSetSpec keeps N replicas of a template running, spread across hosts or
// zones. Its members are made from the embedded VMParams, like POST /vms.
type VMSetSpec struct {
	Replicas int `json:"replicas"`
	VMParams
	// Spread: replicas are placed to keep the per-domain count within
	// MaxSkew of each other.
	Spread  Spread `json:"spread"`
	MaxSkew int    `json:"max_skew"`
	// Standby: extra paused replicas kept ready. A lost replica is replaced by
	// resuming a standby (a snapshot restore) instead of a fresh create.
	Standby    int `json:"standby"`
	MaxUnavail int `json:"max_unavailable"`
}

// WithDefaults fills in what the request left out.
func (s *VMSetSpec) WithDefaults() {
	s.VMParams.WithDefaults(AvailRestartable)
	if s.Spread == "" {
		s.Spread = SpreadZone
	}
	if s.MaxSkew <= 0 {
		s.MaxSkew = 1
	}
	if s.MaxUnavail <= 0 {
		s.MaxUnavail = 1
	}
}

func (s *VMSetSpec) Validate() error {
	switch {
	case s.Replicas < 0 || s.Replicas > 1000:
		return errors.New("replicas must be between 0 and 1000")
	case s.Standby < 0 || s.Standby > 100:
		return errors.New("standby must be between 0 and 100")
	case !s.Spread.Valid():
		return errors.New("spread must be zone or host")
	}
	return s.VMParams.Validate()
}

type VMSet struct {
	ID         string    `json:"id"`
	Tenant     string    `json:"tenant"`
	Name       string    `json:"name"`
	Spec       VMSetSpec `json:"spec"`
	Generation int64     `json:"generation"`
	Deleted    bool      `json:"-"`
	CreatedAt  int64     `json:"created_at_ms"`
	UpdatedAt  int64     `json:"updated_at_ms"`
}

// Operation is the durable record of one change to a VM (engine/operations.go).
type Operation struct {
	ID        string   `json:"id"`
	VMID      string   `json:"vm_id"`
	Tenant    string   `json:"tenant"`
	Kind      OpKind   `json:"kind"`
	State     OpState  `json:"state"`
	Step      OpStep   `json:"step"`
	Params    OpParams `json:"params"`
	Holder    string   `json:"-"`
	Error     string   `json:"error,omitempty"`
	CreatedAt int64    `json:"created_at_ms"`
	UpdatedAt int64    `json:"updated_at_ms"`
}

// OpParams is what an operation touched, recorded so a takeover can finish
// or undo it.
type OpParams struct {
	Snapshot   string `json:"snapshot,omitempty"`
	SourceHost string `json:"source_host,omitempty"`
	TargetHost string `json:"target_host,omitempty"`
	FailedHost string `json:"failed_host,omitempty"`
	Reason     string `json:"reason,omitempty"`
	Children   string `json:"children,omitempty"` // comma-separated VM IDs
}

// Merge sets the fields that are set in o.
func (p *OpParams) Merge(o OpParams) {
	set := func(dst *string, v string) {
		if v != "" {
			*dst = v
		}
	}
	set(&p.Snapshot, o.Snapshot)
	set(&p.SourceHost, o.SourceHost)
	set(&p.TargetHost, o.TargetHost)
	set(&p.FailedHost, o.FailedHost)
	set(&p.Reason, o.Reason)
	set(&p.Children, o.Children)
}

// ChildIDs are the fork's children.
func (p OpParams) ChildIDs() []string {
	var out []string
	for _, id := range strings.Split(p.Children, ",") {
		if id != "" {
			out = append(out, id)
		}
	}
	return out
}

// DrainMove is where a drain will move one running VM.
type DrainMove struct {
	VMID      string    `json:"vm_id"`
	HostID    string    `json:"host_id"`
	Resources Resources `json:"resources"`
}
