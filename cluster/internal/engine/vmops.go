package engine

import (
	"context"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jverre/flint/cluster/internal/hostagent"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// placeAndCreate picks a host for v (already inserted, state pending) and
// creates it there, trying the next candidate if a host is full or fails.
// With snap, the VM is restored from that snapshot instead of its template.
func (e *Engine) placeAndCreate(ctx context.Context, v *model.VM, req PlaceRequest, snap *hostagent.SnapshotRef) (*model.Host, map[string]any, error) {
	lock, err := e.LockPlacement(ctx)
	if err != nil {
		return nil, nil, err
	}
	defer lock.Release()
	ctx = lock.ctx
	req.ReservationVM = v.ID
	req.Resources = v.Resources()
	if req.TemplateID == "" {
		req.TemplateID = v.TemplateID
	}
	if snap == nil && v.TemplateID != model.DefaultTemplate {
		if _, err := e.TemplateFor(ctx, v.TemplateID, v.Tenant); err != nil {
			return nil, nil, err
		}
	}
	if snap != nil {
		req.SnapshotID = snap.ID
		if sn, err := e.store.GetSnapshot(ctx, snap.ID); err == nil {
			req.HostClass, req.Backend = sn.HostClass, sn.Backend
		}
	}
	cands, err := e.Candidates(ctx, req)
	if err != nil {
		return nil, nil, err
	}
	var errs []string
	for i, h := range cands {
		if i >= 3 {
			break
		}
		if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{HostID: &h.ID}); err != nil {
			return nil, nil, err
		}
		cr := hostagent.NewCreateRequest(v)
		cr.ReplaceSharedDisk = snap != nil || v.State == model.VMRecovering
		if snap != nil {
			cr.Snapshot = &hostagent.SnapshotRef{ID: snap.ID, Peers: snap.Peers}
			if len(cr.Snapshot.Peers) == 0 {
				cr.Snapshot.Peers = e.peersFor(ctx, snap.ID, h.ID)
			}
		} else if v.TemplateID != model.DefaultTemplate && !h.HasTemplate(v.TemplateID) {
			ts, err := e.templateSnapshotFor(ctx, v.TemplateID, h.HostClass)
			if err != nil {
				errs = append(errs, h.Name+": "+err.Error())
				continue
			}
			cr.TemplateSnapshot = &hostagent.SnapshotRef{ID: ts.ID, Peers: e.peersFor(ctx, ts.ID, h.ID)}
		}
		info, err := e.agents.Create(ctx, h, cr)
		if err != nil {
			log.Printf("create %s on %s failed: %v", short(v.ID), h.Name, err)
			errs = append(errs, h.Name+": "+err.Error())
			if !hostagent.IsCapacity(err) {
				// Don't leave a half-created VM behind on a host that errored.
				logErr("cleaning up "+short(v.ID)+" on "+h.Name, e.agents.Delete(context.Background(), h, v.ID))
			}
			continue
		}
		if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMRunning), Info: info, Error: store.Ptr("")}); err != nil {
			return nil, nil, err
		}
		v.HostID, v.State, v.Info = h.ID, model.VMRunning, info
		return h, info, nil
	}
	return nil, nil, &CapacityError{fmt.Sprintf("every candidate host failed (%s)", strings.Join(errs, "; "))}
}

// CreateVM records a new VM and places it. The caller has filled v.
func (e *Engine) CreateVM(ctx context.Context, v *model.VM, req PlaceRequest) (map[string]any, error) {
	run, err := e.beginOp(ctx, v.ID, v.Tenant, model.OpCreate, 0)
	if err != nil {
		return nil, err
	}
	info, err := e.createLocked(run, v, req)
	run.end(err)
	return info, err
}

