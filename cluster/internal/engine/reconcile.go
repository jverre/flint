package engine

import (
	"context"
	"encoding/json"
	"log"
	"sync"
	"time"

	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/snapstore"
	"github.com/jverre/flint/cluster/internal/store"
)

// RunReconcilers runs on the leader replica only, until ctx is done. Each
// reconciler has its own loop, so a slow one (a VM set creating members)
// never delays failure detection, and none overlaps itself.
func (e *Engine) RunReconcilers(ctx context.Context) {
	go every(ctx, time.Second, e.checkHosts)
	go every(ctx, 2*time.Second, e.ResumeInterruptedOps)
	go every(ctx, 2*time.Second, e.retryRecoveries)
	go every(ctx, 2*time.Second, e.reconcileVMSets)
	go every(ctx, 2*time.Second, e.drainHosts)
	go every(ctx, 5*time.Second, e.scheduleCheckpoints)
	go every(ctx, 3*time.Second, e.markUploads)
	go every(ctx, time.Minute, e.collectGarbage)
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
func (e *Engine) storageHeartbeatAge(ctx context.Context, hostID string) (time.Duration, bool) {
	b, err := e.remote.Get(ctx, "heartbeats/"+hostID)
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

func (e *Engine) setHostState(ctx context.Context, h *model.Host, state model.HostState) {
	logErr("moving host "+h.Name+" to "+string(state), e.store.SetHostState(ctx, h.ID, state))
}

// checkHosts moves silent hosts through unreachable -> isolated|down and
// recovers their VMs once they're certainly fenced.
func (e *Engine) checkHosts(ctx context.Context) {
	hosts, err := e.store.ListHosts(ctx)
	if err != nil {
		return
	}
	lease := e.cfg.LeaseTTL
	for _, h := range hosts {
		silent := time.Since(time.UnixMilli(h.LastHeartbeat))
		if silent < lease {
			continue
		}
		switch h.State {
		case model.HostJoining:
			// First start builds the golden snapshot before heartbeating.
			if silent > joinGrace {
				e.setHostState(ctx, h, model.HostDown)
				e.Emit(ctx, "host.down", "", "", h.ID, map[string]any{"reason": "never sent a heartbeat after joining"})
			}
			continue
		case model.HostReady, model.HostDraining, model.HostDrained:
			e.setHostState(ctx, h, model.HostUnreachable)
			e.Emit(ctx, "host.unreachable", "", "", h.ID, map[string]any{"silent_ms": silent.Milliseconds()})
			log.Printf("host %s unreachable (no heartbeat for %s)", h.Name, silent.Round(time.Millisecond))
			continue
		case model.HostUnreachable:
		case model.HostIsolated:
			// Still isolated: if storage heartbeats stop too, it's down.
			if age, ok := e.storageHeartbeatAge(ctx, h.ID); !ok || age > lease {
				e.hostDown(ctx, h)
			}
			continue
		default:
			continue
		}
		// The host's agent stops renewing its fence at lease expiry and its
		// VMs with leases die by then; wait a margin on top of that.
		if silent < lease+e.cfg.FenceMargin {
			continue
		}
		if age, ok := e.storageHeartbeatAge(ctx, h.ID); ok && age < lease {
			e.hostIsolated(ctx, h)
		} else {
			e.hostDown(ctx, h)
		}
	}
}

// hostIsolated: the host is alive but can't reach us. Its durable and
// restartable VMs have been fenced (the agent lost their leases), so they are
// recovered elsewhere; ephemeral VMs keep running there.
func (e *Engine) hostIsolated(ctx context.Context, h *model.Host) {
	e.setHostState(ctx, h, model.HostIsolated)
	e.Emit(ctx, "host.isolated", "", "", h.ID, nil)
	log.Printf("host %s isolated: no heartbeats but storage heartbeats continue", h.Name)
	e.failHostVMs(ctx, h, false)
}

func (e *Engine) hostDown(ctx context.Context, h *model.Host) {
	e.setHostState(ctx, h, model.HostDown)
	e.Emit(ctx, "host.down", "", "", h.ID, nil)
	log.Printf("host %s down", h.Name)
	e.failHostVMs(ctx, h, true)
}

// markLost records that a VM is gone for good (a VM set replaces it).
func (e *Engine) markLost(ctx context.Context, v *model.VM, hostID, why, reason string) {
	logErr("marking "+short(v.ID)+" lost", e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMLost), Error: &why}))
	e.Emit(ctx, "vm.lost", v.Tenant, v.ID, hostID, map[string]any{"vm_id": v.ID, "reason": reason})
}

