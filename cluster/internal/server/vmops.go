package server

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
)

// apiState maps catalog states to the names the daemon's API uses.
func apiState(state string) string {
	switch state {
	case VMPending:
		return "Starting"
	case VMRunning:
		return "Started"
	case VMPaused:
		return "Paused"
	case VMMigrating:
		return "Migrating"
	case VMRecovering:
		return "Recovering"
	case VMLost:
		return "Lost"
	default:
		return "Error"
	}
}

// vmDict is the API view of a VM: the host's last view of it plus cluster fields.
func (s *Server) vmDict(v *VM, live map[string]any) map[string]any {
	d := map[string]any{}
	for k, val := range v.Info {
		d[k] = val
	}
	for k, val := range live {
		d[k] = val
	}
	d["vm_id"] = v.ID
	d["state"] = apiState(v.State)
	d["cluster_state"] = v.State
	d["template_id"] = v.TemplateID
	d["tenant"] = v.Tenant
	d["host_id"] = v.HostID
	d["availability"] = v.Availability
	d["labels"] = v.Labels
	if v.VMSetID != "" {
		d["vmset_id"] = v.VMSetID
		d["role"] = v.Role
	}
	if v.CheckpointAt > 0 {
		d["checkpoint_at"] = float64(v.CheckpointAt) / 1000
	}
	if v.Error != "" {
		d["error"] = v.Error
	}
	if _, ok := d["created_at"]; !ok {
		d["created_at"] = float64(v.CreatedAt) / 1000
	}
	if v.State != VMRunning {
		d["agent_healthy"] = false
	}
	return d
}

// placement errors the API turns into 503.
var errNoCapacity = errors.New("capacity_exhausted")

// placeAndCreate picks a host for v (already inserted, state pending) and
// creates it there, trying the next candidate if a host is full or fails.
func (s *Server) placeAndCreate(ctx context.Context, v *VM, req placeReq, snap *snapshotRef) (*Host, map[string]any, error) {
	lock, err := s.lockVM(ctx, "_placement", vmLockWait)
	if err != nil {
		return nil, nil, err
	}
	defer lock.Release()
	ctx = lock.ctx
	req.ReservationVM = v.ID
	req.Resources = vmResources(v)
	if req.TemplateID == "" {
		req.TemplateID = v.TemplateID
	}
	if snap != nil {
		req.SnapshotID = snap.ID
		if sn, err := s.getSnapshot(ctx, snap.ID); err == nil {
			req.HostClass, req.Backend = sn.HostClass, sn.Backend
		}
	}
	cands, err := s.candidates(ctx, req)
	if err != nil {
		return nil, nil, fmt.Errorf("%w: %s", errNoCapacity, strings.TrimPrefix(err.Error(), "capacity_exhausted: "))
	}
	var errs []string
	for i, h := range cands {
		if i >= 3 {
			break
		}
		if err := s.updateVM(ctx, v.ID, vmUpdate{HostID: str(h.ID)}); err != nil {
			return nil, nil, err
		}
		cr := createOnHostReq{
			ReplaceSharedDisk: snap != nil || v.State == VMRecovering,
			VMID:              v.ID, Tenant: v.Tenant, TemplateID: v.TemplateID, AllowInternet: v.Spec.AllowInternet,
			NetworkPolicy: v.Spec.NetworkPolicy, Availability: v.Availability,
			TimeoutSecs: v.Spec.TimeoutSecs, TimeoutPolicy: v.Spec.TimeoutPolicy,
		}
		if snap != nil {
			cr.Snapshot = &snapshotRef{ID: snap.ID, Peers: snap.Peers}
			if len(cr.Snapshot.Peers) == 0 {
				cr.Snapshot.Peers = s.peersFor(ctx, snap.ID, h.ID)
			}
		} else if v.TemplateID != "default" && !hostHasTemplate(h, v.TemplateID) {
			ts, err := s.templateSnapshotFor(ctx, v.TemplateID, h.HostClass)
			if err != nil {
				errs = append(errs, h.Name+": "+err.Error())
				continue
			}
			cr.TemplateSnapshot = &snapshotRef{ID: ts.ID, Peers: s.peersFor(ctx, ts.ID, h.ID)}
		}
		info, err := s.createOnHost(ctx, h, cr)
		if err != nil {
			log.Printf("create %s on %s failed: %v", short(v.ID), h.Name, err)
			errs = append(errs, h.Name+": "+err.Error())
			if !isCapacityErr(err) {
				// Don't leave a half-created VM behind on a host that errored.
				s.deleteOnHost(context.Background(), h, v.ID)
			}
			continue
		}
		if err := s.updateVM(ctx, v.ID, vmUpdate{State: str(VMRunning), Info: info, Error: str("")}); err != nil {
			return nil, nil, err
		}
		v.HostID, v.State, v.Info = h.ID, VMRunning, info
		return h, info, nil
	}
	return nil, nil, fmt.Errorf("%w: every candidate host failed (%s)", errNoCapacity, strings.Join(errs, "; "))
}