func (e *Engine) createLocked(run *opRun, v *model.VM, req PlaceRequest) (map[string]any, error) {
	ctx := run.ctx()
	if err := e.store.InsertVM(ctx, v); err != nil {
		return nil, err
	}
	err := run.step(model.StepPlacing)
	var info map[string]any
	if err == nil {
		_, info, err = e.placeAndCreate(ctx, v, req, nil)
	}
	if err != nil {
		logErr("removing unplaced "+short(v.ID), e.store.DeleteVM(context.WithoutCancel(ctx), v.ID))
		return nil, err
	}
	return info, nil
}

// lockedVM starts an operation on a VM and reloads it under the lock: the
// caller's copy may be stale (another operation may have just changed it).
func (e *Engine) lockedVM(ctx context.Context, id string, kind model.OpKind, wait time.Duration) (*opRun, *model.VM, error) {
	v, err := e.store.GetVM(ctx, id)
	if err != nil {
		if store.IsNotFound(err) {
			return nil, nil, errVMNotFound
		}
		return nil, nil, err
	}
	run, err := e.beginOp(ctx, id, v.Tenant, kind, wait)
	if err != nil {
		return nil, nil, err
	}
	fail := func(err error) (*opRun, *model.VM, error) {
		run.end(err)
		return nil, nil, err
	}
	v, err = e.store.GetVM(run.ctx(), id)
	if err != nil {
		if store.IsNotFound(err) {
			err = errVMNotFound
		}
		return fail(err)
	}
	guard, err := e.LockPlacement(run.ctx())
	if err != nil {
		return fail(err)
	}
	defer guard.Release()
	if v.HostID != "" {
		host, herr := e.store.GetHost(guard.ctx, v.HostID)
		if (herr != nil && !store.IsNotFound(herr)) || (herr == nil && host.InHandover(model.NowMs())) {
			return fail(httpx.Conflict("host agent upgrade in progress or host unavailable"))
		}
	}
	return run, v, nil
}

// DeleteVM removes a VM everywhere. A VM on a dead host is evicted when the
// host comes back (heartbeat reconciliation).
func (e *Engine) DeleteVM(ctx context.Context, v *model.VM) error {
	run, v, err := e.lockedVM(ctx, v.ID, model.OpDelete, LockWait)
	if err != nil {
		if isNotFoundErr(err) {
			return nil // already gone
		}
		return err
	}
	err = e.deleteLocked(run, v)
	run.end(err)
	return err
}

func (e *Engine) deleteLocked(run *opRun, v *model.VM) error {
	ctx := run.ctx()
	hostGone := true
	if h, ok := e.HostIfUsable(ctx, v.HostID); ok {
		if err := e.agents.Delete(ctx, h, v.ID); err != nil {
			return err
		}
		hostGone = false
	}
	if err := e.store.DeleteVM(ctx, v.ID); err != nil {
		return err
	}
	if hostGone {
		e.Emit(ctx, "vm.deleted", v.Tenant, v.ID, v.HostID, map[string]any{"vm_id": v.ID})
	}
	return nil
}

// PauseVM stops a VM and stores its full state in the object store, so it
// can be resumed on any host of the same class.
func (e *Engine) PauseVM(ctx context.Context, v *model.VM) error {
	run, v, err := e.lockedVM(ctx, v.ID, model.OpPause, LockWait)
	if err != nil {
		return err
	}
	err = e.pauseLocked(run, v)
	run.end(err)
	return err
}

func notState(v *model.VM, want string) error {
	return httpx.Conflict(fmt.Sprintf("VM is %s, not %s", v.State.APIName(), want))
}

