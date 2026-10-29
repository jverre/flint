package engine

import (
	"fmt"
	"time"

	"github.com/jverre/flint/cluster/internal/hostagent"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// liveMigrate moves a running VM whose disk and network can follow it to
// dst without pausing it (hostagent/migration.go has the protocol).
func (e *Engine) liveMigrate(run *opRun, v *model.VM, src, dst *model.Host, reason string) error {
	ctx := run.ctx()
	if !v.Maintenance().Live() {
		return fmt.Errorf("VM lacks migration storage or networking")
	}
	if err := e.reserveLiveMove(run, v, src, dst); err != nil {
		return err
	}
	if err := run.step(model.StepLivePreparing, model.OpParams{SourceHost: src.ID, TargetHost: dst.ID, Reason: reason}); err != nil {
		return err
	}
	if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMMigrating)}); err != nil {
		return err
	}
	var receiver map[string]any
	if err := e.agents.Migrate(ctx, dst, v.ID, hostagent.MigratePrepare, hostagent.NewPrepareRequest(v, src.ID), &receiver); err != nil {
		return e.rollbackLive(run, v, src, dst, err)
	}
	if err := run.step(model.StepLiveTransferring); err != nil {
		return retryOperation{err}
	}
	// The sender gets the receiver's answer (its address, port, VMM version) and who it is.
	if receiver == nil {
		receiver = map[string]any{}
	}
	receiver["target_host"] = dst.ID
	if err := e.agents.Migrate(ctx, src, v.ID, hostagent.MigrateSend, receiver, nil); err != nil {
		// A lost sender reply may still have completed the cutover.
		return e.finishLive(run, v)
	}
	if err := run.step(model.StepLiveCutover); err != nil {
		return retryOperation{err}
	}
	deadline := time.Now().Add(60 * time.Second)
	for time.Now().Before(deadline) {
		var status hostagent.MigrationStatus
		if err := e.agents.Migrate(ctx, dst, v.ID, hostagent.MigrateStatus, nil, &status); err != nil {
			return retryOperation{err}
		}
		switch status.Phase {
		case hostagent.PhaseReceived:
			return e.commitLive(run, v, src, dst)
		case hostagent.PhaseAbsent:
			return e.rollbackLive(run, v, src, dst, fmt.Errorf("receiver failed"))
		}
		select {
		case <-ctx.Done():
			return retryOperation{ctx.Err()}
		case <-time.After(200 * time.Millisecond):
		}
	}
	return retryOperation{fmt.Errorf("awaiting receiver cutover confirmation")}
}

// reserveLiveMove checks dst can still take v and reserves room for it
// there, under the placement lock so no create takes it meanwhile.
func (e *Engine) reserveLiveMove(run *opRun, v *model.VM, src, dst *model.Host) error {
	guard, err := e.LockPlacement(run.ctx())
	if err != nil {
		return err
	}
	defer guard.Release()
	all, err := e.store.ListHosts(guard.ctx)
	if err != nil {
		return err
	}
	exclude := map[string]bool{}
	for _, h := range all {
		if h.ID != dst.ID {
			exclude[h.ID] = true
		}
	}
	if _, err := e.Candidates(guard.ctx, PlaceRequest{ReservationVM: v.ID, Resources: v.Resources(), HostClass: src.HostClass,
		Backend: src.Backend, Exclude: exclude}); err != nil {
		return err
	}
	return e.store.ReserveMove(guard.ctx, v.ID, src.ID, dst.ID, v.Resources())
}

func (e *Engine) commitLive(run *opRun, v *model.VM, src, dst *model.Host) error {
	ctx := run.ctx()
	var result struct {
		VM map[string]any `json:"vm"`
	}
	// Receiver remains fenced until this commit. Record ownership before
	// relaxing the temporary fence for an ephemeral VM.
	if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{HostID: &dst.ID, State: store.Ptr(model.VMRunning)}); err != nil {
		return retryOperation{err}
	}
	if err := e.agents.Migrate(ctx, dst, v.ID, hostagent.MigrateCommit, nil, &result); err != nil {
		return retryOperation{err}
	}
	if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{Info: result.VM, PauseID: store.Ptr("")}); err != nil {
		return retryOperation{err}
	}
	if err := e.agents.Delete(ctx, src, v.ID); err != nil {
		return retryOperation{err}
	}
	logErr("releasing move reservation", e.store.DeleteReservation(ctx, v.ID))
	e.Emit(ctx, "vm.moved", v.Tenant, v.ID, dst.ID, map[string]any{"vm_id": v.ID, "from_host": src.ID, "to_host": dst.ID,
		"reason": run.op.Params.Reason, "mode": "live"})
	return nil
}