func (s *Server) templateSnapshotFor(ctx context.Context, templateID, hostClass string) (*Snapshot, error) {
	snaps, err := s.listSnapshots(ctx, `kind = 'template' AND template_id = ? AND host_class = ? AND uploaded = 1`, templateID, hostClass)
	if err != nil {
		return nil, err
	}
	if len(snaps) == 0 {
		return nil, fmt.Errorf("template %s has no snapshot for host class %s", templateID, hostClass)
	}
	return snaps[len(snaps)-1], nil
}

func short(id string) string {
	if len(id) > 8 {
		return id[:8]
	}
	return id
}

// hostIfUsable returns a VM's host if it can be talked to.
func (s *Server) hostIfUsable(ctx context.Context, id string) (*Host, bool) {
	if id == "" {
		return nil, false
	}
	h, err := s.getHost(ctx, id)
	if err != nil {
		return nil, false
	}
	return h, h.State != HostDown && h.State != HostIsolated && h.State != HostUnreachable
}

// createVM records a new VM and places it. The caller has filled v.
func (s *Server) createVM(ctx context.Context, v *VM, req placeReq) (map[string]any, error) {
	run, err := s.beginOp(ctx, v.ID, v.Tenant, OpCreate, 0)
	if err != nil {
		return nil, err
	}
	info, err := s.createLocked(run, v, req)
	run.end(err)
	return info, err
}

func (s *Server) createLocked(run *opRun, v *VM, req placeReq) (map[string]any, error) {
	ctx := run.ctx()
	if err := s.insertVM(ctx, v); err != nil {
		return nil, err
	}
	run.step("placing")
	_, info, err := s.placeAndCreate(ctx, v, req, nil)
	if err != nil {
		s.deleteVMRow(context.WithoutCancel(ctx), v.ID)
		return nil, err
	}
	return info, nil
}

// lockedVM starts an operation on a VM and reloads it under the lock: the
// caller's copy may be stale (another operation may have just changed it).
func (s *Server) lockedVM(ctx context.Context, id, kind string, wait time.Duration) (*opRun, *VM, error) {
	v, err := s.getVM(ctx, id)
	if err != nil {
		if isNotFound(err) {
			return nil, nil, &apiError{http.StatusNotFound, "VM not found"}
		}
		return nil, nil, err
	}
	run, err := s.beginOp(ctx, id, v.Tenant, kind, wait)
	if err != nil {
		return nil, nil, err
	}
	v, err = s.getVM(run.ctx(), id)
	if err != nil {
		if isNotFound(err) {
			err = &apiError{http.StatusNotFound, "VM not found"}
		}
		run.end(err)
		return nil, nil, err
	}
	guard, gerr := s.lockVM(run.ctx(), "_placement", vmLockWait)
	if gerr != nil {
		run.end(gerr)
		return nil, nil, gerr
	}
	if v.HostID != "" {
		host, herr := s.getHost(guard.ctx, v.HostID)
		if (herr != nil && !isNotFound(herr)) || (herr == nil && host.HandoverToken != "" && host.HandoverUntil > nowMs()) {
			guard.Release()
			err := &apiError{http.StatusConflict, "host agent upgrade in progress or host unavailable"}
			run.end(err)
			return nil, nil, err
		}
	}
	guard.Release()
	return run, v, nil
}

// deleteVM removes a VM everywhere. A VM on a dead host is evicted when the
// host comes back (heartbeat reconciliation).
func (s *Server) deleteVM(ctx context.Context, v *VM) error {
	run, v, err := s.lockedVM(ctx, v.ID, OpDelete, vmLockWait)
	if err != nil {
		if e, ok := err.(*apiError); ok && e.Status == http.StatusNotFound {
			return nil // already gone
		}
		return err
	}
	err = s.deleteLocked(run, v)
	run.end(err)
	return err
}