func (e *Engine) pauseLocked(run *opRun, v *model.VM) error {
	ctx := run.ctx()
	if v.State != model.VMRunning {
		return notState(v, "running")
	}
	h, ok := e.HostIfUsable(ctx, v.HostID)
	if !ok {
		return errHostUnreachable
	}
	snapID := "pause-" + uuid.NewString()
	if err := run.step(model.StepPausing, model.OpParams{Snapshot: snapID}); err != nil {
		return err
	}
	res, err := e.agents.Pause(ctx, h, v.ID, hostagent.SnapshotRequest{SnapshotID: snapID, Kind: model.SnapshotPause, Upload: hostagent.UploadSync})
	if err != nil {
		return err
	}
	if err := run.step(model.StepPausedOnHost); err != nil {
		return err
	}
	if err := e.store.InsertSnapshot(ctx, &model.Snapshot{ID: snapID, Kind: model.SnapshotPause, VMID: v.ID, Tenant: v.Tenant, Backend: h.Backend,
		HostClass: h.HostClass, SourceHost: h.ID, Uploaded: res.Snapshot.Uploaded, Bytes: res.Snapshot.Bytes}); err != nil {
		return err
	}
	return e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMPaused), PauseID: &snapID})
}

// ResumeVM resumes on the VM's own host when it can (its files are still
// there), otherwise restores the pause snapshot on another host.
func (e *Engine) ResumeVM(ctx context.Context, v *model.VM) (*model.VM, error) {
	run, v, err := e.lockedVM(ctx, v.ID, model.OpResume, LockWait)
	if err != nil {
		return nil, err
	}
	err = e.resumeLocked(run, v)
	run.end(err)
	if err != nil {
		return nil, err
	}
	return e.store.GetVM(ctx, v.ID)
}

func (e *Engine) resumeLocked(run *opRun, v *model.VM) error {
	ctx := run.ctx()
	if v.State != model.VMPaused {
		return notState(v, "paused")
	}
	old := v.HostID
	if err := run.step(model.StepInPlace, model.OpParams{SourceHost: old, Snapshot: v.PauseID, Reason: "resume"}); err != nil {
		return err
	}
	if h, ok := e.HostIfUsable(ctx, v.HostID); ok && !h.Cordoned && h.State == model.HostReady {
		info, err := e.agents.Resume(ctx, h, v.ID)
		if err == nil {
			// Its pause snapshot is no longer needed (GC reclaims it).
			return e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMRunning), Info: info, PauseID: store.Ptr("")})
		}
		log.Printf("resume %s on its host %s failed (%v); restoring elsewhere", short(v.ID), h.Name, err)
	}
	if v.PauseID == "" {
		return httpx.Conflict("VM has no pause snapshot to restore from")
	}
	if err := run.step(model.StepRestoring); err != nil {
		return err
	}
	if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMMigrating)}); err != nil {
		return err
	}
	h, _, err := e.placeAndCreate(ctx, v, PlaceRequest{Exclude: map[string]bool{old: true}}, &hostagent.SnapshotRef{ID: v.PauseID})
	if err != nil {
		logErr("returning "+short(v.ID)+" to paused", e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMPaused), HostID: &old}))
		return err
	}
	e.dropPartial(ctx, old, v.ID) // its stale paused copy
	logErr("clearing pause of "+short(v.ID), e.store.UpdateVM(ctx, v.ID, store.VMUpdate{PauseID: store.Ptr("")}))
	e.Emit(ctx, "vm.moved", v.Tenant, v.ID, h.ID, map[string]any{"vm_id": v.ID, "from_host": old, "to_host": h.ID, "reason": "resume"})
	return nil
}

// ForkVM checkpoints a running VM in place and starts count copies of it.
// Children go wherever there is room; they fetch the snapshot from the source
// host directly while it uploads. All-or-nothing.
func (e *Engine) ForkVM(ctx context.Context, v *model.VM, count int) ([]*model.VM, error) {
	run, v, err := e.lockedVM(ctx, v.ID, model.OpFork, LockWait)
	if err != nil {
		return nil, err
	}
	children, err := e.forkLocked(run, v, count)
	run.end(err)
	return children, err
}

