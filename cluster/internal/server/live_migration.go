package server

import (
	"context"
	"fmt"
	"net/http"
	"time"
)

// retryOperation leaves a durable operation open for the leader to retry.
// Unknown destination state must never trigger a source resume.
type retryOperation struct{ error }

func (s *Server) migrationCall(ctx context.Context, h *Host, id, action string, in, out any) error {
	return s.agentDo(ctx, h, http.MethodPost, "/vms/"+id+"/live-migration/"+action, in, out, 16*time.Minute)
}

func liveMaintenance(v *VM) map[string]any {
	value, _ := v.Info["maintenance"].(map[string]any)
	return value
}

func (s *Server) liveMigrate(run *opRun, v *VM, src, dst *Host, reason string) error {
	ctx := run.ctx()
	meta := liveMaintenance(v)
	disk, _ := meta["root_disk"].(map[string]any)
	network, _ := meta["network"].(map[string]any)
	if disk == nil || network == nil {
		return fmt.Errorf("VM lacks migration storage or networking")
	}
	guard, err := s.lockVM(ctx, "_placement", vmLockWait)
	if err != nil {
		return err
	}
	all, err := s.listHosts(guard.ctx)
	if err != nil {
		guard.Release()
		return err
	}
	exclude := map[string]bool{}
	for _, h := range all {
		if h.ID != dst.ID {
			exclude[h.ID] = true
		}
	}
	_, err = s.candidates(guard.ctx, placeReq{ReservationVM: v.ID, Resources: vmResources(v), HostClass: src.HostClass, Backend: src.Backend, Exclude: exclude})
	if err == nil {
		r := vmResources(v)
		_, err = s.db.Exec(guard.ctx, `INSERT INTO drain_reservations (vm_id,source_host,target_host,vcpus,memory_mb,disk_mb) VALUES (?,?,?,?,?,?) ON CONFLICT (vm_id) DO UPDATE SET target_host = excluded.target_host`, v.ID, src.ID, dst.ID, r.VCPUs, r.MemoryMB, r.DiskMB)
	}
	guard.Release()
	if err != nil {
		return err
	}
	if err := run.step("live_preparing", "source_host", src.ID, "target_host", dst.ID, "reason", reason); err != nil {
		return err
	}
	if err := s.updateVM(ctx, v.ID, vmUpdate{State: str(VMMigrating)}); err != nil {
		return err
	}
	var receiver map[string]any
	body := map[string]any{"source_host": src.ID, "pool_id": disk["pool_id"], "vni": disk["vni"],
		"root_disk": disk, "network": network, "resources": v.Info["resources"], "template_id": v.TemplateID, "tenant": v.Tenant,
		"internet": v.Spec.AllowInternet, "availability": v.Availability}
	if err := s.migrationCall(ctx, dst, v.ID, "prepare", body, &receiver); err != nil {
		return s.rollbackLive(run, v, src, dst, err)
	}
	if err := run.step("live_transferring"); err != nil {
		return retryOperation{err}
	}
	receiver["target_host"] = dst.ID
	var sent map[string]any
	if err := s.migrationCall(ctx, src, v.ID, "send", receiver, &sent); err != nil {
		// A lost sender reply may still have completed the cutover.
		return s.finishLive(run, v)
	}
	if err := run.step("live_cutover"); err != nil {
		return retryOperation{err}
	}
	deadline := time.Now().Add(60 * time.Second)
	for time.Now().Before(deadline) {
		var status struct {
			Phase string `json:"phase"`
		}
		if err := s.migrationCall(ctx, dst, v.ID, "status", map[string]any{}, &status); err != nil {
			return retryOperation{err}
		}
		if status.Phase == "received" {
			return s.commitLive(run, v, src, dst)
		}
		if status.Phase == "absent" {
			return s.rollbackLive(run, v, src, dst, fmt.Errorf("receiver failed"))
		}
		select {
		case <-ctx.Done():
			return retryOperation{ctx.Err()}
		case <-time.After(200 * time.Millisecond):
		}
	}
	return retryOperation{fmt.Errorf("awaiting receiver cutover confirmation")}
}

