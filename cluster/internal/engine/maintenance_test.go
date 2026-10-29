package engine

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func TestDrainProvesWholeHostFitsAndReservesDestinations(t *testing.T) {
	e, _ := testEngine(t)
	ctx := context.Background()
	addHost(t, e, "source", "z", func(h *model.Host) { h.Capacity.VCPUs = 8 })
	addHost(t, e, "target", "z", func(h *model.Host) {
		h.Capacity.VCPUs = 4
		h.Capacity.MaxVMs = 2
		h.Capacity.MemAvailableMB = 1024
		h.Capacity.DiskFreeMB = 4096
	})
	for _, id := range []string{"one", "two", "three"} {
		v := &model.VM{ID: id, HostID: "source", Tenant: "t", State: model.VMRunning, TemplateID: model.DefaultTemplate,
			Info: map[string]any{"resources": model.Resources{VCPUs: 1, MemoryMB: 256, DiskMB: 1024}}}
		if err := e.store.InsertVM(ctx, v); err != nil {
			t.Fatal(err)
		}
	}
	src, _ := e.store.GetHost(ctx, "source")
	if _, err := e.PlanDrain(ctx, src); err == nil {
		t.Fatal("three VMs fit into two slots")
	}
	got, _ := e.store.GetHost(ctx, "source")
	if got.Cordoned {
		t.Fatal("failed preflight changed source")
	}
	e.store.DeleteVM(ctx, "three")
	_, plan, err := e.DrainHost(ctx, "source")
	if err != nil {
		t.Fatal(err)
	}
	if len(plan) != 2 {
		t.Fatalf("plan: %+v", plan)
	}
	if got, _ := e.store.GetHost(ctx, "source"); !got.Cordoned || got.State != model.HostDraining {
		t.Fatalf("source not draining: %+v", got)
	}
	if _, err := e.Candidates(ctx, PlaceRequest{}); err == nil {
		t.Fatal("unrelated placement consumed drain slots")
	}
	if _, err := e.Candidates(ctx, PlaceRequest{ReservationVM: "one", Resources: model.Resources{VCPUs: 1, MemoryMB: 256, DiskMB: 1024}}); err != nil {
		t.Fatalf("own reservation unavailable: %v", err)
	}
}

func TestDrainRejectsUnknownResourcesAndIncompatibleHosts(t *testing.T) {
	e, _ := testEngine(t)
	ctx := context.Background()
	addHost(t, e, "s", "z")
	addHost(t, e, "other", "z", func(h *model.Host) { h.Capacity.VCPUs = 8; h.HostClass = "different" })
	v := &model.VM{ID: "vm", HostID: "s", Tenant: "t", State: model.VMRunning, TemplateID: model.DefaultTemplate}
	e.store.InsertVM(ctx, v)
	h, _ := e.store.GetHost(ctx, "s")
	if _, err := e.PlanDrain(ctx, h); err == nil || !strings.Contains(err.Error(), "unknown") {
		t.Fatalf("unknown resources: %v", err)
	}
	e.store.UpdateVM(ctx, v.ID, store.VMUpdate{Info: map[string]any{"resources": model.Resources{VCPUs: 1, MemoryMB: 128, DiskMB: 512}}})
	if _, err := e.PlanDrain(ctx, h); err == nil {
		t.Fatal("planned migration to incompatible host")
	}
}

// fakeAgents serves every host's agent; handle sees each call.
func fakeAgents(t *testing.T, e *Engine, handle http.HandlerFunc, hosts ...string) {
	t.Helper()
	fake := httptest.NewServer(handle)
	t.Cleanup(fake.Close)
	e.agents.HTTP = fake.Client()
	for _, id := range hosts {
		addHost(t, e, id, "z", func(h *model.Host) { h.AgentURL = fake.URL })
	}
}

