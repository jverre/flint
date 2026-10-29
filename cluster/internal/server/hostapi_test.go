package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jverre/flint/cluster/internal/engine"
	"github.com/jverre/flint/cluster/internal/hostagent"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

func addHost(t *testing.T, s *Server, id, zone string) {
	t.Helper()
	if err := s.store.InsertHost(context.Background(), &model.Host{ID: id, Name: id, Zone: zone, Backend: "linux-firecracker",
		HostClass: "fc|cpu", State: model.HostReady, Capacity: model.Capacity{MaxVMs: 10, MemAvailableMB: 4000, DiskFreeMB: 10000},
		LastHeartbeat: model.NowMs()}); err != nil {
		t.Fatal(err)
	}
}

// callHost calls a host-API handler as host id.
func callHost(t *testing.T, s *Server, h hostHandlerFunc, id string, body any) *httptest.ResponseRecorder {
	t.Helper()
	host, err := s.store.GetHost(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(body)
	w := httptest.NewRecorder()
	r := httptest.NewRequest("POST", "/", bytes.NewReader(b))
	if err := h(w, r, host); err != nil {
		httpx.WriteErr(w, err)
	}
	return w
}

func TestHandoverIsBoundedAndCannotAdoptOrReviveVMs(t *testing.T) {
	rs, d := replicasDB(t, 1)
	s := rs[0]
	ctx := context.Background()
	addHost(t, s, "h", "z")
	if err := s.store.InsertVM(ctx, &model.VM{ID: "vm", HostID: "h", Tenant: "t", State: model.VMRunning, Availability: model.AvailDurable,
		TemplateID: model.DefaultTemplate}); err != nil {
		t.Fatal(err)
	}
	token := strings.Repeat("a", 32)
	call := func(ids []string, token string) *httptest.ResponseRecorder {
		return callHost(t, s, s.handleHandover, "h", map[string]any{"token": token, "vms": ids})
	}
	if w := call(nil, token); w.Code != 409 {
		t.Fatalf("incomplete frozen set: %d %s", w.Code, w.Body)
	}
	if w := call([]string{"vm"}, token); w.Code != 200 {
		t.Fatalf("begin: %d %s", w.Code, w.Body)
	}
	first, _ := s.store.GetHost(ctx, "h")
	if first.HandoverUntil > model.NowMs()+maxHandover.Milliseconds() {
		t.Fatal("unbounded handover")
	}
	if w := call([]string{"vm"}, token); w.Code != 200 {
		t.Fatalf("renew: %d %s", w.Code, w.Body)
	}
	second, _ := s.store.GetHost(ctx, "h")
	if first.HandoverUntil != second.HandoverUntil {
		t.Fatal("renewal extended upgrade window")
	}
	if w := call([]string{"vm"}, strings.Repeat("b", 32)); w.Code != 409 {
		t.Fatal("another owner took handover")
	}
	if _, err := s.eng.Candidates(ctx, engine.PlaceRequest{}); err == nil {
		t.Fatal("placement during handover")
	}
	d.Exec(ctx, `UPDATE hosts SET last_heartbeat = 0 WHERE id = ?`, "h")
	if w := call([]string{"vm"}, token); w.Code != 409 {
		t.Fatal("revived expired host lease")
	}
}

func TestHandoverClaimRequiredForHeartbeat(t *testing.T) {
	rs, d := replicasDB(t, 1)
	s := rs[0]
	ctx := context.Background()
	addHost(t, s, "h", "z")
	token := strings.Repeat("a", 32)
	d.Exec(ctx, `UPDATE hosts SET handover_token = ?, handover_until = ? WHERE id = ?`, token, model.NowMs()+10000, "h")
	for _, tc := range []struct {
		token  string
		status int
	}{{"", 409}, {"wrong", 409}, {token, 200}} {
		w := callHost(t, s, s.handleHeartbeat, "h", hostagent.Heartbeat{HandoverToken: tc.token, PatchState: map[string]any{"running_kernel": "approved"}})
		if w.Code != tc.status {
			t.Fatalf("heartbeat got %d: %s", w.Code, w.Body)
		}
	}
	h, _ := s.store.GetHost(ctx, "h")
	if h.HandoverToken != "" || h.PatchState["running_kernel"] != "approved" {
		t.Fatal("claim or inventory not persisted")
	}
}

func TestGatewayCollectionRequiresCatalogAbsence(t *testing.T) {
	s := replicas(t, 1)[0]
	ctx := context.Background()
	addHost(t, s, "gateway", "z")
	v := &model.VM{ID: "vm", HostID: "compute", Tenant: "t", State: model.VMRunning, TemplateID: model.DefaultTemplate}
	s.store.InsertVM(ctx, v)
	call := func() bool {
		w := callHost(t, s, s.handleNetworkRelease, "gateway", map[string]string{"vm_id": "vm"})
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
	s.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMMigrating)})
	if call() {
		t.Fatal("reclaimed network during migration")
	}
	s.store.DeleteVM(ctx, v.ID)
	if !call() {
		t.Fatal("leaked removed VM network")
	}
}
