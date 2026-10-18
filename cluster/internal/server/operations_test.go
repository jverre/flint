package server

import (
	"context"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jverre/flint/cluster/internal/db"
)

// replicas returns n servers sharing one database, as replicas behind a load balancer.
func replicas(t *testing.T, n int) []*Server {
	t.Helper()
	url := "sqlite://" + filepath.Join(t.TempDir(), "flint.db")
	var out []*Server
	for i := 0; i < n; i++ {
		d, err := db.Open(url)
		if err != nil {
			t.Fatal(err)
		}
		if err := d.Migrate(context.Background()); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { d.Close() })
		out = append(out, &Server{cfg: Config{ReplicaID: string(rune('a' + i))}, db: d})
	}
	return out
}

func expireLock(t *testing.T, s *Server, vmID string) {
	t.Helper()
	if _, err := s.db.Exec(context.Background(), `UPDATE leases SET expires_at = 0 WHERE name = ?`, vmLockName(vmID)); err != nil {
		t.Fatal(err)
	}
}

func TestVMLockIsExclusiveAcrossReplicas(t *testing.T) {
	rs := replicas(t, 2)
	ctx := context.Background()
	a, err := rs[0].lockVM(ctx, "vm-1", 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := rs[1].lockVM(ctx, "vm-1", 0); !isBusy(err) {
		t.Fatalf("second replica got a held lock: %v", err)
	}
	if _, err := rs[0].lockVM(ctx, "vm-1", 0); !isBusy(err) {
		t.Fatalf("a second operation on the same replica got a held lock: %v", err)
	}
	other, err := rs[1].lockVM(ctx, "vm-2", 0)
	if err != nil {
		t.Fatalf("another VM's lock: %v", err)
	}
	other.Release()

	// A waiter gets it as soon as it's released.
	go func() { time.Sleep(200 * time.Millisecond); a.Release() }()
	b, err := rs[1].lockVM(ctx, "vm-1", 5*time.Second)
	if err != nil {
		t.Fatalf("waiting for a released lock: %v", err)
	}
	b.Release()
}

func TestVMLockLostToAnotherReplicaStopsItsOperation(t *testing.T) {
	rs := replicas(t, 2)
	ctx := context.Background()
	a, err := rs[0].lockVM(ctx, "vm-1", 0)
	if err != nil {
		t.Fatal(err)
	}
	expireLock(t, rs[0], "vm-1") // replica a stopped renewing (partitioned from the database)
	b, err := rs[1].lockVM(ctx, "vm-1", 0)
	if err != nil {
		t.Fatalf("an expired lock wasn't taken over: %v", err)
	}
	defer b.Release()
	select {
	case <-a.Context().Done():
	case <-time.After(2 * vmLockRenew):
		t.Fatal("the operation that lost its lock kept running")
	}
	a.Release() // must not release b's lock
	if _, err := rs[0].lockVM(ctx, "vm-1", 0); !isBusy(err) {
		t.Fatalf("the old holder's release freed the new holder's lock: %v", err)
	}
}

func TestInterruptedOperationIsTakenOverOnlyOnceItsReplicaIsGone(t *testing.T) {
	rs := replicas(t, 2)
	ctx := context.Background()
	if err := rs[0].insertVM(ctx, &VM{ID: "vm-1", Tenant: "t", State: VMRunning, TemplateID: "default", Availability: AvailDurable}); err != nil {
		t.Fatal(err)
	}
	run, err := rs[0].beginOp(ctx, "vm-1", "t", OpCheckpoint, 0)
	if err != nil {
		t.Fatal(err)
	}
	run.step("checkpointing", "snapshot", "ckpt-x")
	age := func() {
		rs[0].db.Exec(ctx, `UPDATE operations SET updated_at = 0 WHERE id = ?`, run.op.ID)
	}
	age()

	rs[1].resumeInterruptedOps(ctx)
	if op, _ := rs[1].getOp(ctx, run.op.ID); op.State != OpRunning {
		t.Fatalf("an operation whose replica is alive was taken over: %+v", op)
	}

	expireLock(t, rs[0], "vm-1") // replica a died
	rs[1].resumeInterruptedOps(ctx)
	op, _ := rs[1].getOp(ctx, run.op.ID)
	if op.State != OpFailed || !strings.Contains(op.Error, "rolled back") {
		t.Fatalf("interrupted checkpoint not rolled back: %+v", op)
	}
	// The dead replica's late outcome doesn't overwrite the takeover's.
	run.end(nil)
	if op, _ := rs[1].getOp(ctx, run.op.ID); op.State != OpFailed {
		t.Fatalf("a stale holder rewrote the outcome: %+v", op)
	}
	if _, err := rs[1].lockVM(ctx, "vm-1", 0); err != nil {
		t.Fatalf("the VM is still locked after the takeover: %v", err)
	}
}

func TestIdempotencyKeyReplaysTheFirstAnswer(t *testing.T) {
	rs := replicas(t, 2)
	calls := 0
	handler := func(w http.ResponseWriter, r *http.Request) {
		calls++
		writeJSON(w, 200, map[string]any{"call": calls})
	}
	do := func(s *Server, token, key, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("POST", "/vms", strings.NewReader(body))
		r.Header.Set(idemHeader, key)
		r = r.WithContext(context.WithValue(r.Context(), principalKey, &Principal{TokenID: token, Kind: KindService}))
		w := httptest.NewRecorder()
		s.idempotent(handler)(w, r)
		return w
	}
	first := do(rs[0], "tok", "k1", `{"tenant":"t"}`)
	again := do(rs[1], "tok", "k1", `{"tenant":"t"}`) // the retry lands on the other replica
	if calls != 1 || again.Code != 200 || again.Body.String() != first.Body.String() || again.Header().Get("Idempotent-Replayed") != "true" {
		t.Fatalf("retry wasn't replayed: calls=%d %d %q", calls, again.Code, again.Body.String())
	}
	if w := do(rs[0], "tok", "k1", `{"tenant":"other"}`); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("same key, different request: %d %s", w.Code, w.Body.String())
	}
	if do(rs[0], "other-token", "k1", `{"tenant":"t"}`); calls != 2 {
		t.Fatalf("keys aren't scoped to the token: calls=%d", calls)
	}
}
