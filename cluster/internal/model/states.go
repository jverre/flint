// Package model is the control plane's vocabulary: the records the catalog
// keeps and the named states they move through. It has no behaviour beyond
// small pure helpers, so every other package can share it.
package model

import (
	"regexp"
	"strings"
	"time"
)

// NowMs is the catalog's clock: unix milliseconds.
func NowMs() int64 { return time.Now().UnixMilli() }

// VMState is a VM's state in the catalog.
type VMState string

const (
	VMPending    VMState = "pending"    // placed, host is creating it
	VMRunning    VMState = "running"    // running on host_id
	VMPaused     VMState = "paused"     // pause_id holds its snapshot; host_id has it cached
	VMMigrating  VMState = "migrating"  // moving to another host
	VMRecovering VMState = "recovering" // its host failed; being restored elsewhere
	VMLost       VMState = "lost"       // its host failed and it can't be recovered (ephemeral)
	VMError      VMState = "error"
)

// APIName is the state as the daemon's API names it (Started, Paused, ...),
// so the SDK works against either.
func (s VMState) APIName() string {
	switch s {
	case VMPending:
		return "Starting"
	case VMRunning:
		return "Started"
	case VMPaused:
		return "Paused"
	case VMMigrating:
		return "Migrating"
	case VMRecovering:
		return "Recovering"
	case VMLost:
		return "Lost"
	default:
		return "Error"
	}
}

// ActiveVMStates use a host's CPU and memory (paused VMs don't).
var ActiveVMStates = []VMState{VMPending, VMRunning, VMMigrating, VMRecovering}

// Active reports whether a VM in this state uses its host's CPU and memory.
func (s VMState) Active() bool {
	for _, a := range ActiveVMStates {
		if s == a {
			return true
		}
	}
	return false
}

// Availability is what happens to a VM when its host fails.
type Availability string

const (
	AvailEphemeral   Availability = "ephemeral"   // lost with its host
	AvailRestartable Availability = "restartable" // recreated fresh from its template
	AvailDurable     Availability = "durable"     // restored from its last checkpoint
)

func (a Availability) Valid() bool {
	return a == AvailEphemeral || a == AvailRestartable || a == AvailDurable
}

// Recoverable VMs are brought back elsewhere when their host fails; their
// host agent fences them when it loses its lease.
func (a Availability) Recoverable() bool { return a == AvailRestartable || a == AvailDurable }

// HostState is a host's state in the registry.
type HostState string

const (
	HostJoining     HostState = "joining" // joined, first heartbeat not in yet (e.g. building its golden snapshot)
	HostReady       HostState = "ready"
	HostUnreachable HostState = "unreachable" // missed heartbeats, lease not yet expired
	HostIsolated    HostState = "isolated"    // no heartbeats, but still writing storage heartbeats
	HostDown        HostState = "down"
	HostDraining    HostState = "draining"
	HostDrained     HostState = "drained"
)

// Reachable reports whether the control plane can talk to a host in this state.
func (s HostState) Reachable() bool {
	return s != HostDown && s != HostIsolated && s != HostUnreachable
}

// TemplateStatus is where a template's build is.
type TemplateStatus string

const (
	TemplateBuilding TemplateStatus = "building"
	TemplateReady    TemplateStatus = "ready"
	TemplateError    TemplateStatus = "error"
)

// DefaultTemplate is the golden image every host has.
const DefaultTemplate = "default"

// SnapshotKind is why a snapshot was taken.
type SnapshotKind string

const (
	SnapshotPause      SnapshotKind = "pause"
	SnapshotFork       SnapshotKind = "fork"
	SnapshotCheckpoint SnapshotKind = "checkpoint"
	SnapshotTemplate   SnapshotKind = "template"
)

// TimeoutPolicy is what a host does to a VM whose timeout expires.
type TimeoutPolicy string

const (
	TimeoutKill  TimeoutPolicy = "kill"
	TimeoutPause TimeoutPolicy = "pause"
)

func (p TimeoutPolicy) Valid() bool { return p == TimeoutKill || p == TimeoutPause }

// Spread is the failure domain a VM set spreads its members across.
type Spread string

const (
	SpreadZone Spread = "zone"
	SpreadHost Spread = "host"
)

func (s Spread) Valid() bool { return s == SpreadZone || s == SpreadHost }

// Domain is the host's failure domain for this spread.
func (s Spread) Domain(h *Host) string {
	if s == SpreadZone {
		return h.Zone
	}
	return h.ID
}

// Role is a VM set member's role: active ("") or a paused standby.
type Role string

const (
	RoleActive  Role = ""
	RoleStandby Role = "standby"
)

// OpKind is the kind of change an operation makes to a VM.
type OpKind string

const (
	OpCreate     OpKind = "create"
	OpDelete     OpKind = "delete"
	OpPause      OpKind = "pause"
	OpResume     OpKind = "resume"
	OpFork       OpKind = "fork"
	OpCheckpoint OpKind = "checkpoint"
	OpMigrate    OpKind = "migrate"
	OpRecover    OpKind = "recover"
)

// OpState is an operation's outcome so far.
type OpState string

const (
	OpRunning   OpState = "running"
	OpSucceeded OpState = "succeeded"
	OpFailed    OpState = "failed"
)

// OpStep is how far an operation got. Each is recorded before the action it
// guards, so a takeover knows what may have happened.
type OpStep string

const (
	StepNone             OpStep = ""
	StepPlacing          OpStep = "placing"           // choosing a host and creating the VM there
	StepPausing          OpStep = "pausing"           // asked the host to pause and export
	StepPausedOnHost     OpStep = "paused_on_host"    // the host paused it; snapshot not yet recorded
	StepInPlace          OpStep = "in_place"          // resuming on the VM's own host
	StepRestoring        OpStep = "restoring"         // restoring its snapshot on another host
	StepCheckpointing    OpStep = "checkpointing"     // asked the host for a checkpoint
	StepCreatingChildren OpStep = "creating_children" // fork: creating the copies
	StepLivePreparing    OpStep = "live_preparing"    // live migration: preparing the receiver
	StepLiveTransferring OpStep = "live_transferring" // live migration: sending memory
	StepLiveCutover      OpStep = "live_cutover"      // live migration: waiting for the receiver to take over
)

// Live reports whether the step belongs to a live migration.
func (s OpStep) Live() bool { return strings.HasPrefix(string(s), "live_") }

var tenantRe = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$`)

// ValidTenant reports whether t can name a tenant.
func ValidTenant(t string) bool { return tenantRe.MatchString(t) }