func (e *Engine) forkLocked(run *opRun, v *model.VM, count int) ([]*model.VM, error) {
	ctx := run.ctx()
	if v.State != model.VMRunning {
		return nil, notState(v, "running")
	}
	h, ok := e.HostIfUsable(ctx, v.HostID)
	if !ok {
		return nil, errHostUnreachable
	}
	if err := e.CheckQuota(ctx, v.Tenant, count); err != nil {
		return nil, err
	}
	snapID := "fork-" + uuid.NewString()
	if err := run.step(model.StepCheckpointing, model.OpParams{Snapshot: snapID}); err != nil {
		return nil, err
	}
	t0 := time.Now()
	res, err := e.agents.Checkpoint(ctx, h, v.ID, hostagent.SnapshotRequest{SnapshotID: snapID, Kind: model.SnapshotFork, Upload: hostagent.UploadAsync})
	if err != nil {
		return nil, err
	}
	if err := e.store.InsertSnapshot(ctx, &model.Snapshot{ID: snapID, Kind: model.SnapshotFork, VMID: v.ID, Tenant: v.Tenant, Backend: h.Backend,
		HostClass: h.HostClass, SourceHost: h.ID, Uploaded: false, Bytes: res.Snapshot.Bytes}); err != nil {
		return nil, err
	}
	log.Printf("fork %s: checkpoint %s on %s in %s", short(v.ID), snapID, h.Name, time.Since(t0))
	var children []*model.VM
	var ids []string
	fail := func(err error) ([]*model.VM, error) {
		c := context.WithoutCancel(ctx)
		for _, id := range ids {
			if cv, err := e.store.GetVM(c, id); err == nil {
				e.dropPartial(c, cv.HostID, cv.ID)
				logErr("removing fork child "+short(cv.ID), e.store.DeleteVM(c, cv.ID))
			}
		}
		return nil, err
	}
	for i := 0; i < count; i++ {
		c := &model.VM{ID: uuid.NewString(), Tenant: v.Tenant, State: model.VMPending, TemplateID: v.TemplateID, Availability: v.Availability,
			Spec: v.Spec, Labels: map[string]string{"forked_from": v.ID}}
		if v.Availability == model.AvailDurable {
			c.CheckpointID = snapID // recoverable from the fork point until its first own checkpoint
		}
		ids = append(ids, c.ID)
		// Recorded before the child exists, so a takeover can find and remove it.
		if err := run.step(model.StepCreatingChildren, model.OpParams{Children: strings.Join(ids, ",")}); err != nil {
			return fail(err)
		}
		if err := e.store.InsertVM(ctx, c); err != nil {
			return fail(err)
		}
		// The source's host has every chunk locally, so it's preferred; others fetch from it.
		if _, _, err := e.placeAndCreate(ctx, c, PlaceRequest{PreferHost: h.ID}, &hostagent.SnapshotRef{ID: snapID, Peers: []string{h.SnapdURL}}); err != nil {
			return fail(err)
		}
		got, err := e.store.GetVM(ctx, c.ID)
		if err != nil {
			return fail(err)
		}
		children = append(children, got)
	}
	return children, nil
}

// CheckpointVM snapshots a running VM so it can be restored after a host
// loss. wait=0 skips a VM another operation is busy with (the scheduler
// comes back to it). The VM is reloaded afterwards.
func (e *Engine) CheckpointVM(ctx context.Context, v *model.VM, wait time.Duration) (*model.VM, error) {
	run, v, err := e.lockedVM(ctx, v.ID, model.OpCheckpoint, wait)
	if err != nil {
		if wait == 0 && IsBusy(err) {
			return nil, nil
		}
		return nil, err
	}
	err = e.checkpointLocked(run, v)
	run.end(err)
	if err != nil {
		return nil, err
	}
	return e.store.GetVM(ctx, v.ID)
}