func (e *Engine) failHostVMs(ctx context.Context, h *model.Host, down bool) {
	vms, err := e.store.ListVMs(ctx, store.VMFilter{HostID: h.ID})
	if err != nil {
		return
	}
	var wg sync.WaitGroup
	for _, v := range vms {
		switch {
		case v.State == model.VMPaused:
			// Its state is in the object store: it can resume anywhere.
			if down {
				logErr("detaching paused "+short(v.ID), e.store.UpdateVM(ctx, v.ID, store.VMUpdate{HostID: store.Ptr("")}))
			}
		case v.Availability == model.AvailEphemeral && !down:
			// Isolation response for ephemeral VMs: leave them running.
		case v.VMSetID != "" || v.Availability == model.AvailEphemeral:
			e.markLost(ctx, v, h.ID, "host "+h.Name+" failed", "host "+map[bool]string{true: "down", false: "isolated"}[down])
		default:
			if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMRecovering), Error: store.Ptr("host " + h.Name + " failed")}); err != nil {
				log.Printf("marking %s for recovery: %v", short(v.ID), err)
				continue // retried next round: the host is still down
			}
			v.State = model.VMRecovering
			wg.Add(1)
			go func(v *model.VM) {
				defer wg.Done()
				logErr("recovering "+short(v.ID), e.recoverVM(ctx, v, h.ID, false))
			}(v)
		}
	}
	wg.Wait()
}

// retryRecoveries retries VMs whose recovery found no capacity earlier.
func (e *Engine) retryRecoveries(ctx context.Context) {
	vms, err := e.store.ListVMs(ctx, store.VMFilter{States: []model.VMState{model.VMRecovering}, UpdatedBefore: model.NowMs() - 3000})
	if err != nil {
		return
	}
	for _, v := range vms {
		go e.recoverVM(ctx, v, v.HostID, false)
	}
}

// scheduleCheckpoints checkpoints durable VMs whose last one is too old.
func (e *Engine) scheduleCheckpoints(ctx context.Context) {
	vms, err := e.store.ListVMs(ctx, store.VMFilter{States: []model.VMState{model.VMRunning}, Availability: model.AvailDurable,
		CheckpointBefore: model.NowMs() - e.cfg.CheckpointInterval.Milliseconds()})
	if err != nil {
		return
	}
	sem := make(chan struct{}, 4)
	for _, v := range vms {
		sem <- struct{}{}
		go func(v *model.VM) {
			defer func() { <-sem }()
			if _, err := e.CheckpointVM(ctx, v, 0); err != nil {
				log.Printf("checkpoint %s: %v", short(v.ID), err)
			}
		}(v)
	}
}

// markUploads flips snapshots uploaded asynchronously (forks, moves) once
// their manifest is in the object store.
func (e *Engine) markUploads(ctx context.Context) {
	snaps, err := e.store.ListSnapshots(ctx, store.SnapshotFilter{PendingOnly: true, CreatedBefore: model.NowMs() - 2000})
	if err != nil {
		return
	}
	for _, sn := range snaps {
		if ok, err := e.remote.Exists(ctx, "manifests/"+sn.ID+".json"); err == nil && ok {
			logErr("marking "+sn.ID+" uploaded", e.store.MarkSnapshotUploaded(ctx, sn.ID))
		}
	}
}

// collectGarbage deletes snapshots nothing needs any more, then their chunks.
func (e *Engine) collectGarbage(ctx context.Context) {
	e.pruneOps(ctx)
	snaps, err := e.store.ListSnapshots(ctx, store.SnapshotFilter{})
	if err != nil {
		return
	}
	vms, err := e.store.ListVMs(ctx, store.VMFilter{})
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
	recent := model.NowMs() - 10*60*1000
	for _, sn := range snaps {
		if sn.Kind == model.SnapshotTemplate || live[sn.ID] || sn.CreatedAt > recent {
			live[sn.ID] = true
			continue
		}
		if err := e.remote.Delete(ctx, "manifests/"+sn.ID+".json"); err == nil {
			logErr("forgetting snapshot "+sn.ID, e.store.DeleteSnapshot(ctx, sn.ID))
		}
	}
	res, err := snapstore.GC(ctx, e.remote, live, time.Hour)
	if err != nil {
		log.Printf("gc: %v", err)
		return
	}
	if res.ManifestsDeleted+res.ChunksDeleted > 0 {
		log.Printf("gc: %d manifests, %d chunks deleted", res.ManifestsDeleted, res.ChunksDeleted)
	}
}
