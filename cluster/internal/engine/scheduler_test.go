package engine

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jverre/flint/cluster/internal/db"
	"github.com/jverre/flint/cluster/internal/hostagent"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// testEngines returns n engines (replicas) sharing one database, and the
// database for setting up states the API can't reach.
func testEngines(t *testing.T, n int) ([]*Engine, *db.DB) {
	t.Helper()
	url := "sqlite://" + filepath.Join(t.TempDir(), "flint.db")
	var out []*Engine
	var raw *db.DB
	for i := 0; i < n; i++ {
		d, err := db.Open(url)
		if err != nil {
			t.Fatal(err)
		}
		if err := d.Migrate(context.Background()); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { d.Close() })
		raw = d
		out = append(out, New(Config{LeaseTTL: 15 * time.Second, ReplicaID: string(rune('a' + i))}, store.New(d), &hostagent.Client{}, nil))
	}
	return out, raw
}

func testEngine(t *testing.T) (*Engine, *db.DB) {
	es, d := testEngines(t, 1)
	return es[0], d
}

type hostOpt func(*model.Host)

func addHost(t *testing.T, e *Engine, id, zone string, opts ...hostOpt) {
	t.Helper()
	h := &model.Host{ID: id, Name: id, Zone: zone, Backend: "linux-firecracker", HostClass: "fc|cpu", State: model.HostReady,
		Capacity: model.Capacity{MaxVMs: 10, MemAvailableMB: 4000, DiskFreeMB: 10000}, LastHeartbeat: model.NowMs()}
	for _, o := range opts {
		o(h)
	}
	if err := e.store.InsertHost(context.Background(), h); err != nil {
		t.Fatal(err)
	}
}

func addVMs(t *testing.T, e *Engine, host string, n int, state model.VMState) {
	t.Helper()
	for i := 0; i < n; i++ {
		v := &model.VM{ID: host + "-vm-" + string(rune('a'+i)) + string(state), Tenant: "t", HostID: host, State: state,
			TemplateID: model.DefaultTemplate, Availability: model.AvailEphemeral}
		if err := e.store.InsertVM(context.Background(), v); err != nil {
			t.Fatal(err)
		}
	}
}

func ids(hs []*model.Host) []string {
	var out []string
	for _, h := range hs {
		out = append(out, h.ID)
	}
	return out
}

func TestPlacementSkipsHostsThatCantTakeAVM(t *testing.T) {
	e, _ := testEngine(t)
	ctx := context.Background()
	addHost(t, e, "full", "z1", func(h *model.Host) { h.Capacity.MaxVMs = 2 })
	addVMs(t, e, "full", 2, model.VMRunning)
	addHost(t, e, "cordoned", "z1", func(h *model.Host) { h.Cordoned = true })
	addHost(t, e, "silent", "z1", func(h *model.Host) { h.LastHeartbeat = model.NowMs() - 60_000 })
	addHost(t, e, "down", "z1", func(h *model.Host) { h.State = model.HostDown })
	addHost(t, e, "nomem", "z1", func(h *model.Host) { h.Capacity.MemAvailableMB = 100 })
	addHost(t, e, "capped", "z1", func(h *model.Host) { h.MaxVMs = 1 })
	addVMs(t, e, "capped", 1, model.VMRunning)
	addHost(t, e, "ok", "z1")
	// Paused VMs don't count against capacity.
	addHost(t, e, "paused-only", "z1", func(h *model.Host) { h.Capacity.MaxVMs = 1 })
	addVMs(t, e, "paused-only", 3, model.VMPaused)

	got, err := e.Candidates(ctx, PlaceRequest{TemplateID: model.DefaultTemplate})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(ids(got), ",") != "ok,paused-only" && strings.Join(ids(got), ",") != "paused-only,ok" {
		t.Fatalf("eligible hosts = %v, want ok and paused-only", ids(got))
	}

	if _, err := e.Candidates(ctx, PlaceRequest{TemplateID: model.DefaultTemplate, Exclude: map[string]bool{"ok": true, "paused-only": true}}); err == nil ||
		!strings.Contains(err.Error(), "full (2/2 VMs)") || !strings.Contains(err.Error(), "cordoned") {
		t.Fatalf("want an explanation naming why each host was refused, got %v", err)
	}
}

func TestPlacementMatchesSnapshotHostClassAndPrefersCachedCopies(t *testing.T) {
	e, _ := testEngine(t)
	ctx := context.Background()
	addHost(t, e, "other-class", "z1", func(h *model.Host) { h.HostClass = "fc|other-cpu"; h.Cached = []string{"snap"} })
	addHost(t, e, "cold", "z1")
	addHost(t, e, "warm", "z1", func(h *model.Host) { h.Cached = []string{"snap"} })
	got, err := e.Candidates(ctx, PlaceRequest{SnapshotID: "snap", HostClass: "fc|cpu"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(ids(got), ",") != "warm,cold" {
		t.Fatalf("got %v, want warm (has the chunks) before cold, other class excluded", ids(got))
	}
}

func TestPlacementSpreadsAcrossZones(t *testing.T) {
	e, _ := testEngine(t)
	ctx := context.Background()
	addHost(t, e, "a1", "zone-a")
	addHost(t, e, "a2", "zone-a")
	addHost(t, e, "b1", "zone-b")
	got, err := e.Candidates(ctx, PlaceRequest{TemplateID: model.DefaultTemplate, Spread: model.SpreadZone, SpreadCounts: map[string]int{"zone-a": 1}})
	if err != nil {
		t.Fatal(err)
	}
	if got[0].ID != "b1" {
		t.Fatalf("got %v, want the empty zone first", ids(got))
	}
}

func TestPlacementNeedsTheTemplateOrItsSnapshotForTheHostClass(t *testing.T) {
	e, _ := testEngine(t)
	ctx := context.Background()
	addHost(t, e, "has-it", "z1", func(h *model.Host) { h.Templates = []model.HostTemplate{{ID: "py", Status: model.TemplateReady}} })
	addHost(t, e, "same-class", "z1")
	addHost(t, e, "other-class", "z1", func(h *model.Host) { h.HostClass = "ch|cpu" })
	if err := e.store.InsertSnapshot(ctx, &model.Snapshot{ID: "tpl-py", Kind: model.SnapshotTemplate, TemplateID: "py", Backend: "linux-firecracker",
		HostClass: "fc|cpu", Uploaded: true}); err != nil {
		t.Fatal(err)
	}
	got, err := e.Candidates(ctx, PlaceRequest{TemplateID: "py"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(ids(got), ",") != "has-it,same-class" {
		t.Fatalf("got %v, want has-it (local) then same-class (can fetch it); other-class can't restore it", ids(got))
	}
}

// A VM set saved with its own template keeps booting only that tenant's:
// if the ID later belongs to someone else, its members are refused.
func TestPlacementRefusesAnotherTenantsTemplate(t *testing.T) {
	e, d := testEngine(t)
	ctx := context.Background()
	if _, err := d.Exec(ctx, `INSERT INTO templates (id, name, tenant, dockerfile, rootfs_mb, status, build_host, error, created_at, updated_at)
		VALUES ('acme-app', 'acme-app', 'acme', 'FROM alpine', 500, 'ready', NULL, '', 1, 1)`); err != nil {
		t.Fatal(err)
	}
	v := &model.VM{ID: "vm-1", Tenant: "globex", State: model.VMPending, TemplateID: "acme-app"}
	_, _, err := e.placeAndCreate(ctx, v, PlaceRequest{}, nil)
	if !isNotFoundErr(err) {
		t.Fatalf("placed a globex VM on acme's template: %v", err)
	}
}
