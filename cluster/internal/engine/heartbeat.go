package engine

import (
	"context"
	"log"

	"github.com/jverre/flint/cluster/internal/hostagent"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// disappearedGrace: a running VM a host stops reporting is only taken as
// deleted on the host once its catalog record is this old (ms), so a VM
// placed just now isn't mistaken for one that vanished.
const disappearedGrace = 20000

// ReconcileHeartbeat folds a host's heartbeat (its lease is already
// renewed) into the catalog: the host is back in service, template builds
// it finished are recorded, and its VMs are reconciled. It returns the VMs
// the host must delete: ones the catalog has moved elsewhere or deleted
// while the host was away.
func (e *Engine) ReconcileHeartbeat(ctx context.Context, h *model.Host, hb *hostagent.Heartbeat) []string {
	switch h.State {
	case model.HostJoining, model.HostUnreachable, model.HostIsolated, model.HostDown:
		e.setHostState(ctx, h, model.HostReady)
		e.Emit(ctx, "host.ready", "", "", h.ID, map[string]any{"previous": h.State})
		log.Printf("host %s is back (was %s)", h.Name, h.State)
	}
	e.syncTemplates(ctx, h, hb.Templates)

	// VMs an operation holds are its business; if we can't tell which, skip
	// this round of VM reconciliation.
	evict := []string{}
	busy, err := e.BusyVMs(ctx)
	if err != nil {
		log.Printf("heartbeat %s: %v", h.Name, err)
		return evict
	}
	reported := map[string]bool{}
	for _, hv := range hb.VMs {
		reported[hv.ID] = true
		v, err := e.store.GetVM(ctx, hv.ID)
		if err == nil && (v.State == model.VMMigrating || busy[v.ID]) {
			continue // an operation owns it (both hosts may hold it until a move commits or rolls back)
		}
		if err != nil || v.HostID != h.ID {
			// Deleted, or now owned by another host (it was recovered or moved
			// while this host was away): the stale copy must go.
			if err == nil || store.IsNotFound(err) {
				evict = append(evict, hv.ID)
			}
			continue
		}
		e.reconcileReportedVM(ctx, h, v, hv)
	}
	// VMs the catalog places here that the host no longer has were deleted
	// locally (timeout policy, error cleanup).
	mine, err := e.store.ListVMs(ctx, store.VMFilter{HostID: h.ID, States: []model.VMState{model.VMRunning}})
	if err != nil {
		log.Printf("heartbeat %s: %v", h.Name, err)
		return evict
	}
	now := model.NowMs()
	for _, v := range mine {
		if reported[v.ID] || now-v.UpdatedAt < disappearedGrace || busy[v.ID] {
			continue
		}
		if v.VMSetID != "" {
			logErr("marking "+short(v.ID)+" lost", e.store.UpdateVM(ctx, v.ID,
				store.VMUpdate{State: store.Ptr(model.VMLost), Error: store.Ptr("disappeared from its host")}))
			continue
		}
		if err := e.store.DeleteVM(ctx, v.ID); err != nil {
			log.Printf("removing %s: %v", short(v.ID), err)
			continue
		}
		log.Printf("vm %s no longer on host %s; removed from catalog", short(v.ID), h.Name)
	}
	return evict
}

// reconcileReportedVM folds a host's view of one of its VMs into the catalog.
func (e *Engine) reconcileReportedVM(ctx context.Context, h *model.Host, v *model.VM, hv hostagent.ReportedVM) {
	if len(hv.Resources) > 0 {
		if v.Info == nil {
			v.Info = map[string]any{}
		}
		v.Info["resources"] = hv.Resources
		v.Info["maintenance"] = hv.Maintenance
		logErr("recording report of "+short(v.ID), e.store.UpdateVM(ctx, v.ID, store.VMUpdate{Info: v.Info}))
	}
	switch {
	case v.State == model.VMRunning && hv.State.Failed():
		// The VMM died on a healthy host: recover in place if the class allows.
		if v.Availability == model.AvailEphemeral || v.VMSetID != "" {
			e.markLost(ctx, v, h.ID, "VM process died", "crashed")
			if hh, ok := e.HostIfUsable(ctx, h.ID); ok {
				go e.dropPartial(context.Background(), hh.ID, v.ID)
			}
			return
		}
		if err := e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMRecovering), Error: store.Ptr("crashed on " + h.Name)}); err != nil {
			log.Printf("marking %s for recovery: %v", short(v.ID), err)
			return
		}
		e.Emit(ctx, "vm.crashed", v.Tenant, v.ID, h.ID, map[string]any{"vm_id": v.ID})
		v.State = model.VMRecovering
		go func() { logErr("recovering "+short(v.ID), e.recoverVM(context.Background(), v, h.ID, true)) }()
	case v.State == model.VMLost && v.VMSetID == "" && hv.State.Up():
		// Its host was only partitioned and the VM kept running: it's not lost after all.
		logErr("recording "+short(v.ID)+" found", e.store.UpdateVM(ctx, v.ID, store.VMUpdate{State: store.Ptr(model.VMRunning), Error: store.Ptr("")}))
		e.Emit(ctx, "vm.found", v.Tenant, v.ID, h.ID, map[string]any{"vm_id": v.ID})
	case v.State == model.VMRunning && hv.State == hostagent.VMPaused:
		// Paused on the host by its own timeout policy.
		upd := store.VMUpdate{State: store.Ptr(model.VMPaused)}
		if hv.PauseSnapshotID != "" {
			upd.PauseID = &hv.PauseSnapshotID
			logErr("recording snapshot "+hv.PauseSnapshotID, e.store.InsertSnapshot(ctx, &model.Snapshot{ID: hv.PauseSnapshotID,
				Kind: model.SnapshotPause, VMID: v.ID, Tenant: v.Tenant, Backend: h.Backend, HostClass: h.HostClass, SourceHost: h.ID, Uploaded: true}))
		}
		logErr("recording pause of "+short(v.ID), e.store.UpdateVM(ctx, v.ID, upd))
	}
}

// syncTemplates records template snapshots hosts publish and finishes builds.
func (e *Engine) syncTemplates(ctx context.Context, h *model.Host, ts []model.HostTemplate) {
	for _, t := range ts {
		if t.ID == model.DefaultTemplate {
			continue
		}
		tmpl, err := e.store.GetTemplate(ctx, t.ID)
		if err != nil {
			continue // a template only this host knows (built before it joined)
		}
		if t.SnapshotID != "" {
			if _, err := e.store.GetSnapshot(ctx, t.SnapshotID); store.IsNotFound(err) {
				logErr("recording template snapshot "+t.SnapshotID, e.store.InsertSnapshot(ctx, &model.Snapshot{ID: t.SnapshotID,
					Kind: model.SnapshotTemplate, TemplateID: t.ID, Tenant: tmpl.Tenant, Backend: h.Backend, HostClass: h.HostClass,
					SourceHost: h.ID, Uploaded: true}))
			}
		}
		built := t.Status == model.TemplateReady && t.SnapshotID != "" || t.Status == model.TemplateError
		if tmpl.BuildHost == h.ID && tmpl.Status == model.TemplateBuilding && built {
			if err := e.store.SetTemplateStatus(ctx, t.ID, t.Status, t.Error); err != nil {
				log.Printf("template %s: %v", t.ID, err)
				continue
			}
			e.Emit(ctx, "template."+string(t.Status), tmpl.Tenant, "", h.ID, map[string]any{"template_id": t.ID})
		}
	}
}
