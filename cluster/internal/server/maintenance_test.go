package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHandoverIsBoundedAndCannotAdoptOrReviveVMs(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	addHost(t, s, "h", "z")
	v := &VM{ID: "vm", HostID: "h", Tenant: "t", State: VMRunning, Availability: AvailDurable, TemplateID: "default"}
	if err := s.insertVM(ctx, v); err != nil {
		t.Fatal(err)
	}
	token := strings.Repeat("a", 32)
	call := func(ids []string, token string) *httptest.ResponseRecorder {
		t.Helper()
		h, err := s.getHost(ctx, "h")
		if err != nil {
			t.Fatal(err)
		}
		b, _ := json.Marshal(map[string]any{"token": token, "vms": ids})
		w := httptest.NewRecorder()
		s.handleHandover(w, httptest.NewRequest("POST", "/", bytes.NewReader(b)), h)
		return w
	}
	if w := call(nil, token); w.Code != 409 {
		t.Fatalf("incomplete frozen set: %d %s", w.Code, w.Body)
	}
	if w := call([]string{"vm"}, token); w.Code != 200 {
		t.Fatalf("begin: %d %s", w.Code, w.Body)
	}
	first, _ := s.getHost(ctx, "h")
	if first.HandoverUntil > nowMs()+maxHandover.Milliseconds() {
		t.Fatal("unbounded handover")
	}
	if w := call([]string{"vm"}, token); w.Code != 200 {
		t.Fatalf("renew: %d %s", w.Code, w.Body)
	}
	second, _ := s.getHost(ctx, "h")
	if first.HandoverUntil != second.HandoverUntil {
		t.Fatal("renewal extended upgrade window")
	}
	if w := call([]string{"vm"}, strings.Repeat("b", 32)); w.Code != 409 {
		t.Fatal("another owner took handover")
	}
	if _, err := s.candidates(ctx, placeReq{}); err == nil {
		t.Fatal("placement during handover")
	}
	s.db.Exec(ctx, `UPDATE hosts SET last_heartbeat = 0 WHERE id = ?`, "h")
	if w := call([]string{"vm"}, token); w.Code != 409 {
		t.Fatal("revived expired host lease")
	}
}

func TestHandoverClaimRequiredForHeartbeat(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	addHost(t, s, "h", "z")
	token := strings.Repeat("a", 32)
	s.db.Exec(ctx, `UPDATE hosts SET handover_token = ?, handover_until = ? WHERE id = ?`, token, nowMs()+10000, "h")
	for _, tc := range []struct {
		token  string
		status int
	}{{"", 409}, {"wrong", 409}, {token, 200}} {
		h, _ := s.getHost(ctx, "h")
		b, _ := json.Marshal(heartbeatReq{HandoverToken: tc.token, PatchState: map[string]any{"running_kernel": "approved"}})
		w := httptest.NewRecorder()
		s.handleHeartbeat(w, httptest.NewRequest("POST", "/", bytes.NewReader(b)), h)
		if w.Code != tc.status {
			t.Fatalf("heartbeat got %d: %s", w.Code, w.Body)
		}
	}
	h, _ := s.getHost(ctx, "h")
	if h.HandoverToken != "" || h.PatchState["running_kernel"] != "approved" {
		t.Fatal("claim or inventory not persisted")
	}
}

func TestDrainProvesWholeHostFitsAndReservesDestinations(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	addHost(t, s, "source", "z", func(h *Host) { h.Capacity.VCPUs = 8 })
	addHost(t, s, "target", "z", func(h *Host) {
		h.Capacity.VCPUs = 4
		h.Capacity.MaxVMs = 2
		h.Capacity.MemAvailableMB = 1024
		h.Capacity.DiskFreeMB = 4096
	})
	for _, id := range []string{"one", "two", "three"} {
		v := &VM{ID: id, HostID: "source", Tenant: "t", State: VMRunning, TemplateID: "default", Info: map[string]any{"resources": Resources{1, 256, 1024}}}
		if err := s.insertVM(ctx, v); err != nil {
			t.Fatal(err)
		}
	}
	src, _ := s.getHost(ctx, "source")
	if _, err := s.planDrain(ctx, src); err == nil {
		t.Fatal("three VMs fit into two slots")
	}
	got, _ := s.getHost(ctx, "source")
	if got.Cordoned {
		t.Fatal("failed preflight changed source")
	}
	s.deleteVMRow(ctx, "three")
	plan, err := s.planDrain(ctx, src)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan) != 2 {
		t.Fatalf("plan: %+v", plan)
	}
	if err := s.reserveDrain(ctx, "source", plan); err != nil {
		t.Fatal(err)
	}
	if _, err := s.candidates(ctx, placeReq{}); err == nil {
		t.Fatal("unrelated placement consumed drain slots")
	}
	if _, err := s.candidates(ctx, placeReq{ReservationVM: "one", Resources: Resources{1, 256, 1024}}); err != nil {
		t.Fatalf("own reservation unavailable: %v", err)
	}
}