func (s *Server) deleteLocked(run *opRun, v *VM) error {
	ctx := run.ctx()
	hostGone := true
	if h, ok := s.hostIfUsable(ctx, v.HostID); ok {
		if err := s.deleteOnHost(ctx, h, v.ID); err != nil {
			return err
		}
		hostGone = false
	}
	if err := s.deleteVMRow(ctx, v.ID); err != nil {
		return err
	}
	if hostGone {
		s.emit(ctx, "vm.deleted", v.Tenant, v.ID, v.HostID, map[string]any{"vm_id": v.ID})
	}
	return nil
}

// pauseVM stops a VM and stores its full state in the object store, so it
// can be resumed on any host of the same class.
func (s *Server) pauseVM(ctx context.Context, v *VM) error {
	run, v, err := s.lockedVM(ctx, v.ID, OpPause, vmLockWait)
	if err != nil {
		return err
	}
	err = s.pauseLocked(run, v)
	run.end(err)
	return err
}

func (s *Server) pauseLocked(run *opRun, v *VM) error {
	ctx := run.ctx()
	if v.State != VMRunning {
		return &apiError{http.StatusConflict, fmt.Sprintf("VM is %s, not running", apiState(v.State))}
	}
	h, ok := s.hostIfUsable(ctx, v.HostID)
	if !ok {
		return &apiError{http.StatusServiceUnavailable, "the VM's host is unreachable"}
	}
	snapID := "pause-" + uuid.NewString()
	if err := run.step("pausing", "snapshot", snapID); err != nil {
		return err
	}
	res, err := s.pauseOnHost(ctx, h, v.ID, snapshotReq{SnapshotID: snapID, Kind: "pause", Upload: "sync"})
	if err != nil {
		return err
	}
	run.step("paused_on_host")
	if err := s.insertSnapshot(ctx, &Snapshot{ID: snapID, Kind: "pause", VMID: v.ID, Tenant: v.Tenant, Backend: h.Backend,
		HostClass: h.HostClass, SourceHost: h.ID, Uploaded: res.Snapshot.Uploaded, Bytes: res.Snapshot.Bytes}); err != nil {
		return err
	}
	return s.updateVM(ctx, v.ID, vmUpdate{State: str(VMPaused), PauseID: str(snapID)})
}

// resumeVM resumes on the VM's own host when it can (its files are still
// there), otherwise restores the pause snapshot on another host.
func (s *Server) resumeVM(ctx context.Context, v *VM) (*VM, error) {
	run, v, err := s.lockedVM(ctx, v.ID, OpResume, vmLockWait)
	if err != nil {
		return nil, err
	}
	err = s.resumeLocked(run, v)
	run.end(err)
	if err != nil {
		return nil, err
	}
	return s.getVM(ctx, v.ID)
}

func (s *Server) resumeLocked(run *opRun, v *VM) error {
	ctx := run.ctx()
	if v.State != VMPaused {
		return &apiError{http.StatusConflict, fmt.Sprintf("VM is %s, not paused", apiState(v.State))}
	}
	old := v.HostID
	if err := run.step("in_place", "source_host", old, "snapshot", v.PauseID, "reason", "resume"); err != nil {
		return err
	}
	if h, ok := s.hostIfUsable(ctx, v.HostID); ok && !h.Cordoned && h.State == HostReady {
		info, err := s.resumeOnHost(ctx, h, v.ID)
		if err == nil {
			// Its pause snapshot is no longer needed (GC reclaims it).
			return s.updateVM(ctx, v.ID, vmUpdate{State: str(VMRunning), Info: info, PauseID: str("")})
		}
		log.Printf("resume %s on its host %s failed (%v); restoring elsewhere", short(v.ID), h.Name, err)
	}
	if v.PauseID == "" {
		return &apiError{http.StatusConflict, "VM has no pause snapshot to restore from"}
	}
	if err := run.step("restoring"); err != nil {
		return err
	}
	s.updateVM(ctx, v.ID, vmUpdate{State: str(VMMigrating)})
	h, _, err := s.placeAndCreate(ctx, v, placeReq{PreferHost: "", Exclude: map[string]bool{old: true}}, &snapshotRef{ID: v.PauseID})
	if err != nil {
		s.updateVM(ctx, v.ID, vmUpdate{State: str(VMPaused), HostID: str(old)})
		return err
	}
	if oh, ok := s.hostIfUsable(ctx, old); ok {
		s.deleteOnHost(ctx, oh, v.ID) // drop its stale paused copy
	}
	s.updateVM(ctx, v.ID, vmUpdate{PauseID: str("")})
	s.emit(ctx, "vm.moved", v.Tenant, v.ID, h.ID, map[string]any{"vm_id": v.ID, "from_host": old, "to_host": h.ID, "reason": "resume"})
	return nil
}

