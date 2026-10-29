// Package engine is what the control plane does to VMs and hosts: it places
// VMs (scheduler.go), changes them through durable, locked operations
// (operations.go, vmops.go, live_migration.go), folds host reports into the
// catalog (heartbeat.go) and, on the leader replica, runs the reconcilers
// that recover, drain, checkpoint and garbage-collect (reconcile.go).
//
// It knows nothing of HTTP beyond the status its errors map to.
package engine

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"time"

	"github.com/jverre/flint/cluster/internal/auth"
	"github.com/jverre/flint/cluster/internal/hostagent"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/snapstore"
	"github.com/jverre/flint/cluster/internal/store"
)

// Config is the engine's share of a replica's configuration.
type Config struct {
	LeaseTTL           time.Duration // host lease; a silent host is fenced after this
	FenceMargin        time.Duration // extra wait after lease expiry before recovering its VMs
	CheckpointInterval time.Duration // durable VMs
	ReplicaID          string
}

// Engine is one replica's VM engine. Its state is all in the store and the
// object store, so replicas share the work.
type Engine struct {
	cfg    Config
	store  *store.Store
	agents *hostagent.Client
	remote snapstore.Remote
}

func New(cfg Config, st *store.Store, agents *hostagent.Client, remote snapstore.Remote) *Engine {
	return &Engine{cfg: cfg, store: st, agents: agents, remote: remote}
}

// Emit records a cluster event. Payload keys follow the daemon's events
// (vm_id, vm, from, to, ...); type, ts and the actor are added.
func (e *Engine) Emit(ctx context.Context, typ, tenant, vmID, hostID string, payload map[string]any) {
	if payload == nil {
		payload = map[string]any{}
	}
	ts := time.Now()
	payload["type"] = typ
	payload["ts"] = float64(ts.UnixNano()) / 1e9
	if hostID != "" {
		payload["host_id"] = hostID
	}
	if tenant != "" {
		payload["tenant"] = tenant
	}
	actor, name := auth.ActorOf(ctx)
	payload["actor"] = actor
	if name != "" {
		payload["actor_name"] = name
	}
	if err := e.store.InsertEvent(ctx, store.Event{TS: ts.UnixMilli(), Type: typ, Tenant: tenant, VMID: vmID, HostID: hostID,
		Actor: actor, ActorName: name, Payload: payload}); err != nil {
		log.Printf("emit %s: %v", typ, err)
	}
}

// HostIfUsable returns a host if it can be talked to.
func (e *Engine) HostIfUsable(ctx context.Context, id string) (*model.Host, bool) {
	if id == "" {
		return nil, false
	}
	h, err := e.store.GetHost(ctx, id)
	if err != nil {
		return nil, false
	}
	return h, h.State.Reachable()
}

// HostUsable reports whether a host may take new VMs right now.
func (e *Engine) HostUsable(h *model.Host) bool {
	return h.State == model.HostReady && model.NowMs()-h.LastHeartbeat < e.cfg.LeaseTTL.Milliseconds()
}

// ── Errors ────────────────────────────────────────────────────────────────

// CapacityError: no host can take a VM. The API answers 503.
type CapacityError struct{ Reason string }

func (e *CapacityError) Error() string            { return "capacity_exhausted: " + e.Reason }
func (e *CapacityError) HTTPError() (int, string) { return http.StatusServiceUnavailable, e.Error() }

// QuotaError: a tenant is at its limit. The API answers 429.
type QuotaError struct {
	Tenant          string
	Max, Used, Want int
}

func (e *QuotaError) Error() string {
	return fmt.Sprintf("tenant %s is at its quota: %d of %d VMs, %d more requested", e.Tenant, e.Used, e.Max, e.Want)
}
func (e *QuotaError) HTTPError() (int, string) { return http.StatusTooManyRequests, e.Error() }

var (
	errVMNotFound      = httpx.NotFound("VM not found")
	errHostUnreachable = httpx.Unavailable("the VM's host is unreachable")
)

func isNotFoundErr(err error) bool {
	var he *httpx.Error
	return errors.As(err, &he) && he.Status == http.StatusNotFound
}

func short(id string) string {
	if len(id) > 8 {
		return id[:8]
	}
	return id
}

// logErr logs a failure of a best-effort step that can't fail its caller.
func logErr(what string, err error) {
	if err != nil {
		log.Printf("%s: %v", what, err)
	}
}