func (e *Engine) checkpointLocked(run *opRun, v *model.VM) error {
	ctx := run.ctx()
	h, ok := e.HostIfUsable(ctx, v.HostID)
	if !ok || v.State != model.VMRunning {
		return nil
	}
	snapID := "ckpt-" + uuid.NewString()
	if err := run.step(model.StepCheckpointing, model.OpParams{Snapshot: snapID}); err != nil {
		return err
	}
	res, err := e.agents.Checkpoint(ctx, h, v.ID, hostagent.SnapshotRequest{SnapshotID: snapID, Kind: model.SnapshotCheckpoint,
		Parent: v.CheckpointID, Upload: hostagent.UploadSync})
	if err != nil {
		return err
	}
	if err := e.store.InsertSnapshot(ctx, &model.Snapshot{ID: snapID, Kind: model.SnapshotCheckpoint, VMID: v.ID, Tenant: v.Tenant, Backend: h.Backend,
		HostClass: h.HostClass, SourceHost: h.ID, Uploaded: res.Snapshot.Uploaded, Bytes: res.Snapshot.Bytes}); err != nil {
		return err
	}
	if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{CheckpointID: &snapID, CheckpointAt: store.Ptr(model.NowMs())}); err != nil {
		return err
	}
	e.Emit(ctx, "vm.checkpointed", v.Tenant, v.ID, h.ID, map[string]any{"vm_id": v.ID, "snapshot_id": snapID, "bytes": res.Snapshot.Bytes})
	return nil
}

// MigrateVM moves a VM to another host (target, or the best one if "").
// Running VMs with shared storage move live; others pause, export, restore
// elsewhere and drop the old copy (downtime is the snapshot plus the restore).
func (e *Engine) MigrateVM(ctx context.Context, v *model.VM, target, reason string) (*model.VM, error) {
	run, v, err := e.lockedVM(ctx, v.ID, model.OpMigrate, LockWait)
	if err != nil {
		return nil, err
	}
	err = e.migrateLocked(run, v, target, reason)
	run.end(err)
	if err != nil {
		return nil, err
	}
	return e.store.GetVM(ctx, v.ID)
}

func (e *Engine) migrateLocked(run *opRun, v *model.VM, target, reason string) error {
	ctx := run.ctx()
	src, ok := e.HostIfUsable(ctx, v.HostID)
	if !ok {
		return errHostUnreachable
	}
	if v.State == model.VMPaused {
		// Nothing runs: just forget the old host so resume places it anywhere.
		logErr("dropping paused copy of "+short(v.ID), e.agents.Delete(ctx, src, v.ID))
		return e.store.UpdateVM(ctx, v.ID, store.VMUpdate{HostID: store.Ptr("")})
	}
	if v.State != model.VMRunning {
		return httpx.Conflict("VM is " + v.State.APIName())
	}
	// Check there is somewhere to go before stopping it.
	req := PlaceRequest{ReservationVM: v.ID, Resources: v.Resources(), TemplateID: v.TemplateID, HostClass: src.HostClass, Backend: src.Backend,
		Exclude: map[string]bool{src.ID: true}}
	if target != "" {
		hosts, err := e.store.ListHosts(ctx)
		if err != nil {
			return err
		}
		for _, h := range hosts {
			if h.ID != target {
				req.Exclude[h.ID] = true
			}
		}
	}
	cands, err := e.Candidates(ctx, req)
	if err != nil {
		return err
	}
	if v.Maintenance().SharedDisk() {
		return e.liveMigrate(run, v, src, cands[0], reason)
	}
	t0 := time.Now()
	snapID := "move-" + uuid.NewString()
	if err := run.step(model.StepPausing, model.OpParams{SourceHost: src.ID, Snapshot: snapID, Reason: reason}); err != nil {
		return err
	}
	res, err := e.agents.Pause(ctx, src, v.ID, hostagent.SnapshotRequest{SnapshotID: snapID, Kind: model.SnapshotPause, Upload: hostagent.UploadAsync})
	if err != nil {
		return err
	}
	logErr("recording snapshot "+snapID, e.store.InsertSnapshot(ctx, &model.Snapshot{ID: snapID, Kind: model.SnapshotPause, VMID: v.ID, Tenant: v.Tenant,
		Backend: src.Backend, HostClass: src.HostClass, SourceHost: src.ID, Uploaded: res.Snapshot.Uploaded, Bytes: res.Snapshot.Bytes}))
	if err := run.step(model.StepRestoring); err != nil {
		e.resumeInPlace(context.WithoutCancel(ctx), v)
		return err
	}
	if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMMigrating), PauseID: &snapID}); err != nil {
		e.resumeInPlace(context.WithoutCancel(ctx), v)
		return err
	}
	h, _, err := e.placeAndCreate(ctx, v, req, &hostagent.SnapshotRef{ID: snapID, Peers: []string{src.SnapdURL}})
	if err != nil {
		// Roll back: it's still intact on the source.
		log.Printf("migrate %s: restore elsewhere failed (%v), resuming on %s", short(v.ID), err, src.Name)
		logErr("returning "+short(v.ID)+" to its source",
			e.store.UpdateVM(ctx, v.ID, store.VMUpdate{HostID: &src.ID, State: store.Ptr(model.VMPaused)}))
		v.HostID = src.ID
		e.resumeInPlace(ctx, v)
		return err
	}
	logErr("dropping source copy of "+short(v.ID), e.agents.Delete(ctx, src, v.ID))
	logErr("clearing pause of "+short(v.ID), e.store.UpdateVM(ctx, v.ID, store.VMUpdate{PauseID: store.Ptr("")}))
	e.Emit(ctx, "vm.moved", v.Tenant, v.ID, h.ID, map[string]any{"vm_id": v.ID, "from_host": src.ID, "to_host": h.ID,
		"reason": reason, "downtime_ms": time.Since(t0).Milliseconds()})
	return nil
}

