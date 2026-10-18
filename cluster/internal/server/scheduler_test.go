package server

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jverre/flint/cluster/internal/db"
)

func testServer(t *testing.T) *Server {
	t.Helper()
	d, err := db.Open("sqlite://" + filepath.Join(t.TempDir(), "flint.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := d.Migrate(context.Background()); err != nil {
		t.Fatal(err)
	}
	s := &Server{cfg: Config{LeaseTTL: 15 * time.Second}, db: d}
	t.Cleanup(func() { d.Close() })
	return s
}

type hostOpt func(*Host)

func addHost(t *testing.T, s *Server, id, zone string, opts ...hostOpt) {
	t.Helper()
	h := &Host{ID: id, Name: id, Zone: zone, Backend: "linux-firecracker", HostClass: "fc|cpu", State: HostReady,
		Capacity: Capacity{MaxVMs: 10, MemAvailableMB: 4000, DiskFreeMB: 10000}, LastHeartbeat: nowMs()}
	for _, o := range opts {
		o(h)
	}
	cordoned := 0
	if h.Cordoned {
		cordoned = 1
	}
	if _, err := s.db.Exec(context.Background(), `INSERT INTO hosts (id, name, agent_url, snapd_url, zone, backend, host_class, state, cordoned,
		capacity, cached, templates, last_heartbeat, max_vms, created_at) VALUES (?, ?, '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		h.ID, h.Name, h.Zone, h.Backend, h.HostClass, h.State, cordoned, mustJSON(h.Capacity), mustJSON(h.Cached),
		mustJSON(h.Templates), h.LastHeartbeat, h.MaxVMs, nowMs()); err != nil {
		t.Fatal(err)
	}
}

func addVMs(t *testing.T, s *Server, host string, n int, state string) {
	t.Helper()
	for i := 0; i < n; i++ {
		v := &VM{ID: host + "-vm-" + string(rune('a'+i)) + state, Tenant: "t", HostID: host, State: state, TemplateID: "default", Availability: AvailEphemeral}
		if err := s.insertVM(context.Background(), v); err != nil {
			t.Fatal(err)
		}
	}
}

func ids(hs []*Host) []string {
	var out []string
	for _, h := range hs {
		out = append(out, h.ID)
	}
	return out
}

func TestPlacementSkipsHostsThatCantTakeAVM(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	addHost(t, s, "full", "z1", func(h *Host) { h.Capacity.MaxVMs = 2 })
	addVMs(t, s, "full", 2, VMRunning)
	addHost(t, s, "cordoned", "z1", func(h *Host) { h.Cordoned = true })
	addHost(t, s, "silent", "z1", func(h *Host) { h.LastHeartbeat = nowMs() - 60_000 })
	addHost(t, s, "down", "z1", func(h *Host) { h.State = HostDown })
	addHost(t, s, "nomem", "z1", func(h *Host) { h.Capacity.MemAvailableMB = 100 })
	addHost(t, s, "capped", "z1", func(h *Host) { h.MaxVMs = 1 })
	addVMs(t, s, "capped", 1, VMRunning)
	addHost(t, s, "ok", "z1")
	// Paused VMs don't count against capacity.
	addHost(t, s, "paused-only", "z1", func(h *Host) { h.Capacity.MaxVMs = 1 })
	addVMs(t, s, "paused-only", 3, VMPaused)

	got, err := s.candidates(ctx, placeReq{TemplateID: "default"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(ids(got), ",") != "ok,paused-only" && strings.Join(ids(got), ",") != "paused-only,ok" {
		t.Fatalf("eligible hosts = %v, want ok and paused-only", ids(got))
	}

	if _, err := s.candidates(ctx, placeReq{TemplateID: "default", Exclude: map[string]bool{"ok": true, "paused-only": true}}); err == nil ||
		!strings.Contains(err.Error(), "full (2/2 VMs)") || !strings.Contains(err.Error(), "cordoned") {
		t.Fatalf("want an explanation naming why each host was refused, got %v", err)
	}
}

func TestPlacementMatchesSnapshotHostClassAndPrefersCachedCopies(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	addHost(t, s, "other-class", "z1", func(h *Host) { h.HostClass = "fc|other-cpu"; h.Cached = []string{"snap"} })
	addHost(t, s, "cold", "z1")
	addHost(t, s, "warm", "z1", func(h *Host) { h.Cached = []string{"snap"} })
	got, err := s.candidates(ctx, placeReq{SnapshotID: "snap", HostClass: "fc|cpu"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(ids(got), ",") != "warm,cold" {
		t.Fatalf("got %v, want warm (has the chunks) before cold, other class excluded", ids(got))
	}
}

func TestPlacementSpreadsAcrossZones(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	addHost(t, s, "a1", "zone-a")
	addHost(t, s, "a2", "zone-a")
	addHost(t, s, "b1", "zone-b")
	got, err := s.candidates(ctx, placeReq{TemplateID: "default", Spread: "zone", SpreadCounts: map[string]int{"zone-a": 1}})
	if err != nil {
		t.Fatal(err)
	}
	if got[0].ID != "b1" {
		t.Fatalf("got %v, want the empty zone first", ids(got))
	}
}

func TestPlacementNeedsTheTemplateOrItsSnapshotForTheHostClass(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	addHost(t, s, "has-it", "z1", func(h *Host) { h.Templates = []HostTemplate{{ID: "py", Status: "ready"}} })
	addHost(t, s, "same-class", "z1")
	addHost(t, s, "other-class", "z1", func(h *Host) { h.HostClass = "ch|cpu" })
	if err := s.insertSnapshot(ctx, &Snapshot{ID: "tpl-py", Kind: "template", TemplateID: "py", Backend: "linux-firecracker",
		HostClass: "fc|cpu", Uploaded: true}); err != nil {
		t.Fatal(err)
	}
	got, err := s.candidates(ctx, placeReq{TemplateID: "py"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(ids(got), ",") != "has-it,same-class" {
		t.Fatalf("got %v, want has-it (local) then same-class (can fetch it); other-class can't restore it", ids(got))
	}
}