func TestDrainRejectsUnknownResourcesAndIncompatibleHosts(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	addHost(t, s, "s", "z")
	addHost(t, s, "other", "z", func(h *Host) { h.Capacity.VCPUs = 8; h.HostClass = "different" })
	v := &VM{ID: "vm", HostID: "s", Tenant: "t", State: VMRunning, TemplateID: "default"}
	s.insertVM(ctx, v)
	h, _ := s.getHost(ctx, "s")
	if _, err := s.planDrain(ctx, h); err == nil || !strings.Contains(err.Error(), "unknown") {
		t.Fatalf("unknown resources: %v", err)
	}
	s.updateVM(ctx, v.ID, vmUpdate{Info: map[string]any{"resources": Resources{1, 128, 512}}})
	if _, err := s.planDrain(ctx, h); err == nil {
		t.Fatal("planned migration to incompatible host")
	}
}

func TestLiveMigrationLostReplyCommitsWithoutResumingSource(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	var calls []string
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.Path)
		switch r.URL.Path {
		case "/vms/vm/live-migration/prepare":
			writeJSON(w, 200, map[string]any{"address": "10.1.0.2", "port": 1234, "vmm_version": "46.0"})
		case "/vms/vm/live-migration/send":
			writeErr(w, 500, "lost sender reply")
		case "/vms/vm/live-migration/status":
			phase := "received"
			if r.Header.Get("X-Source") == "yes" {
				phase = "sent"
			}
			// Both successful receiver and a confirmed paused sender.
			writeJSON(w, 200, map[string]any{"phase": phase, "vmm_state": "Paused"})
		case "/vms/vm/live-migration/commit":
			writeJSON(w, 200, map[string]any{"vm": map[string]any{"vm_id": "vm"}})
		case "/vms/vm":
			writeJSON(w, 200, map[string]any{"ok": true})
		default:
			t.Errorf("unexpected action %s", r.URL.Path)
			writeErr(w, 500, "unexpected")
		}
	}))
	defer fake.Close()
	s.agents = fake.Client()
	for _, id := range []string{"source", "target"} {
		addHost(t, s, id, "z")
		s.db.Exec(ctx, `UPDATE hosts SET agent_url = ? WHERE id = ?`, fake.URL, id)
	}
	v := &VM{ID: "vm", HostID: "source", Tenant: "t", State: VMRunning, TemplateID: "default", Availability: AvailDurable,
		Info: map[string]any{"resources": map[string]any{"vcpus": 1, "memory_mb": 128, "disk_mb": 256},
			"maintenance": map[string]any{"root_disk": map[string]any{"pool_id": "pool", "vni": 1}, "network": map[string]any{"gateway": "stable"}}}}
	if err := s.insertVM(ctx, v); err != nil {
		t.Fatal(err)
	}
	moved, err := s.migrateVM(ctx, v, "target", "test")
	if err != nil {
		t.Fatal(err)
	}
	if moved.HostID != "target" || moved.State != VMRunning {
		t.Fatalf("migration not committed: %+v", moved)
	}
	for _, call := range calls {
		if strings.Contains(call, "rollback") {
			t.Fatal("resumed source after completed cutover")
		}
	}
}