// forkVM checkpoints a running VM in place and starts count copies of it.
// Children go wherever there is room; they fetch the snapshot from the source
// host directly while it uploads. All-or-nothing.
func (s *Server) forkVM(ctx context.Context, v *VM, count int) ([]*VM, error) {
	run, v, err := s.lockedVM(ctx, v.ID, OpFork, vmLockWait)
	if err != nil {
		return nil, err
	}
	children, err := s.forkLocked(run, v, count)
	run.end(err)
	return children, err
}

func (s *Server) forkLocked(run *opRun, v *VM, count int) ([]*VM, error) {
	ctx := run.ctx()
	if v.State != VMRunning {
		return nil, &apiError{http.StatusConflict, fmt.Sprintf("VM is %s, not running", apiState(v.State))}
	}
	h, ok := s.hostIfUsable(ctx, v.HostID)
	if !ok {
		return nil, &apiError{http.StatusServiceUnavailable, "the VM's host is unreachable"}
	}
	if err := s.checkQuota(ctx, v.Tenant, count); err != nil {
		return nil, err
	}
	snapID := "fork-" + uuid.NewString()
	if err := run.step("checkpointing", "snapshot", snapID); err != nil {
		return nil, err
	}
	t0 := time.Now()
	res, err := s.checkpointOnHost(ctx, h, v.ID, snapshotReq{SnapshotID: snapID, Kind: "fork", Upload: "async"})
	if err != nil {
		return nil, err
	}
	sn := &Snapshot{ID: snapID, Kind: "fork", VMID: v.ID, Tenant: v.Tenant, Backend: h.Backend, HostClass: h.HostClass,
		SourceHost: h.ID, Uploaded: false, Bytes: res.Snapshot.Bytes}
	if err := s.insertSnapshot(ctx, sn); err != nil {
		return nil, err
	}
	log.Printf("fork %s: checkpoint %s on %s in %s", short(v.ID), snapID, h.Name, time.Since(t0))
	var children []*VM
	var ids []string
	undo := func() {
		c := context.WithoutCancel(ctx)
		for _, id := range ids {
			if cv, err := s.getVM(c, id); err == nil {
				s.dropPartial(c, cv.HostID, cv.ID)
				s.deleteVMRow(c, cv.ID)
			}
		}
	}
	for i := 0; i < count; i++ {
		c := &VM{ID: uuid.NewString(), Tenant: v.Tenant, State: VMPending, TemplateID: v.TemplateID, Availability: v.Availability,
			Spec: v.Spec, Labels: map[string]string{"forked_from": v.ID}}
		if v.Availability == AvailDurable {
			c.CheckpointID = snapID // recoverable from the fork point until its first own checkpoint
		}
		ids = append(ids, c.ID)
		// Recorded before the child exists, so a takeover can find and remove it.
		if err := run.step("creating_children", "children", strings.Join(ids, ",")); err != nil {
			undo()
			return nil, err
		}
		if err := s.insertVM(ctx, c); err != nil {
			undo()
			return nil, err
		}
		// The source's host has every chunk locally, so it's preferred; others fetch from it.
		if _, _, err := s.placeAndCreate(ctx, c, placeReq{PreferHost: h.ID}, &snapshotRef{ID: snapID, Peers: []string{h.SnapdURL}}); err != nil {
			undo()
			return nil, err
		}
		got, _ := s.getVM(ctx, c.ID)
		children = append(children, got)
	}
	return children, nil
}

// checkpointVM snapshots a durable VM so it can be restored after a host
// loss. wait=0 skips a VM another operation is busy with (the scheduler
// will come back to it).
func (s *Server) checkpointVM(ctx context.Context, v *VM, wait time.Duration) error {
	run, v, err := s.lockedVM(ctx, v.ID, OpCheckpoint, wait)
	if err != nil {
		if wait == 0 && isBusy(err) {
			return nil
		}
		return err
	}
	err = s.checkpointLocked(run, v)
	run.end(err)
	return err
}