func (s *Server) commitLive(run *opRun, v *VM, src, dst *Host) error {
	var result struct {
		VM map[string]any `json:"vm"`
	}
	// Receiver remains fenced until this commit. Record ownership before
	// relaxing the temporary fence for an ephemeral VM.
	if err := s.updateVM(run.ctx(), v.ID, vmUpdate{HostID: str(dst.ID), State: str(VMRunning)}); err != nil {
		return retryOperation{err}
	}
	if err := s.migrationCall(run.ctx(), dst, v.ID, "commit", map[string]any{}, &result); err != nil {
		return retryOperation{err}
	}
	if err := s.updateVM(run.ctx(), v.ID, vmUpdate{Info: result.VM, PauseID: str("")}); err != nil {
		return retryOperation{err}
	}
	if err := s.deleteOnHost(run.ctx(), src, v.ID); err != nil && !isAgentNotFound(err) {
		return retryOperation{err}
	}
	s.db.Exec(run.ctx(), `DELETE FROM drain_reservations WHERE vm_id = ?`, v.ID)
	s.emit(run.ctx(), "vm.moved", v.Tenant, v.ID, dst.ID, map[string]any{"vm_id": v.ID, "from_host": src.ID, "to_host": dst.ID,
		"reason": run.op.Params["reason"], "mode": "live"})
	return nil
}

func (s *Server) rollbackLive(run *opRun, v *VM, src, dst *Host, cause error) error {
	// A receiver may already be executing after a lost reply. Confirm its
	// deletion before asking CH to resume the source; never guess.
	var aborted struct {
		Executed bool `json:"executed"`
	}
	if err := s.migrationCall(run.ctx(), dst, v.ID, "abort", map[string]any{}, &aborted); err != nil {
		return retryOperation{fmt.Errorf("receiver deletion unconfirmed: %w", err)}
	}
	if aborted.Executed {
		return s.failLive(run, v, src, cause)
	}
	var result struct {
		VM map[string]any `json:"vm"`
	}
	if err := s.migrationCall(run.ctx(), src, v.ID, "rollback", map[string]any{}, &result); err != nil {
		if ae, ok := err.(*AgentError); ok && ae.Status == 409 {
			return s.failLive(run, v, src, cause)
		}
		return retryOperation{err}
	}
	if err := s.updateVM(run.ctx(), v.ID, vmUpdate{HostID: str(src.ID), State: str(VMRunning), Info: result.VM}); err != nil {
		return retryOperation{err}
	}
	s.db.Exec(run.ctx(), `DELETE FROM drain_reservations WHERE vm_id = ?`, v.ID)
	return rolledBack(cause.Error())
}

func (s *Server) finishLive(run *opRun, v *VM) error {
	src, err := s.getHost(run.ctx(), run.op.Params["source_host"])
	if err != nil {
		return retryOperation{err}
	}
	dst, err := s.getHost(run.ctx(), run.op.Params["target_host"])
	if err != nil {
		return retryOperation{err}
	}
	type status struct {
		Phase    string `json:"phase"`
		VMMState string `json:"vmm_state"`
	}
	var source, target status
	if err := s.migrationCall(run.ctx(), dst, v.ID, "status", map[string]any{}, &target); err != nil {
		return retryOperation{err}
	}
	if err := s.migrationCall(run.ctx(), src, v.ID, "status", map[string]any{}, &source); err != nil {
		return retryOperation{err}
	}
	if target.Phase == "received" && (source.Phase == "sent" || source.VMMState == "Paused" || (source.Phase == "absent" && source.VMMState == "Absent")) {
		return s.commitLive(run, v, src, dst)
	}
	// Recovered operations with a transfer still underway wait for a terminal
	// state. Deleting an in-flight receiver could race CH's source rollback.
	if target.Phase == "receiving" || target.Phase == "cutover" {
		return retryOperation{fmt.Errorf("migration transfer still in flight")}
	}
	return s.rollbackLive(run, v, src, dst, fmt.Errorf("interrupted live migration"))
}

// After a receiver has executed, its disk writes may have advanced beyond the
// source's paused RAM. Fence both copies and recover a coherent checkpoint;
// never resume stale source RAM against an advanced shared disk.
func (s *Server) failLive(run *opRun, v *VM, src *Host, cause error) error {
	if err := s.migrationCall(run.ctx(), src, v.ID, "abort", map[string]any{}, nil); err != nil {
		return retryOperation{err}
	}
	state := VMLost
	if v.Availability == AvailDurable || v.Availability == AvailRestartable {
		state = VMRecovering
	}
	if err := s.updateVM(run.ctx(), v.ID, vmUpdate{HostID: str(src.ID), State: str(state), Error: str("live migration failed after possible cutover: " + cause.Error())}); err != nil {
		return retryOperation{err}
	}
	s.db.Exec(run.ctx(), `DELETE FROM drain_reservations WHERE vm_id = ?`, v.ID)
	return rolledBack("both copies fenced; coherent recovery required")
}