func TestUnconfirmedReceiverDeletionNeverResumesSource(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	resumed := false
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/rollback") {
			resumed = true
		}
		writeErr(w, 503, "partitioned destination")
	}))
	defer fake.Close()
	s.agents = fake.Client()
	for _, id := range []string{"source", "target"} {
		addHost(t, s, id, "z")
		s.db.Exec(ctx, `UPDATE hosts SET agent_url = ? WHERE id = ?`, fake.URL, id)
	}
	v := &VM{ID: "vm", HostID: "source", Tenant: "t", State: VMRunning, TemplateID: "default"}
	s.insertVM(ctx, v)
	run, err := s.beginOp(ctx, v.ID, v.Tenant, OpMigrate, 0)
	if err != nil {
		t.Fatal(err)
	}
	src, _ := s.getHost(ctx, "source")
	dst, _ := s.getHost(ctx, "target")
	err = s.rollbackLive(run, v, src, dst, fmt.Errorf("transfer failed"))
	run.end(err)
	var retry retryOperation
	if !errors.As(err, &retry) {
		t.Fatalf("uncertainty was not retried: %v", err)
	}
	if resumed {
		t.Fatal("resumed source without confirming receiver deletion")
	}
	op, _ := s.getOp(ctx, run.op.ID)
	if op.State != OpRunning {
		t.Fatalf("lost recovery intent: %s", op.State)
	}
}

func TestReceiverExecutionRequiresCoherentRecoveryNotSourceResume(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	aborts := 0
	sourceResumed := false
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/vms/vm/live-migration/abort":
			aborts++
			writeJSON(w, 200, map[string]any{"phase": "absent", "executed": true})
		case "/vms/vm/live-migration/rollback":
			sourceResumed = true
			writeErr(w, 500, "unsafe source resume")
		default:
			writeErr(w, 500, "unexpected")
		}
	}))
	defer fake.Close()
	s.agents = fake.Client()
	addHost(t, s, "source", "z")
	addHost(t, s, "target", "z")
	src, _ := s.getHost(ctx, "source")
	dst, _ := s.getHost(ctx, "target")
	src.AgentURL = fake.URL
	dst.AgentURL = fake.URL
	v := &VM{ID: "vm", HostID: "source", Tenant: "t", State: VMMigrating, TemplateID: "default", Availability: AvailDurable, CheckpointID: "coherent-checkpoint"}
	if err := s.insertVM(ctx, v); err != nil {
		t.Fatal(err)
	}
	run, err := s.beginOp(ctx, v.ID, v.Tenant, OpMigrate, 0)
	if err != nil {
		t.Fatal(err)
	}
	err = s.rollbackLive(run, v, src, dst, fmt.Errorf("gateway activation failed after guest execution"))
	run.end(err)
	got, _ := s.getVM(ctx, v.ID)
	if got.State != VMRecovering || got.CheckpointID != "coherent-checkpoint" {
		t.Fatalf("lost coherent recovery state: %+v", got)
	}
	if sourceResumed {
		t.Fatal("resumed stale source RAM after receiver wrote shared disk")
	}
	if aborts != 2 {
		t.Fatalf("both VMMs must be fenced before recovery, aborted %d", aborts)
	}
}

func TestGatewayCollectionRequiresCatalogAbsence(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	addHost(t, s, "gateway", "z")
	v := &VM{ID: "vm", HostID: "compute", Tenant: "t", State: VMRunning, TemplateID: "default"}
	s.insertVM(ctx, v)
	h, _ := s.getHost(ctx, "gateway")
	call := func() bool {
		w := httptest.NewRecorder()
		s.handleNetworkRelease(w, httptest.NewRequest("POST", "/", strings.NewReader(`{"vm_id":"vm"}`)), h)
		if w.Code != 200 {
			t.Fatalf("release check: %d %s", w.Code, w.Body)
		}
		var result struct {
			Allowed bool `json:"allowed"`
		}
		json.Unmarshal(w.Body.Bytes(), &result)
		return result.Allowed
	}
	if call() {
		t.Fatal("reclaimed live network")
	}
	s.updateVM(ctx, v.ID, vmUpdate{State: str(VMMigrating)})
	if call() {
		t.Fatal("reclaimed network during migration")
	}
	s.deleteVMRow(ctx, v.ID)
	if !call() {
		t.Fatal("leaked removed VM network")
	}
}
