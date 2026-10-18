package server

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

// VM states in the catalog. The API reports them with the daemon's names
// (Started, Paused, ...) so the SDK works against either.
const (
	VMPending    = "pending"    // placed, host is creating it
	VMRunning    = "running"    // running on host_id
	VMPaused     = "paused"     // pause_id holds its snapshot; host_id has it cached
	VMMigrating  = "migrating"  // moving to another host
	VMRecovering = "recovering" // its host failed; being restored elsewhere
	VMLost       = "lost"       // its host failed and it can't be recovered (ephemeral)
	VMError      = "error"
)

// Availability classes.
const (
	AvailEphemeral   = "ephemeral"
	AvailRestartable = "restartable"
	AvailDurable     = "durable"
)

// Host states.
const (
	HostJoining     = "joining" // joined, first heartbeat not in yet (e.g. building its golden snapshot)
	HostReady       = "ready"
	HostUnreachable = "unreachable" // missed heartbeats, lease not yet expired
	HostIsolated    = "isolated"    // no heartbeats, but still writing storage heartbeats
	HostDown        = "down"
	HostDraining    = "draining"
	HostDrained     = "drained"
)

func nowMs() int64 { return time.Now().UnixMilli() }

// VMSpec is what the caller asked for; kept to recreate the VM elsewhere.
type VMSpec struct {
	AllowInternet bool            `json:"allow_internet_access"`
	NetworkPolicy json.RawMessage `json:"network_policy,omitempty"`
	TimeoutSecs   float64         `json:"timeout_seconds,omitempty"`
	TimeoutPolicy string          `json:"timeout_policy,omitempty"`
}

type VM struct {
	ID           string            `json:"vm_id"`
	Tenant       string            `json:"tenant"`
	HostID       string            `json:"host_id"`
	State        string            `json:"cluster_state"`
	TemplateID   string            `json:"template_id"`
	Availability string            `json:"availability"`
	Spec         VMSpec            `json:"spec"`
	Labels       map[string]string `json:"labels"`
	VMSetID      string            `json:"vmset_id,omitempty"`
	Role         string            `json:"role,omitempty"`
	CheckpointID string            `json:"checkpoint_id,omitempty"`
	CheckpointAt int64             `json:"checkpoint_at,omitempty"`
	PauseID      string            `json:"pause_id,omitempty"`
	Error        string            `json:"error,omitempty"`
	Info         map[string]any    `json:"-"` // last dict the host returned for it
	CreatedAt    int64             `json:"created_at_ms"`
	UpdatedAt    int64             `json:"updated_at_ms"`
}

const vmCols = `id, tenant, COALESCE(host_id, ''), state, template_id, availability, spec, labels, COALESCE(vmset_id, ''), role,
COALESCE(checkpoint_id, ''), checkpoint_at, COALESCE(pause_id, ''), error, info, created_at, updated_at`

type scanner interface{ Scan(dest ...any) error }

