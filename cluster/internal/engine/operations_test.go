package engine

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/jverre/flint/cluster/internal/db"
	"github.com/jverre/flint/cluster/internal/model"
)

func expireLock(t *testing.T, d *db.DB, vmID string) {
	t.Helper()
	if _, err := d.Exec(context.Background(), `UPDATE leases SET expires_at = 0 WHERE name = ?`, vmLockName(vmID)); err != nil {
		t.Fatal(err)
	}
}

func TestVMLockIsExclusiveAcrossReplicas(t *testing.T) {
	rs, _ := testEngines(t, 2)
	ctx := context.Background()
	a, err := rs[0].lockVM(ctx, "vm-1", 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := rs[1].lockVM(ctx, "vm-1", 0); !IsBusy(err) {
		t.Fatalf("second replica got a held lock: %v", err)
	}
	if _, err := rs[0].lockVM(ctx, "vm-1", 0); !IsBusy(err) {
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
	rs, d := testEngines(t, 2)
	ctx := context.Background()
	a, err := rs[0].lockVM(ctx, "vm-1", 0)
	if err != nil {
		t.Fatal(err)
	}
	expireLock(t, d, "vm-1") // replica a stopped renewing (partitioned from the database)
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
	if _, err := rs[0].lockVM(ctx, "vm-1", 0); !IsBusy(err) {
		t.Fatalf("the old holder's release freed the new holder's lock: %v", err)
	}
}

func TestInterruptedOperationIsTakenOverOnlyOnceItsReplicaIsGone(t *testing.T) {
	rs, d := testEngines(t, 2)
	ctx := context.Background()
	if err := rs[0].store.InsertVM(ctx, &model.VM{ID: "vm-1", Tenant: "t", State: model.VMRunning, TemplateID: model.DefaultTemplate,
		Availability: model.AvailDurable}); err != nil {
		t.Fatal(err)
	}
	run, err := rs[0].beginOp(ctx, "vm-1", "t", model.OpCheckpoint, 0)
	if err != nil {
		t.Fatal(err)
	}
	if err := run.step(model.StepCheckpointing, model.OpParams{Snapshot: "ckpt-x"}); err != nil {
		t.Fatal(err)
	}
	d.Exec(ctx, `UPDATE operations SET updated_at = 0 WHERE id = ?`, run.op.ID)

	rs[1].ResumeInterruptedOps(ctx)
	if op, _ := rs[1].store.GetOp(ctx, run.op.ID); op.State != model.OpRunning {
		t.Fatalf("an operation whose replica is alive was taken over: %+v", op)
	}

	expireLock(t, d, "vm-1") // replica a died
	rs[1].ResumeInterruptedOps(ctx)
	op, _ := rs[1].store.GetOp(ctx, run.op.ID)
	if op.State != model.OpFailed || !strings.Contains(op.Error, "rolled back") || op.Params.Snapshot != "ckpt-x" {
		t.Fatalf("interrupted checkpoint not rolled back: %+v", op)
	}
	// The dead replica's late outcome doesn't overwrite the takeover's.
	run.end(nil)
	if op, _ := rs[1].store.GetOp(ctx, run.op.ID); op.State != model.OpFailed {
		t.Fatalf("a stale holder rewrote the outcome: %+v", op)
	}
	if _, err := rs[1].lockVM(ctx, "vm-1", 0); err != nil {
		t.Fatalf("the VM is still locked after the takeover: %v", err)
	}
}
