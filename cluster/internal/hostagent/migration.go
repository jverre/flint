package hostagent

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/jverre/flint/cluster/internal/model"
)

// Live migration (src/flint/cluster/live_migration.py): the receiver is
// prepared, the sender streams memory to it, the receiver reports the
// cutover, and the control plane commits or rolls back.

// MigrationAction is one step of the live migration protocol.
type MigrationAction string

const (
	MigratePrepare  MigrationAction = "prepare"  // receiver: get ready to receive
	MigrateSend     MigrationAction = "send"     // sender: stream to the receiver
	MigrateStatus   MigrationAction = "status"   // either side: where it is
	MigrateCommit   MigrationAction = "commit"   // receiver: take over for good
	MigrateAbort    MigrationAction = "abort"    // either side: stop and fence its copy
	MigrateRollback MigrationAction = "rollback" // sender: resume the source
)

// MigrationPhase is where one side of a live migration is.
type MigrationPhase string

const (
	PhaseReceiving MigrationPhase = "receiving"
	PhaseCutover   MigrationPhase = "cutover"
	PhaseReceived  MigrationPhase = "received" // receiver has the VM
	PhaseSent      MigrationPhase = "sent"     // sender handed it over
	PhaseAbsent    MigrationPhase = "absent"   // no migration state for the VM
)

// PrepareRequest asks the receiving host to get ready for a VM.
type PrepareRequest struct {
	SourceHost   string             `json:"source_host"`
	PoolID       json.RawMessage    `json:"pool_id"`
	VNI          json.RawMessage    `json:"vni"`
	RootDisk     json.RawMessage    `json:"root_disk"`
	Network      json.RawMessage    `json:"network"`
	Resources    model.Resources    `json:"resources"`
	TemplateID   string             `json:"template_id"`
	Tenant       string             `json:"tenant"`
	Internet     bool               `json:"internet"`
	Availability model.Availability `json:"availability"`
}

// NewPrepareRequest describes v, moving from source, to its receiver.
func NewPrepareRequest(v *model.VM, source string) PrepareRequest {
	m := v.Maintenance()
	var disk struct {
		PoolID json.RawMessage `json:"pool_id"`
		VNI    json.RawMessage `json:"vni"`
	}
	json.Unmarshal(m.RootDisk, &disk)
	return PrepareRequest{SourceHost: source, PoolID: orNull(disk.PoolID), VNI: orNull(disk.VNI), RootDisk: m.RootDisk, Network: m.Network,
		Resources: v.Resources(), TemplateID: v.TemplateID, Tenant: v.Tenant, Internet: v.Spec.AllowInternet, Availability: v.Availability}
}

func orNull(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 {
		return json.RawMessage("null")
	}
	return raw
}

// MigrationStatus is one side's answer to MigrateStatus.
type MigrationStatus struct {
	Phase    MigrationPhase `json:"phase"`
	VMMState VMState        `json:"vmm_state"`
}

// AbortResult says whether the receiver ever ran the VM (so its disk
// writes may be ahead of the source's memory).
type AbortResult struct {
	Executed bool `json:"executed"`
}

// Migrate makes one live-migration call on h.
func (c *Client) Migrate(ctx context.Context, h *model.Host, vmID string, action MigrationAction, in, out any) error {
	if in == nil {
		in = map[string]any{}
	}
	return c.Do(ctx, h, http.MethodPost, "/vms/"+vmID+"/live-migration/"+string(action), in, out, 16*time.Minute)
}

// ── Reports hosts send the control plane ──────────────────────────────────

// ReportedVM is a VM as a host reports it in a heartbeat.
type ReportedVM struct {
	Resources       map[string]any `json:"resources"`
	Maintenance     map[string]any `json:"maintenance"`
	ID              string         `json:"vm_id"`
	State           VMState        `json:"state"`
	PauseSnapshotID string         `json:"pause_snapshot_id,omitempty"`
}

// Heartbeat renews a host's lease and reports its inventory.
type Heartbeat struct {
	HandoverToken string               `json:"handover_token"`
	PatchState    map[string]any       `json:"patch_state"`
	Version       string               `json:"version"`
	Backend       string               `json:"backend"`
	HostClass     string               `json:"host_class"`
	Capacity      model.Capacity       `json:"capacity"`
	VMs           []ReportedVM         `json:"vms"`
	Cached        []string             `json:"cached_snapshots"`
	Templates     []model.HostTemplate `json:"templates"`
}