// recoverVM restores a VM whose host failed (or whose VMM crashed):
// durable from its last checkpoint, restartable fresh from its template.
// It skips a VM another operation holds; retryRecoveries comes back to it.
func (e *Engine) recoverVM(ctx context.Context, v *model.VM, failedHost string, sameHostOK bool) error {
	run, v, err := e.lockedVM(ctx, v.ID, model.OpRecover, 0)
	if err != nil {
		if IsBusy(err) {
			return nil
		}
		return err
	}
	err = e.recoverLocked(run, v, failedHost, sameHostOK)
	run.end(err)
	return err
}

func (e *Engine) recoverLocked(run *opRun, v *model.VM, failedHost string, sameHostOK bool) error {
	ctx := run.ctx()
	if v.State != model.VMRecovering {
		return nil // recovered, deleted or moved meanwhile
	}
	var snap *hostagent.SnapshotRef
	from := "template"
	if v.Availability == model.AvailDurable && v.CheckpointID != "" {
		if sn, err := e.store.GetSnapshot(ctx, v.CheckpointID); err == nil && (sn.Uploaded || sn.Kind == model.SnapshotFork) {
			snap = &hostagent.SnapshotRef{ID: sn.ID}
			from = "checkpoint"
		}
	}
	if err := run.step(model.StepPlacing, model.OpParams{FailedHost: failedHost}); err != nil {
		return err
	}
	req := PlaceRequest{}
	if sameHostOK {
		req.PreferHost = failedHost          // local restart first (Masakari-style)
		e.dropPartial(ctx, failedHost, v.ID) // clear the crashed instance so the ID is free
	} else if failedHost != "" {
		req.Exclude = map[string]bool{failedHost: true}
	}
	h, _, err := e.placeAndCreate(ctx, v, req, snap)
	if err != nil {
		logErr("recording pending recovery of "+short(v.ID), e.store.UpdateVM(ctx, v.ID,
			store.VMUpdate{State: store.Ptr(model.VMRecovering), HostID: store.Ptr(""), Error: store.Ptr("recovery pending: " + err.Error())}))
		return err
	}
	payload := map[string]any{"vm_id": v.ID, "from": from, "failed_host": failedHost, "host_id": h.ID}
	if from == "checkpoint" {
		payload["checkpoint_at"] = float64(v.CheckpointAt) / 1000
	}
	e.Emit(ctx, "vm.recovered", v.Tenant, v.ID, h.ID, payload)
	log.Printf("recovered %s on %s from %s", short(v.ID), h.Name, from)
	return nil
}