func scanVM(r scanner) (*VM, error) {
	var v VM
	var spec, labels, info string
	if err := r.Scan(&v.ID, &v.Tenant, &v.HostID, &v.State, &v.TemplateID, &v.Availability, &spec, &labels, &v.VMSetID, &v.Role,
		&v.CheckpointID, &v.CheckpointAt, &v.PauseID, &v.Error, &info, &v.CreatedAt, &v.UpdatedAt); err != nil {
		return nil, err
	}
	json.Unmarshal([]byte(spec), &v.Spec)
	json.Unmarshal([]byte(labels), &v.Labels)
	json.Unmarshal([]byte(info), &v.Info)
	return &v, nil
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func mustJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func (s *Server) insertVM(ctx context.Context, v *VM) error {
	now := nowMs()
	v.CreatedAt, v.UpdatedAt = now, now
	if v.Labels == nil {
		v.Labels = map[string]string{}
	}
	_, err := s.db.Exec(ctx, `INSERT INTO vms (id, tenant, host_id, state, template_id, availability, spec, labels, vmset_id, role,
		checkpoint_id, checkpoint_at, pause_id, error, info, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		v.ID, v.Tenant, nullable(v.HostID), v.State, v.TemplateID, v.Availability, mustJSON(v.Spec), mustJSON(v.Labels),
		nullable(v.VMSetID), v.Role, nullable(v.CheckpointID), v.CheckpointAt, nullable(v.PauseID), v.Error, mustJSON(v.Info), now, now)
	return err
}

func (s *Server) getVM(ctx context.Context, id string) (*VM, error) {
	return scanVM(s.db.QueryRow(ctx, `SELECT `+vmCols+` FROM vms WHERE id = ?`, id))
}

// listVMs returns VMs matching a WHERE clause ("" for all).
func (s *Server) listVMs(ctx context.Context, where string, args ...any) ([]*VM, error) {
	q := `SELECT ` + vmCols + ` FROM vms`
	if where != "" {
		q += ` WHERE ` + where
	}
	q += ` ORDER BY created_at`
	rows, err := s.db.Query(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*VM
	for rows.Next() {
		v, err := scanVM(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// vmUpdate is a partial update; nil fields are left alone.
type vmUpdate struct {
	HostID       *string
	State        *string
	CheckpointID *string
	CheckpointAt *int64
	PauseID      *string
	Error        *string
	Info         map[string]any
	Spec         *VMSpec
	Role         *string
}

func str(s string) *string { return &s }
func i64(n int64) *int64   { return &n }

func (s *Server) updateVM(ctx context.Context, id string, u vmUpdate) error {
	sets := []string{"updated_at = ?"}
	args := []any{nowMs()}
	if u.HostID != nil {
		sets = append(sets, "host_id = ?")
		args = append(args, nullable(*u.HostID))
	}
	if u.State != nil {
		sets = append(sets, "state = ?")
		args = append(args, *u.State)
	}
	if u.CheckpointID != nil {
		sets = append(sets, "checkpoint_id = ?")
		args = append(args, nullable(*u.CheckpointID))
	}
	if u.CheckpointAt != nil {
		sets = append(sets, "checkpoint_at = ?")
		args = append(args, *u.CheckpointAt)
	}
	if u.PauseID != nil {
		sets = append(sets, "pause_id = ?")
		args = append(args, nullable(*u.PauseID))
	}
	if u.Error != nil {
		sets = append(sets, "error = ?")
		args = append(args, *u.Error)
	}
	if u.Info != nil {
		sets = append(sets, "info = ?")
		args = append(args, mustJSON(u.Info))
	}
	if u.Spec != nil {
		sets = append(sets, "spec = ?")
		args = append(args, mustJSON(u.Spec))
	}
	if u.Role != nil {
		sets = append(sets, "role = ?")
		args = append(args, *u.Role)
	}
	args = append(args, id)
	_, err := s.db.Exec(ctx, `UPDATE vms SET `+strings.Join(sets, ", ")+` WHERE id = ?`, args...)
	return err
}

func (s *Server) deleteVMRow(ctx context.Context, id string) error {
	_, err := s.db.Exec(ctx, `DELETE FROM vms WHERE id = ?`, id)
	return err
}

// Capacity is what a host reports in its heartbeat.
type Capacity struct {
	MaxVMs         int   `json:"max_vms"`
	VCPUs          int   `json:"vcpus"`
	MemTotalMB     int64 `json:"mem_total_mb"`
	MemAvailableMB int64 `json:"mem_available_mb"`
	DiskFreeMB     int64 `json:"disk_free_mb"`
	NetnsFree      int   `json:"netns_free"`
}

// HostTemplate is a template a host has locally.
type HostTemplate struct {
	ID         string `json:"id"`
	Status     string `json:"status"`
	SnapshotID string `json:"snapshot_id,omitempty"`
	Error      string `json:"error,omitempty"`
}

type Host struct {
	ID            string            `json:"id"`
	Name          string            `json:"name"`
	AgentURL      string            `json:"agent_url"`
	SnapdURL      string            `json:"snapd_url"`
	Zone          string            `json:"zone"`
	Labels        map[string]string `json:"labels"`
	Backend       string            `json:"backend"`
	HostClass     string            `json:"host_class"`
	State         string            `json:"state"`
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

// maxVMs is the effective VM cap.
func (h *Host) maxVMs() int {
	if h.MaxVMs > 0 && (h.Capacity.MaxVMs == 0 || h.MaxVMs < h.Capacity.MaxVMs) {
		return h.MaxVMs
	}
	return h.Capacity.MaxVMs
}

const hostCols = `id, name, agent_url, snapd_url, zone, labels, backend, host_class, state, cordoned, capacity, cached, templates, version, last_heartbeat, state_since, max_vms, created_at`

func scanHost(r scanner) (*Host, error) {
	var h Host
	var labels, capacity, cached, templates string
	var cordoned int
	if err := r.Scan(&h.ID, &h.Name, &h.AgentURL, &h.SnapdURL, &h.Zone, &labels, &h.Backend, &h.HostClass, &h.State, &cordoned,
		&capacity, &cached, &templates, &h.Version, &h.LastHeartbeat, &h.StateSince, &h.MaxVMs, &h.CreatedAt); err != nil {
		return nil, err
	}
	h.Cordoned = cordoned != 0
	json.Unmarshal([]byte(labels), &h.Labels)
	json.Unmarshal([]byte(capacity), &h.Capacity)
	json.Unmarshal([]byte(cached), &h.Cached)
	json.Unmarshal([]byte(templates), &h.Templates)
	return &h, nil
}

func (s *Server) getHost(ctx context.Context, id string) (*Host, error) {
	return scanHost(s.db.QueryRow(ctx, `SELECT `+hostCols+` FROM hosts WHERE id = ?`, id))
}

func (s *Server) listHosts(ctx context.Context) ([]*Host, error) {
	rows, err := s.db.Query(ctx, `SELECT `+hostCols+` FROM hosts ORDER BY created_at`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Host
	for rows.Next() {
		h, err := scanHost(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, h)
	}
	return out, rows.Err()
}

func (s *Server) setHostState(ctx context.Context, id, state string) error {
	_, err := s.db.Exec(ctx, `UPDATE hosts SET state = ?, state_since = ? WHERE id = ? AND state <> ?`, state, nowMs(), id, state)
	return err
}

// Snapshot is the catalog entry for a snapshot in the object store.
type Snapshot struct {
	ID         string `json:"id"`
	Kind       string `json:"kind"`
	VMID       string `json:"vm_id,omitempty"`
	TemplateID string `json:"template_id,omitempty"`
	Tenant     string `json:"tenant,omitempty"`
	Backend    string `json:"backend"`
	HostClass  string `json:"host_class"`
	SourceHost string `json:"source_host,omitempty"`
	Uploaded   bool   `json:"uploaded"`
	Bytes      int64  `json:"bytes"`
	CreatedAt  int64  `json:"created_at_ms"`
}

const snapCols = `id, kind, COALESCE(vm_id, ''), COALESCE(template_id, ''), tenant, backend, host_class, COALESCE(source_host, ''), uploaded, bytes, created_at`

func scanSnapshot(r scanner) (*Snapshot, error) {
	var sn Snapshot
	var up int
	if err := r.Scan(&sn.ID, &sn.Kind, &sn.VMID, &sn.TemplateID, &sn.Tenant, &sn.Backend, &sn.HostClass, &sn.SourceHost, &up, &sn.Bytes, &sn.CreatedAt); err != nil {
		return nil, err
	}
	sn.Uploaded = up != 0
	return &sn, nil
}

func (s *Server) insertSnapshot(ctx context.Context, sn *Snapshot) error {
	if sn.CreatedAt == 0 {
		sn.CreatedAt = nowMs()
	}
	up := 0
	if sn.Uploaded {
		up = 1
	}
	_, err := s.db.Exec(ctx, `INSERT INTO snapshots (id, kind, vm_id, template_id, tenant, backend, host_class, source_host, uploaded, bytes, created_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET uploaded = excluded.uploaded, bytes = excluded.bytes`,
		sn.ID, sn.Kind, nullable(sn.VMID), nullable(sn.TemplateID), sn.Tenant, sn.Backend, sn.HostClass, nullable(sn.SourceHost), up, sn.Bytes, sn.CreatedAt)
	return err
}

func (s *Server) getSnapshot(ctx context.Context, id string) (*Snapshot, error) {
	return scanSnapshot(s.db.QueryRow(ctx, `SELECT `+snapCols+` FROM snapshots WHERE id = ?`, id))
}

func (s *Server) listSnapshots(ctx context.Context, where string, args ...any) ([]*Snapshot, error) {
	q := `SELECT ` + snapCols + ` FROM snapshots`
	if where != "" {
		q += ` WHERE ` + where
	}
	q += ` ORDER BY created_at`
	rows, err := s.db.Query(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Snapshot
	for rows.Next() {
		sn, err := scanSnapshot(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, sn)
	}
	return out, rows.Err()
}

// Template is a cluster template; its artifacts are per-host-class snapshots.
type Template struct {
	ID         string `json:"template_id"`
	Name       string `json:"name"`
	Tenant     string `json:"tenant,omitempty"`
	Dockerfile string `json:"-"`
	RootfsMB   int    `json:"rootfs_size_mb"`
	Status     string `json:"status"`
	BuildHost  string `json:"build_host,omitempty"`
	Error      string `json:"error,omitempty"`
	CreatedAt  int64  `json:"created_at_ms"`
	UpdatedAt  int64  `json:"updated_at_ms"`
}

func (s *Server) getTemplate(ctx context.Context, id string) (*Template, error) {
	var t Template
	var bh sql.NullString
	err := s.db.QueryRow(ctx, `SELECT id, name, tenant, dockerfile, rootfs_mb, status, build_host, error, created_at, updated_at FROM templates WHERE id = ?`, id).
		Scan(&t.ID, &t.Name, &t.Tenant, &t.Dockerfile, &t.RootfsMB, &t.Status, &bh, &t.Error, &t.CreatedAt, &t.UpdatedAt)
	t.BuildHost = bh.String
	return &t, err
}

func (s *Server) listTemplates(ctx context.Context) ([]*Template, error) {
	rows, err := s.db.Query(ctx, `SELECT id, name, tenant, dockerfile, rootfs_mb, status, build_host, error, created_at, updated_at FROM templates ORDER BY created_at`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Template
	for rows.Next() {
		var t Template
		var bh sql.NullString
		if err := rows.Scan(&t.ID, &t.Name, &t.Tenant, &t.Dockerfile, &t.RootfsMB, &t.Status, &bh, &t.Error, &t.CreatedAt, &t.UpdatedAt); err != nil {
			return nil, err
		}
		t.BuildHost = bh.String
		out = append(out, &t)
	}
	return out, rows.Err()
}

// VMSetSpec keeps N replicas of a template running, spread across hosts or zones.
type VMSetSpec struct {
	Replicas     int    `json:"replicas"`
	TemplateID   string `json:"template_id"`
	Availability string `json:"availability"`
	// Spread: "zone" or "host"; replicas are placed to keep the per-domain
	// count within MaxSkew of each other.
	Spread  string `json:"spread"`
	MaxSkew int    `json:"max_skew"`
	// Standby: extra paused replicas kept ready. A lost replica is replaced by
	// resuming a standby (a snapshot restore) instead of a fresh create.
	Standby       int    `json:"standby"`
	AllowInternet *bool  `json:"allow_internet_access,omitempty"`
	MaxUnavail    int    `json:"max_unavailable"`
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

func scanVMSet(r scanner) (*VMSet, error) {
	var vs VMSet
	var spec string
	var del int
	if err := r.Scan(&vs.ID, &vs.Tenant, &vs.Name, &spec, &vs.Generation, &del, &vs.CreatedAt, &vs.UpdatedAt); err != nil {
		return nil, err
	}
	vs.Deleted = del != 0
	json.Unmarshal([]byte(spec), &vs.Spec)
	return &vs, nil
}

const vmsetCols = `id, tenant, name, spec, generation, deleted, created_at, updated_at`

func (s *Server) getVMSet(ctx context.Context, id string) (*VMSet, error) {
	return scanVMSet(s.db.QueryRow(ctx, `SELECT `+vmsetCols+` FROM vmsets WHERE id = ?`, id))
}

func (s *Server) listVMSets(ctx context.Context, includeDeleted bool) ([]*VMSet, error) {
	q := `SELECT ` + vmsetCols + ` FROM vmsets`
	if !includeDeleted {
		q += ` WHERE deleted = 0`
	}
	rows, err := s.db.Query(ctx, q+` ORDER BY created_at`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*VMSet
	for rows.Next() {
		vs, err := scanVMSet(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, vs)
	}
	return out, rows.Err()
}

func isNotFound(err error) bool { return errors.Is(err, sql.ErrNoRows) }