func (e *Engine) rollbackLive(run *opRun, v *model.VM, src, dst *model.Host, cause error) error {
	ctx := run.ctx()
	// A receiver may already be executing after a lost reply. Confirm its
	// deletion before asking CH to resume the source; never guess.
	var aborted hostagent.AbortResult
	if err := e.agents.Migrate(ctx, dst, v.ID, hostagent.MigrateAbort, nil, &aborted); err != nil {
		return retryOperation{fmt.Errorf("receiver deletion unconfirmed: %w", err)}
	}
	if aborted.Executed {
		return e.failLive(run, v, src, cause)
	}
	var result struct {
		VM map[string]any `json:"vm"`
	}
	if err := e.agents.Migrate(ctx, src, v.ID, hostagent.MigrateRollback, nil, &result); err != nil {
		if hostagent.IsConflict(err) {
			return e.failLive(run, v, src, cause)
		}
		return retryOperation{err}
	}
	if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{HostID: &src.ID, State: store.Ptr(model.VMRunning), Info: result.VM}); err != nil {
		return retryOperation{err}
	}
	logErr("releasing move reservation", e.store.DeleteReservation(ctx, v.ID))
	return rolledBack(cause.Error())
}

// finishLive settles a live migration whose outcome is unknown (a lost
// reply, or a takeover) from what both hosts report.
func (e *Engine) finishLive(run *opRun, v *model.VM) error {
	ctx := run.ctx()
	src, err := e.store.GetHost(ctx, run.op.Params.SourceHost)
	if err != nil {
		return retryOperation{err}
	}
	dst, err := e.store.GetHost(ctx, run.op.Params.TargetHost)
	if err != nil {
		return retryOperation{err}
	}
	var source, target hostagent.MigrationStatus
	if err := e.agents.Migrate(ctx, dst, v.ID, hostagent.MigrateStatus, nil, &target); err != nil {
		return retryOperation{err}
	}
	if err := e.agents.Migrate(ctx, src, v.ID, hostagent.MigrateStatus, nil, &source); err != nil {
		return retryOperation{err}
	}
	sourceGaveUp := source.Phase == hostagent.PhaseSent || source.VMMState == hostagent.VMPaused ||
		(source.Phase == hostagent.PhaseAbsent && source.VMMState == hostagent.VMAbsent)
	if target.Phase == hostagent.PhaseReceived && sourceGaveUp {
		return e.commitLive(run, v, src, dst)
	}
	// Recovered operations with a transfer still underway wait for a terminal
	// state. Deleting an in-flight receiver could race CH's source rollback.
	if target.Phase == hostagent.PhaseReceiving || target.Phase == hostagent.PhaseCutover {
		return retryOperation{fmt.Errorf("migration transfer still in flight")}
	}
	return e.rollbackLive(run, v, src, dst, fmt.Errorf("interrupted live migration"))
}

// failLive: after a receiver has executed, its disk writes may have advanced
// beyond the source's paused RAM. Fence both copies and recover a coherent
// checkpoint; never resume stale source RAM against an advanced shared disk.
func (e *Engine) failLive(run *opRun, v *model.VM, src *model.Host, cause error) error {
	ctx := run.ctx()
	if err := e.agents.Migrate(ctx, src, v.ID, hostagent.MigrateAbort, nil, nil); err != nil {
		return retryOperation{err}
	}
	state := model.VMLost
	if v.Availability.Recoverable() {
		state = model.VMRecovering
	}
	if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{HostID: &src.ID, State: &state,
		Error: store.Ptr("live migration failed after possible cutover: " + cause.Error())}); err != nil {
		return retryOperation{err}
	}
	logErr("releasing move reservation", e.store.DeleteReservation(ctx, v.ID))
	return rolledBack("both copies fenced; coherent recovery required")
}