func (s *Server) checkpointLocked(run *opRun, v *VM) error {
	ctx := run.ctx()
	h, ok := s.hostIfUsable(ctx, v.HostID)
	if !ok || v.State != VMRunning {
		return nil
	}
	snapID := "ckpt-" + uuid.NewString()
	if err := run.step("checkpointing", "snapshot", snapID); err != nil {
		return err
	}
	res, err := s.checkpointOnHost(ctx, h, v.ID, snapshotReq{SnapshotID: snapID, Kind: "checkpoint", Parent: v.CheckpointID, Upload: "sync"})
	if err != nil {
		return err
	}
	if err := s.insertSnapshot(ctx, &Snapshot{ID: snapID, Kind: "checkpoint", VMID: v.ID, Tenant: v.Tenant, Backend: h.Backend,
		HostClass: h.HostClass, SourceHost: h.ID, Uploaded: res.Snapshot.Uploaded, Bytes: res.Snapshot.Bytes}); err != nil {
		return err
	}
	now := nowMs()
	if err := s.updateVM(ctx, v.ID, vmUpdate{CheckpointID: str(snapID), CheckpointAt: &now}); err != nil {
		return err
	}
	s.emit(ctx, "vm.checkpointed", v.Tenant, v.ID, h.ID, map[string]any{"vm_id": v.ID, "snapshot_id": snapID, "bytes": res.Snapshot.Bytes})
	return nil
}

// migrateVM moves a running VM to another host: pause + export, restore
// elsewhere, drop the old copy. Downtime is the snapshot plus the restore.
func (s *Server) migrateVM(ctx context.Context, v *VM, target string, reason string) (*VM, error) {
	run, v, err := s.lockedVM(ctx, v.ID, OpMigrate, vmLockWait)
	if err != nil {
		return nil, err
	}
	err = s.migrateLocked(run, v, target, reason)
	run.end(err)
	if err != nil {
		return nil, err
	}
	return s.getVM(ctx, v.ID)
}

func (s *Server) migrateLocked(run *opRun, v *VM, target string, reason string) error {
	ctx := run.ctx()
	src, ok := s.hostIfUsable(ctx, v.HostID)
	if !ok {
		return &apiError{http.StatusServiceUnavailable, "the VM's host is unreachable"}
	}
	if v.State == VMPaused {
		// Nothing runs: just forget the old host so resume places it anywhere.
		s.deleteOnHost(ctx, src, v.ID)
		return s.updateVM(ctx, v.ID, vmUpdate{HostID: str("")})
	}
	if v.State != VMRunning {
		return &apiError{http.StatusConflict, fmt.Sprintf("VM is %s", apiState(v.State))}
	}
	// Check there is somewhere to go before stopping it.
	req := placeReq{ReservationVM: v.ID, Resources: vmResources(v), TemplateID: v.TemplateID, HostClass: src.HostClass, Backend: src.Backend, Exclude: map[string]bool{src.ID: true}}
	if target != "" {
		for _, h := range mustHosts(s.listHosts(ctx)) {
			if h.ID != target {
				req.Exclude[h.ID] = true
			}
		}
	}
	if _, err := s.candidates(ctx, req); err != nil {
		return fmt.Errorf("%w: %s", errNoCapacity, strings.TrimPrefix(err.Error(), "capacity_exhausted: "))
	}
	if meta := liveMaintenance(v); meta["root_disk"] != nil {
		cands, err := s.candidates(ctx, req)
		if err != nil {
			return err
		}
		return s.liveMigrate(run, v, src, cands[0], reason)
	}
	t0 := time.Now()
	snapID := "move-" + uuid.NewString()
	if err := run.step("pausing", "source_host", src.ID, "snapshot", snapID, "reason", reason); err != nil {
		return err
	}
	res, err := s.pauseOnHost(ctx, src, v.ID, snapshotReq{SnapshotID: snapID, Kind: "pause", Upload: "async"})
	if err != nil {
		return err
	}
	s.insertSnapshot(ctx, &Snapshot{ID: snapID, Kind: "pause", VMID: v.ID, Tenant: v.Tenant, Backend: src.Backend,
		HostClass: src.HostClass, SourceHost: src.ID, Uploaded: res.Snapshot.Uploaded, Bytes: res.Snapshot.Bytes})
	if err := run.step("restoring"); err != nil {
		s.resumeInPlace(context.WithoutCancel(ctx), v)
		return err
	}
	s.updateVM(ctx, v.ID, vmUpdate{State: str(VMMigrating), PauseID: str(snapID)})
	h, _, err := s.placeAndCreate(ctx, v, req, &snapshotRef{ID: snapID, Peers: []string{src.SnapdURL}})
	if err != nil {
		// Roll back: it's still intact on the source.
		log.Printf("migrate %s: restore elsewhere failed (%v), resuming on %s", short(v.ID), err, src.Name)
		s.updateVM(ctx, v.ID, vmUpdate{HostID: str(src.ID), State: str(VMPaused)})
		if info, rerr := s.resumeOnHost(ctx, src, v.ID); rerr == nil {
			s.updateVM(ctx, v.ID, vmUpdate{State: str(VMRunning), Info: info})
		}
		return err
	}
	s.deleteOnHost(ctx, src, v.ID)
	s.updateVM(ctx, v.ID, vmUpdate{PauseID: str("")})
	s.emit(ctx, "vm.moved", v.Tenant, v.ID, h.ID, map[string]any{"vm_id": v.ID, "from_host": src.ID, "to_host": h.ID,
		"reason": reason, "downtime_ms": time.Since(t0).Milliseconds()})
	return nil
}

