package engine

import (
	"context"
	"log"
	"sort"

	"github.com/google/uuid"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// reconcileVMSets (leader) converges every VM set to its spec.
func (e *Engine) reconcileVMSets(ctx context.Context) {
	sets, err := e.store.ListVMSets(ctx, true)
	if err != nil {
		return
	}
	for _, vs := range sets {
		if err := e.reconcileVMSet(ctx, vs); err != nil {
			log.Printf("vmset %s: %v", vs.Name, err)
		}
	}
}

// deleteMember deletes a member; a failure is retried on the next pass.
func (e *Engine) deleteMember(ctx context.Context, vs *model.VMSet, m *model.VM) {
	logErr("vmset "+vs.Name+": deleting "+short(m.ID), e.DeleteVM(ctx, m))
}

func (e *Engine) reconcileVMSet(ctx context.Context, vs *model.VMSet) error {
	members, err := e.store.ListVMs(ctx, store.VMFilter{VMSetID: vs.ID})
	if err != nil {
		return err
	}
	if vs.Deleted {
		for _, m := range members {
			if err := e.DeleteVM(ctx, m); err != nil {
				return err // the set goes once its members have
			}
		}
		return e.store.DeleteVMSet(ctx, vs.ID)
	}
	spec := vs.Spec
	var active, standby []*model.VM
	for _, m := range members {
		switch {
		case m.State == model.VMLost || m.State == model.VMError:
			// Replaced below; the failed member is removed (evicted from its
			// host when the host returns).
			e.deleteMember(ctx, vs, m)
			e.Emit(ctx, "vmset.member_replaced", vs.Tenant, m.ID, m.HostID, map[string]any{"vmset_id": vs.ID, "vm_id": m.ID})
		case m.Role == model.RoleStandby:
			standby = append(standby, m)
		case m.State.Active():
			active = append(active, m)
		case m.State == model.VMPaused:
			standby = append(standby, m) // a paused active member counts as a spare
		}
	}

	// Too few running: promote standbys first (a snapshot restore), then create.
	for len(active) < spec.Replicas {
		var promoted *model.VM
		for len(standby) > 0 && promoted == nil {
			sb := standby[0]
			standby = standby[1:]
			if sb.State != model.VMPaused {
				continue
			}
			nv, err := e.ResumeVM(ctx, sb)
			if err != nil {
				log.Printf("vmset %s: promoting standby %s: %v", vs.Name, short(sb.ID), err)
				continue
			}
			if err := e.store.UpdateVM(ctx, nv.ID, store.VMUpdate{Role: store.Ptr(model.RoleActive)}); err != nil {
				return err
			}
			e.Emit(ctx, "vmset.standby_promoted", vs.Tenant, nv.ID, nv.HostID, map[string]any{"vmset_id": vs.ID, "vm_id": nv.ID})
			promoted = nv
		}
		if promoted == nil {
			m, err := e.createVMSetMember(ctx, vs, active, model.RoleActive)
			if err != nil {
				return err
			}
			promoted = m
		}
		active = append(active, promoted)
	}
	// Too many: remove the newest.
	if len(active) > spec.Replicas {
		sort.Slice(active, func(i, j int) bool { return active[i].CreatedAt < active[j].CreatedAt })
		for _, m := range active[spec.Replicas:] {
			e.deleteMember(ctx, vs, m)
		}
		active = active[:spec.Replicas]
	}
	// Standbys: create (then pause) or trim.
	for len(standby) < spec.Standby {
		m, err := e.createVMSetMember(ctx, vs, append(active, standby...), model.RoleStandby)
		if err != nil {
			return err
		}
		if err := e.PauseVM(ctx, m); err != nil {
			return err
		}
		if m, err = e.store.GetVM(ctx, m.ID); err != nil {
			return err
		}
		standby = append(standby, m)
	}
	for len(standby) > spec.Standby {
		e.deleteMember(ctx, vs, standby[len(standby)-1])
		standby = standby[:len(standby)-1]
	}
	return e.rollVMSet(ctx, vs, active, standby)
}

// rollVMSet replaces members on an old template, MaxUnavail at a time,
// surging a new member before removing an old one.
func (e *Engine) rollVMSet(ctx context.Context, vs *model.VMSet, active, standby []*model.VM) error {
	for _, m := range active {
		if m.State != model.VMRunning {
			return nil // wait until the set is healthy
		}
	}
	replaced := 0
	for _, m := range active {
		if m.TemplateID == vs.Spec.TemplateID || replaced >= vs.Spec.MaxUnavail {
			continue
		}
		if _, err := e.createVMSetMember(ctx, vs, active, model.RoleActive); err != nil {
			return err
		}
		e.deleteMember(ctx, vs, m)
		e.Emit(ctx, "vmset.member_updated", vs.Tenant, m.ID, m.HostID, map[string]any{"vmset_id": vs.ID, "vm_id": m.ID, "template_id": vs.Spec.TemplateID})
		replaced++
	}
	for _, m := range standby {
		if m.TemplateID != vs.Spec.TemplateID {
			e.deleteMember(ctx, vs, m) // recreated on the next pass
		}
	}
	return nil
}

// createVMSetMember places a new member, spreading members across zones or hosts.
func (e *Engine) createVMSetMember(ctx context.Context, vs *model.VMSet, siblings []*model.VM, role model.Role) (*model.VM, error) {
	if err := e.CheckQuota(ctx, vs.Tenant, 1); err != nil {
		return nil, err
	}
	hosts, err := e.store.ListHosts(ctx)
	if err != nil {
		return nil, err
	}
	byID := map[string]*model.Host{}
	for _, h := range hosts {
		byID[h.ID] = h
	}
	counts := map[string]int{}
	for _, m := range siblings {
		if h := byID[m.HostID]; h != nil {
			counts[vs.Spec.Spread.Domain(h)]++
		}
	}
	v := &model.VM{ID: uuid.NewString(), Tenant: vs.Tenant, State: model.VMPending, TemplateID: vs.Spec.TemplateID,
		Availability: vs.Spec.Availability, Spec: vs.Spec.VMParams.Spec(), VMSetID: vs.ID, Role: role,
		Labels: map[string]string{"vmset": vs.Name}}
	if _, err := e.CreateVM(ctx, v, PlaceRequest{Spread: vs.Spec.Spread, SpreadCounts: counts}); err != nil {
		return nil, err
	}
	e.Emit(ctx, "vmset.member_created", vs.Tenant, v.ID, v.HostID, map[string]any{"vmset_id": vs.ID, "vm_id": v.ID, "role": role})
	return e.store.GetVM(ctx, v.ID)
}
