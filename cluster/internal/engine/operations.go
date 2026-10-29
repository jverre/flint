package engine

import (
	"context"
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jverre/flint/cluster/internal/hostagent"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// An operation is the durable record of one change to a VM: what it is, how
// far it got (its step) and what it touched (its params: snapshot IDs, the
// source host). Each step is written before the action it guards, so if the
// replica running it dies, the leader can tell from the record and from the
// hosts whether to finish the operation or roll it back.
//
// An operation always holds its VM's lock (lock.go). The leader only takes
// over an operation once it can take that lock, i.e. once the replica that
// started it has stopped renewing it.

// retryOperation leaves a durable operation open for the leader to retry.
// Unknown destination state must never trigger a source resume.
type retryOperation struct{ error }

func (r retryOperation) Unwrap() error { return r.error }

// rolledBack is the outcome of an interrupted operation that was undone.
type rolledBack string

func (e rolledBack) Error() string { return "rolled back: " + string(e) }

type idemKey struct{}

// WithIdempotencyKey ties the operation a request starts to its
// Idempotency-Key, so a retry can be answered from the operation's outcome.
func WithIdempotencyKey(ctx context.Context, key string) context.Context {
	return context.WithValue(ctx, idemKey{}, key)
}

// opRun is an operation in progress on this replica.
type opRun struct {
	e    *Engine
	op   *model.Operation
	lock *Lock
	once sync.Once
}

// beginOp locks the VM (waiting up to wait) and records the operation. Use
// run.ctx() for everything the operation does: it's cancelled if the lock is
// lost, so a replica that was cut off stops touching the VM.
func (e *Engine) beginOp(ctx context.Context, vmID, tenant string, kind model.OpKind, wait time.Duration) (*opRun, error) {
	l, err := e.lockVM(ctx, vmID, wait)
	if err != nil {
		return nil, err
	}
	now := model.NowMs()
	op := &model.Operation{ID: "op-" + uuid.NewString(), VMID: vmID, Tenant: tenant, Kind: kind, State: model.OpRunning,
		Holder: l.holder, CreatedAt: now, UpdatedAt: now}
	if err := e.store.InsertOp(l.ctx, op); err != nil {
		l.Release()
		return nil, err
	}
	if key, _ := ctx.Value(idemKey{}).(string); key != "" {
		logErr("linking idempotency key", e.store.LinkIdempotencyKey(l.ctx, key, op.ID))
	}
	return &opRun{e: e, op: op, lock: l}, nil
}

func (r *opRun) ctx() context.Context { return r.lock.Context() }

// step records progress (and any params) before the action it guards.
func (r *opRun) step(step model.OpStep, params ...model.OpParams) error {
	for _, p := range params {
		r.op.Params.Merge(p)
	}
	r.op.Step = step
	return r.e.store.SaveOpStep(r.ctx(), r.op)
}

// end records the outcome and releases the VM's lock. Safe to call twice.
func (r *opRun) end(err error) {
	r.once.Do(func() {
		state, msg := model.OpSucceeded, ""
		if err != nil {
			state, msg = model.OpFailed, err.Error()
			var retry retryOperation
			if errors.As(err, &retry) {
				state = model.OpRunning
			}
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		// Only while we still hold the lock: a takeover has rewritten holder.
		if derr := r.e.store.FinishOp(ctx, r.op.ID, r.op.Holder, state, msg); derr != nil {
			log.Printf("operation %s: recording outcome: %v", r.op.ID, derr)
		}
		r.lock.Release()
	})
}

// ── Taking over interrupted operations ────────────────────────────────────

// ResumeInterruptedOps (leader) finishes or rolls back operations whose
// replica died. An operation is interrupted when its VM's lock is free while
// the operation is still running.
func (e *Engine) ResumeInterruptedOps(ctx context.Context) {
	ops, err := e.store.StaleOps(ctx, model.NowMs()-vmLockRenew.Milliseconds())
	if err != nil {
		return
	}
	var wg sync.WaitGroup
	for _, op := range ops {
		wg.Add(1)
		go func(op *model.Operation) {
			defer wg.Done()
			e.takeOver(ctx, op)
		}(op)
	}
	wg.Wait()
}

func (e *Engine) takeOver(ctx context.Context, stale *model.Operation) {
	l, err := e.lockVM(ctx, stale.VMID, 0)
	if err != nil {
		return // its replica is still at it
	}
	// It may have finished between the listing and the lock.
	op, err := e.store.GetOp(l.ctx, stale.ID)
	if err != nil || op.State != model.OpRunning {
		l.Release()
		return
	}
	if ok, err := e.store.TakeOverOp(l.ctx, op.ID, l.holder); err != nil || !ok {
		l.Release()
		return
	}
	op.Holder = l.holder
	run := &opRun{e: e, op: op, lock: l}
	log.Printf("taking over interrupted %s of %s (step %q, was %s)", op.Kind, short(op.VMID), op.Step, stale.Holder)
	outcome := e.finishInterrupted(run)
	if outcome != nil {
		outcome = fmt.Errorf("interrupted at %q: %w", op.Step, outcome)
	}
	e.Emit(run.ctx(), "operation.recovered", op.Tenant, op.VMID, "", map[string]any{
		"operation_id": op.ID, "kind": op.Kind, "step": op.Step, "vm_id": op.VMID,
		"outcome": map[bool]string{true: "completed", false: "rolled_back"}[outcome == nil]})
	run.end(outcome)
}

// finishInterrupted decides, from the operation's last step and what the
// hosts actually have, whether to complete or undo it. nil = completed.
func (e *Engine) finishInterrupted(r *opRun) error {
	ctx := r.ctx()
	op := r.op
	v, err := e.store.GetVM(ctx, op.VMID)
	if store.IsNotFound(err) {
		if op.Kind == model.OpFork {
			e.dropForkChildren(ctx, op)
		}
		return nil // nothing left to fix
	}
	if err != nil {
		return err
	}
	if v.HostID != "" {
		if h, err := e.store.GetHost(ctx, v.HostID); err == nil && h.InHandover(model.NowMs()) {
			return retryOperation{fmt.Errorf("host agent upgrade in progress")}
		}
	}
	if op.Kind == model.OpMigrate && op.Step.Live() {
		return e.finishLive(r, v)
	}
	switch op.Kind {
	case model.OpCreate:
		if st, _ := e.probe(ctx, v.HostID, v.ID); st.Up() {
			return e.commitPlaced(ctx, v)
		}
		e.dropPartial(ctx, v.HostID, v.ID)
		if err := e.store.DeleteVM(ctx, v.ID); err != nil {
			return err
		}
		return rolledBack("the VM never started")

	case model.OpDelete:
		if h, ok := e.HostIfUsable(ctx, v.HostID); ok {
			if err := e.agents.Delete(ctx, h, v.ID); err != nil {
				return err
			}
		}
		return e.store.DeleteVM(ctx, v.ID)

	case model.OpPause:
		snap := op.Params.Snapshot
		st, _ := e.probe(ctx, v.HostID, v.ID)
		if op.Step == model.StepPausedOnHost || st == hostagent.VMPaused {
			if ok, _ := e.remote.Exists(ctx, "manifests/"+snap+".json"); ok {
				sn := &model.Snapshot{ID: snap, Kind: model.SnapshotPause, VMID: v.ID, Tenant: v.Tenant, SourceHost: v.HostID, Uploaded: true}
				if h, err := e.store.GetHost(ctx, v.HostID); err == nil {
					sn.Backend, sn.HostClass = h.Backend, h.HostClass
				}
				if err := e.store.InsertSnapshot(ctx, sn); err != nil {
					return err
				}
				return e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMPaused), PauseID: &snap})
			}
			if st == hostagent.VMPaused {
				e.resumeInPlace(ctx, v)
			}
		}
		return rolledBack("the VM was left running")

	case model.OpResume, model.OpMigrate:
		src := op.Params.SourceHost
		if op.Step == model.StepRestoring {
			if v.HostID != src {
				if st, _ := e.probe(ctx, v.HostID, v.ID); st.Up() {
					if h, ok := e.HostIfUsable(ctx, src); ok && src != "" {
						logErr("dropping source copy", e.agents.Delete(ctx, h, v.ID))
					}
					if err := e.commitPlaced(ctx, v); err != nil {
						return err
					}
					if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{PauseID: store.Ptr("")}); err != nil {
						return err
					}
					e.Emit(ctx, "vm.moved", v.Tenant, v.ID, v.HostID, map[string]any{"vm_id": v.ID, "from_host": src,
						"to_host": v.HostID, "reason": op.Params.Reason})
					return nil
				}
				e.dropPartial(ctx, v.HostID, v.ID)
			}
			// Back where it was: paused there, with its snapshot.
			if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{HostID: &src, State: store.Ptr(model.VMPaused), PauseID: &op.Params.Snapshot}); err != nil {
				return err
			}
			v.HostID, v.State = src, model.VMPaused
		}
		if op.Kind == model.OpMigrate {
			// Interrupted while pausing or restoring: it was running before, so
			// it runs again on its source.
			if st, _ := e.probe(ctx, v.HostID, v.ID); st == hostagent.VMPaused {
				e.resumeInPlace(ctx, v)
			}
			return rolledBack("the VM stayed on its host")
		}
		// A resume interrupted on the VM's own host may have gone through.
		if st, _ := e.probe(ctx, v.HostID, v.ID); st.Up() {
			if err := e.commitPlaced(ctx, v); err != nil {
				return err
			}
			return e.store.UpdateVM(ctx, v.ID, store.VMUpdate{PauseID: store.Ptr("")})
		}
		return rolledBack("the VM is still paused")

	case model.OpRecover:
		if st, _ := e.probe(ctx, v.HostID, v.ID); st.Up() {
			return e.commitPlaced(ctx, v)
		}
		e.dropPartial(ctx, v.HostID, v.ID)
		if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMRecovering), HostID: store.Ptr("")}); err != nil {
			return err
		}
		return rolledBack("recovery will be retried")

	case model.OpFork:
		e.dropForkChildren(ctx, op)
		return rolledBack("forks are all-or-nothing")

	default: // checkpoint: the VM kept running; an unrecorded snapshot is garbage-collected
		return rolledBack("nothing to undo")
	}
}