func mustHosts(h []*Host, _ error) []*Host { return h }

// recoverVM restores a VM whose host failed (or whose VMM crashed):
// durable from its last checkpoint, restartable fresh from its template.
// It skips a VM another operation holds; retryRecoveries comes back to it.
func (s *Server) recoverVM(ctx context.Context, v *VM, failedHost string, sameHostOK bool) error {
	run, v, err := s.lockedVM(ctx, v.ID, OpRecover, 0)
	if err != nil {
		if isBusy(err) {
			return nil
		}
		return err
	}
	err = s.recoverLocked(run, v, failedHost, sameHostOK)
	run.end(err)
	return err
}

func (s *Server) recoverLocked(run *opRun, v *VM, failedHost string, sameHostOK bool) error {
	ctx := run.ctx()
	if v.State != VMRecovering {
		return nil // recovered, deleted or moved meanwhile
	}
	var snap *snapshotRef
	from := "template"
	if v.Availability == AvailDurable && v.CheckpointID != "" {
		if sn, err := s.getSnapshot(ctx, v.CheckpointID); err == nil && (sn.Uploaded || sn.Kind == "fork") {
			snap = &snapshotRef{ID: sn.ID}
			from = "checkpoint"
		}
	}
	if err := run.step("placing", "failed_host", failedHost); err != nil {
		return err
	}
	req := placeReq{}
	if sameHostOK {
		req.PreferHost = failedHost // local restart first (Masakari-style)
		if h, ok := s.hostIfUsable(ctx, failedHost); ok {
			s.deleteOnHost(ctx, h, v.ID) // clear the crashed instance so the ID is free
		}
	} else if failedHost != "" {
		req.Exclude = map[string]bool{failedHost: true}
	}
	h, _, err := s.placeAndCreate(ctx, v, req, snap)
	if err != nil {
		s.updateVM(ctx, v.ID, vmUpdate{State: str(VMRecovering), HostID: str(""), Error: str("recovery pending: " + err.Error())})
		return err
	}
	payload := map[string]any{"vm_id": v.ID, "from": from, "failed_host": failedHost, "host_id": h.ID}
	if from == "checkpoint" {
		payload["checkpoint_at"] = float64(v.CheckpointAt) / 1000
	}
	s.emit(ctx, "vm.recovered", v.Tenant, v.ID, h.ID, payload)
	log.Printf("recovered %s on %s from %s", short(v.ID), h.Name, from)
	return nil
}

// apiError carries an HTTP status through the operation layer.
type apiError struct {
	Status int
	Detail string
}

func (e *apiError) Error() string { return e.Detail }

// statusFor maps an operation error to an HTTP status.
func statusFor(err error) (int, string) {
	var ae *apiError
	if errors.As(err, &ae) {
		return ae.Status, ae.Detail
	}
	var qe *quotaError
	if errors.As(err, &qe) {
		return http.StatusTooManyRequests, qe.Error()
	}
	if errors.Is(err, errNoCapacity) {
		return http.StatusServiceUnavailable, err.Error()
	}
	var ag *AgentError
	if errors.As(err, &ag) {
		if ag.Status >= 400 && ag.Status < 500 {
			return ag.Status, ag.Detail
		}
		return http.StatusBadGateway, err.Error()
	}
	return http.StatusInternalServerError, err.Error()
}
