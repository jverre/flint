package server

import (
	"context"
	"encoding/json"
	"log"
	"sync"
	"time"

	"github.com/jverre/flint/cluster/internal/snapstore"
)

// reconcileLoop runs on the leader replica only. Each reconciler has its own
// loop, so a slow one (a VM set creating members) never delays failure
// detection, and none overlaps itself.
func (s *Server) reconcileLoop(ctx context.Context) {
	go every(ctx, time.Second, s.checkHosts)
	go every(ctx, 2*time.Second, s.resumeInterruptedOps)
	go every(ctx, 2*time.Second, s.retryRecoveries)
	go every(ctx, 2*time.Second, s.reconcileVMSets)
	go every(ctx, 2*time.Second, s.drainHosts)
	go every(ctx, 5*time.Second, s.scheduleCheckpoints)
	go every(ctx, 3*time.Second, s.markUploads)
	go every(ctx, time.Minute, s.collectGarbage)
	<-ctx.Done()
}

func every(ctx context.Context, d time.Duration, fn func(context.Context)) {
	t := time.NewTicker(d)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			fn(ctx)
		}
	}
}

// joinGrace is how long a joined host may take to send its first heartbeat.
const joinGrace = 15 * time.Minute

// storageHeartbeatAge returns how old the host's object-store heartbeat is.
// snapd writes it independently of the agent's network path to us, so a
// host that only lost its link to the control plane still shows up here
// (vSphere's datastore heartbeat).
func (s *Server) storageHeartbeatAge(ctx context.Context, hostID string) (time.Duration, bool) {
	b, err := s.remote.Get(ctx, "heartbeats/"+hostID)
	if err != nil {
		return 0, false
	}
	var hb struct {
		TS int64 `json:"ts"`
	}
	if json.Unmarshal(b, &hb) != nil || hb.TS == 0 {
		return 0, false
	}
	return time.Since(time.UnixMilli(hb.TS)), true
}

// checkHosts moves silent hosts through unreachable -> isolated|down and
// recovers their VMs once they're certainly fenced.
func (s *Server) checkHosts(ctx context.Context) {
	hosts, err := s.listHosts(ctx)
	if err != nil {
		return
	}
	lease := s.cfg.LeaseTTL
	for _, h := range hosts {
		silent := time.Since(time.UnixMilli(h.LastHeartbeat))
		if silent < lease {
			continue
		}
		if h.State == HostJoining {
			// First start builds the golden snapshot before heartbeating.
			if silent > joinGrace {
				s.setHostState(ctx, h.ID, HostDown)
				s.emit(ctx, "host.down", "", "", h.ID, map[string]any{"reason": "never sent a heartbeat after joining"})
			}
			continue
		}
		switch h.State {
		case HostReady, HostDraining, HostDrained:
			s.setHostState(ctx, h.ID, HostUnreachable)
			s.emit(ctx, "host.unreachable", "", "", h.ID, map[string]any{"silent_ms": silent.Milliseconds()})
			log.Printf("host %s unreachable (no heartbeat for %s)", h.Name, silent.Round(time.Millisecond))
			continue
		case HostUnreachable:
		case HostIsolated:
			// Still isolated: if storage heartbeats stop too, it's down.
			if age, ok := s.storageHeartbeatAge(ctx, h.ID); !ok || age > lease {
				s.hostDown(ctx, h)
			}
			continue
		default:
			continue
		}
		// The host's agent stops renewing its fence at lease expiry and its
		// VMs with leases die by then; wait a margin on top of that.
		if silent < lease+s.cfg.FenceMargin {
			continue
		}
		if age, ok := s.storageHeartbeatAge(ctx, h.ID); ok && age < lease {
			s.hostIsolated(ctx, h)
		} else {
			s.hostDown(ctx, h)
		}
	}
}

// hostIsolated: the host is alive but can't reach us. Its durable and
// restartable VMs have been fenced (the agent lost their leases), so they are
// recovered elsewhere; ephemeral VMs keep running there.
func (s *Server) hostIsolated(ctx context.Context, h *Host) {
	s.setHostState(ctx, h.ID, HostIsolated)
	s.emit(ctx, "host.isolated", "", "", h.ID, nil)
	log.Printf("host %s isolated: no heartbeats but storage heartbeats continue", h.Name)
	s.failHostVMs(ctx, h, false)
}

func (s *Server) hostDown(ctx context.Context, h *Host) {
	s.setHostState(ctx, h.ID, HostDown)
	s.emit(ctx, "host.down", "", "", h.ID, nil)
	log.Printf("host %s down", h.Name)
	s.failHostVMs(ctx, h, true)
}

func (s *Server) failHostVMs(ctx context.Context, h *Host, down bool) {
	vms, err := s.listVMs(ctx, `host_id = ?`, h.ID)
	if err != nil {
		return
	}
	var wg sync.WaitGroup
	for _, v := range vms {
		switch {
		case v.State == VMPaused:
			// Its state is in the object store: it can resume anywhere.
			if down {
				s.updateVM(ctx, v.ID, vmUpdate{HostID: str("")})
			}
		case v.Availability == AvailEphemeral && !down:
			// Isolation response for ephemeral VMs: leave them running.
		case v.VMSetID != "" || v.Availability == AvailEphemeral:
			s.updateVM(ctx, v.ID, vmUpdate{State: str(VMLost), Error: str("host " + h.Name + " failed")})
			s.emit(ctx, "vm.lost", v.Tenant, v.ID, h.ID, map[string]any{"vm_id": v.ID, "reason": "host " + map[bool]string{true: "down", false: "isolated"}[down]})
		default:
			s.updateVM(ctx, v.ID, vmUpdate{State: str(VMRecovering), Error: str("host " + h.Name + " failed")})
			v.State = VMRecovering
			wg.Add(1)
			go func(v *VM) {
				defer wg.Done()
				s.recoverVM(ctx, v, h.ID, false)
			}(v)
		}
	}
	wg.Wait()
}