// probe asks a host for its view of a VM: its state, "" if the host doesn't
// have it; ok=false if the host couldn't be asked.
func (e *Engine) probe(ctx context.Context, hostID, vmID string) (hostagent.VMState, bool) {
	h, ok := e.HostIfUsable(ctx, hostID)
	if !ok {
		return "", false
	}
	st, err := e.agents.Probe(ctx, h, vmID)
	return st, err == nil
}

// commitPlaced records a VM as running on the host it was being placed on.
func (e *Engine) commitPlaced(ctx context.Context, v *model.VM) error {
	upd := store.VMUpdate{State: store.Ptr(model.VMRunning), Error: store.Ptr("")}
	if h, ok := e.HostIfUsable(ctx, v.HostID); ok {
		if info, err := e.agents.Get(ctx, h, v.ID); err == nil {
			upd.Info = info
		}
	}
	return e.store.UpdateVM(ctx, v.ID, upd)
}

// dropPartial deletes a VM's copy on a host, best effort.
func (e *Engine) dropPartial(ctx context.Context, hostID, vmID string) {
	if h, ok := e.HostIfUsable(ctx, hostID); ok {
		logErr("dropping "+short(vmID)+" from "+h.Name, e.agents.Delete(ctx, h, vmID))
	}
}

// resumeInPlace resumes a paused VM on its own host, best effort.
func (e *Engine) resumeInPlace(ctx context.Context, v *model.VM) {
	h, ok := e.HostIfUsable(ctx, v.HostID)
	if !ok {
		return
	}
	info, err := e.agents.Resume(ctx, h, v.ID)
	if err != nil {
		log.Printf("resuming %s on %s: %v", short(v.ID), h.Name, err)
		return
	}
	logErr("recording resume of "+short(v.ID), e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMRunning), Info: info}))
}

func (e *Engine) dropForkChildren(ctx context.Context, op *model.Operation) {
	for _, id := range op.Params.ChildIDs() {
		if c, err := e.store.GetVM(ctx, id); err == nil {
			e.dropPartial(ctx, c.HostID, c.ID)
			logErr("removing fork child "+short(c.ID), e.store.DeleteVM(ctx, c.ID))
		}
	}
}

// pruneOps forgets finished operations, idempotency keys and VM locks after a day.
func (e *Engine) pruneOps(ctx context.Context) {
	logErr("pruning operations", e.store.PruneOps(ctx, model.NowMs()-(24*time.Hour).Milliseconds(), model.NowMs()-time.Hour.Milliseconds()))
}