func TestLiveMigrationLostReplyCommitsWithoutResumingSource(t *testing.T) {
	e, _ := testEngine(t)
	ctx := context.Background()
	var mu sync.Mutex
	var calls []string
	var prepared map[string]any
	fakeAgents(t, e, func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		calls = append(calls, r.Method+" "+r.URL.Path)
		mu.Unlock()
		switch r.URL.Path {
		case "/vms/vm/live-migration/prepare":
			json.NewDecoder(r.Body).Decode(&prepared)
			writeJSON(w, 200, map[string]any{"address": "10.1.0.2", "port": 1234, "vmm_version": "46.0"})
		case "/vms/vm/live-migration/send":
			writeJSON(w, 500, map[string]any{"detail": "lost sender reply"})
		case "/vms/vm/live-migration/status":
			// Both a successful receiver and a confirmed paused sender.
			writeJSON(w, 200, map[string]any{"phase": "received", "vmm_state": "Paused"})
		case "/vms/vm/live-migration/commit":
			writeJSON(w, 200, map[string]any{"vm": map[string]any{"vm_id": "vm"}})
		case "/vms/vm":
			writeJSON(w, 200, map[string]any{"ok": true})
		default:
			t.Errorf("unexpected action %s", r.URL.Path)
			writeJSON(w, 500, map[string]any{"detail": "unexpected"})
		}
	}, "source", "target")
	v := &model.VM{ID: "vm", HostID: "source", Tenant: "t", State: model.VMRunning, TemplateID: model.DefaultTemplate, Availability: model.AvailDurable,
		Spec: model.VMSpec{AllowInternet: true},
		Info: map[string]any{"resources": map[string]any{"vcpus": 1, "memory_mb": 128, "disk_mb": 256},
			"maintenance": map[string]any{"root_disk": map[string]any{"pool_id": "pool", "vni": 1}, "network": map[string]any{"gateway": "stable"}}}}
	if err := e.store.InsertVM(ctx, v); err != nil {
		t.Fatal(err)
	}
	moved, err := e.MigrateVM(ctx, v, "target", "test")
	if err != nil {
		t.Fatal(err)
	}
	if moved.HostID != "target" || moved.State != model.VMRunning {
		t.Fatalf("migration not committed: %+v", moved)
	}
	for _, call := range calls {
		if strings.Contains(call, "rollback") {
			t.Fatal("resumed source after completed cutover")
		}
	}
	// The receiver is told what the Python side reads (src/flint/cluster/live_migration.py).
	for k, want := range map[string]any{"source_host": "source", "pool_id": "pool", "vni": float64(1), "internet": true,
		"availability": "durable", "tenant": "t", "template_id": "default"} {
		if prepared[k] != want {
			t.Errorf("prepare %s = %v, want %v (body %v)", k, prepared[k], want, prepared)
		}
	}
	if fmt.Sprint(prepared["root_disk"]) != "map[pool_id:pool vni:1]" || fmt.Sprint(prepared["resources"]) != "map[disk_mb:256 memory_mb:128 vcpus:1]" {
		t.Errorf("prepare body: %v", prepared)
	}
}

func TestUnconfirmedReceiverDeletionNeverResumesSource(t *testing.T) {
	e, _ := testEngine(t)
	ctx := context.Background()
	resumed := false
	fakeAgents(t, e, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/rollback") {
			resumed = true
		}
		writeJSON(w, 503, map[string]any{"detail": "partitioned destination"})
	}, "source", "target")
	v := &model.VM{ID: "vm", HostID: "source", Tenant: "t", State: model.VMRunning, TemplateID: model.DefaultTemplate}
	e.store.InsertVM(ctx, v)
	run, err := e.beginOp(ctx, v.ID, v.Tenant, model.OpMigrate, 0)
	if err != nil {
		t.Fatal(err)
	}
	src, _ := e.store.GetHost(ctx, "source")
	dst, _ := e.store.GetHost(ctx, "target")
	err = e.rollbackLive(run, v, src, dst, fmt.Errorf("transfer failed"))
	run.end(err)
	var retry retryOperation
	if !errors.As(err, &retry) {
		t.Fatalf("uncertainty was not retried: %v", err)
	}
	if resumed {
		t.Fatal("resumed source without confirming receiver deletion")
	}
	op, _ := e.store.GetOp(ctx, run.op.ID)
	if op.State != model.OpRunning {
		t.Fatalf("lost recovery intent: %s", op.State)
	}
}

func TestReceiverExecutionRequiresCoherentRecoveryNotSourceResume(t *testing.T) {
	e, _ := testEngine(t)
	ctx := context.Background()
	aborts := 0
	sourceResumed := false
	fakeAgents(t, e, func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/vms/vm/live-migration/abort":
			aborts++
			writeJSON(w, 200, map[string]any{"phase": "absent", "executed": true})
		case "/vms/vm/live-migration/rollback":
			sourceResumed = true
			writeJSON(w, 500, map[string]any{"detail": "unsafe source resume"})
		default:
			writeJSON(w, 500, map[string]any{"detail": "unexpected"})
		}
	}, "source", "target")
	src, _ := e.store.GetHost(ctx, "source")
	dst, _ := e.store.GetHost(ctx, "target")
	v := &model.VM{ID: "vm", HostID: "source", Tenant: "t", State: model.VMMigrating, TemplateID: model.DefaultTemplate,
		Availability: model.AvailDurable, CheckpointID: "coherent-checkpoint"}
	if err := e.store.InsertVM(ctx, v); err != nil {
		t.Fatal(err)
	}
	run, err := e.beginOp(ctx, v.ID, v.Tenant, model.OpMigrate, 0)
	if err != nil {
		t.Fatal(err)
	}
	err = e.rollbackLive(run, v, src, dst, fmt.Errorf("gateway activation failed after guest execution"))
	run.end(err)
	got, _ := e.store.GetVM(ctx, v.ID)
	if got.State != model.VMRecovering || got.CheckpointID != "coherent-checkpoint" {
		t.Fatalf("lost coherent recovery state: %+v", got)
	}
	if sourceResumed {
		t.Fatal("resumed stale source RAM after receiver wrote shared disk")
	}
	if aborts != 2 {
		t.Fatalf("both VMMs must be fenced before recovery, aborted %d", aborts)
	}
}