// retryRecoveries retries VMs whose recovery found no capacity earlier.
func (s *Server) retryRecoveries(ctx context.Context) {
	vms, err := s.listVMs(ctx, `state = ? AND updated_at < ?`, VMRecovering, nowMs()-3000)
	if err != nil {
		return
	}
	for _, v := range vms {
		failed := v.HostID
		go s.recoverVM(ctx, v, failed, false)
	}
}

// scheduleCheckpoints checkpoints durable VMs whose last one is too old.
func (s *Server) scheduleCheckpoints(ctx context.Context) {
	cutoff := nowMs() - s.cfg.CheckpointInterval.Milliseconds()
	vms, err := s.listVMs(ctx, `state = ? AND availability = ? AND checkpoint_at < ?`, VMRunning, AvailDurable, cutoff)
	if err != nil {
		return
	}
	sem := make(chan struct{}, 4)
	for _, v := range vms {
		sem <- struct{}{}
		go func(v *VM) {
			defer func() { <-sem }()
			if err := s.checkpointVM(ctx, v, 0); err != nil {
				log.Printf("checkpoint %s: %v", short(v.ID), err)
			}
		}(v)
	}
}

// markUploads flips snapshots uploaded asynchronously (forks, moves) once
// their manifest is in the object store.
func (s *Server) markUploads(ctx context.Context) {
	snaps, err := s.listSnapshots(ctx, `uploaded = 0 AND created_at < ?`, nowMs()-2000)
	if err != nil {
		return
	}
	for _, sn := range snaps {
		if ok, err := s.remote.Exists(ctx, "manifests/"+sn.ID+".json"); err == nil && ok {
			s.db.Exec(ctx, `UPDATE snapshots SET uploaded = 1 WHERE id = ?`, sn.ID)
		}
	}
}

// drainHosts migrates VMs off draining hosts, one at a time per host.
func (s *Server) drainHosts(ctx context.Context) {
	hosts, err := s.listHosts(ctx)
	if err != nil {
		return
	}
	for _, h := range hosts {
		if h.State != HostDraining {
			continue
		}
		vms, err := s.listVMs(ctx, `host_id = ? AND state IN (?, ?, ?, ?, ?)`, h.ID, VMRunning, VMPaused, VMPending, VMMigrating, VMRecovering)
		if err != nil {
			continue
		}
		if len(vms) == 0 {
			s.setHostState(ctx, h.ID, HostDrained)
			s.emit(ctx, "host.drained", "", "", h.ID, nil)
			log.Printf("host %s drained", h.Name)
			continue
		}
		v := vms[0]
		if v.State != VMRunning && v.State != VMPaused {
			continue
		}
		target := ""
		if v.State == VMRunning {
			if err := s.db.QueryRow(ctx, `SELECT target_host FROM drain_reservations WHERE vm_id = ? AND source_host = ?`, v.ID, h.ID).Scan(&target); err != nil {
				log.Printf("drain %s: missing reservation for %s; re-plan before moving", h.Name, v.ID)
				continue
			}
		}
		if _, err := s.migrateVM(ctx, v, target, "drain"); err != nil {
			log.Printf("drain %s: moving %s: %v", h.Name, short(v.ID), err)
		} else {
			s.db.Exec(ctx, `DELETE FROM drain_reservations WHERE vm_id = ?`, v.ID)
		}
	}
}

// collectGarbage deletes snapshots nothing needs any more, then their chunks.
func (s *Server) collectGarbage(ctx context.Context) {
	s.pruneOps(ctx)
	snaps, err := s.listSnapshots(ctx, "")
	if err != nil {
		return
	}
	vms, err := s.listVMs(ctx, "")
	if err != nil {
		return
	}
	live := map[string]bool{}
	for _, v := range vms {
		if v.CheckpointID != "" {
			live[v.CheckpointID] = true
		}
		if v.PauseID != "" {
			live[v.PauseID] = true
		}
	}
	recent := nowMs() - 10*60*1000
	for _, sn := range snaps {
		if sn.Kind == "template" || live[sn.ID] || sn.CreatedAt > recent {
			live[sn.ID] = true
			continue
		}
		if err := s.remote.Delete(ctx, "manifests/"+sn.ID+".json"); err == nil {
			s.db.Exec(ctx, `DELETE FROM snapshots WHERE id = ?`, sn.ID)
		}
	}
	res, err := snapstore.GC(ctx, s.remote, live, time.Hour)
	if err != nil {
		log.Printf("gc: %v", err)
		return
	}
	if res.ManifestsDeleted+res.ChunksDeleted > 0 {
		log.Printf("gc: %d manifests, %d chunks deleted", res.ManifestsDeleted, res.ChunksDeleted)
	}
}
