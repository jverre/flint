package engine

import (
	"context"
	"fmt"
	"log"
	"sort"

	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// PlanDrain proves that every running VM has a simultaneous compatible home.
// It is read-only: no VM is paused and no host is cordoned on a failed plan.
func (e *Engine) PlanDrain(ctx context.Context, src *model.Host) ([]model.DrainMove, error) {
	vms, err := e.store.ListVMs(ctx, store.VMFilter{HostID: src.ID})
	if err != nil {
		return nil, err
	}
	busy, err := e.BusyVMs(ctx)
	if err != nil {
		return nil, err
	}
	running := []*model.VM{}
	for _, v := range vms {
		if busy[v.ID] {
			return nil, fmt.Errorf("VM %s has an operation in progress", v.ID)
		}
		switch v.State {
		case model.VMRunning:
			if !v.Resources().Known() {
				return nil, fmt.Errorf("VM %s has unknown resource requirements; wait for a fresh host report", v.ID)
			}
			running = append(running, v)
		case model.VMPaused:
			sn, err := e.store.GetSnapshot(ctx, v.PauseID)
			if err != nil || !sn.Uploaded {
				return nil, fmt.Errorf("paused VM %s has no uploaded snapshot", v.ID)
			}
		case model.VMPending, model.VMMigrating, model.VMRecovering:
			return nil, fmt.Errorf("VM %s is %s", v.ID, v.State)
		}
	}
	hosts, err := e.store.ListHosts(ctx)
	if err != nil {
		return nil, err
	}
	if err := e.subtractReservations(ctx, hosts, ""); err != nil {
		return nil, err
	}
	active, err := e.store.ActiveVMsByHost(ctx)
	if err != nil {
		return nil, err
	}
	type budget struct {
		h          *model.Host
		slots, cpu int
		mem, disk  int64
	}
	budgets := []budget{}
	for _, h := range hosts {
		req := PlaceRequest{HostClass: src.HostClass, Backend: src.Backend, Exclude: map[string]bool{src.ID: true}}
		if e.eligibility(h, active[h.ID], req, nil) != "" {
			continue
		}
		maxVMs := h.EffectiveMaxVMs()
		if h.Capacity.VCPUs <= 0 || h.Capacity.MemAvailableMB <= 0 || h.Capacity.DiskFreeMB <= 0 || maxVMs <= 0 {
			continue // Unknown is not spare capacity.
		}
		cpu := h.Capacity.VCPUs - h.Capacity.ReserveVCPUs - active[h.ID]*max(1, h.Capacity.VMVCPUs)
		mem := h.Capacity.MemAvailableMB - max(minFreeMemMB, h.Capacity.ReserveMemoryMB)
		disk := h.Capacity.DiskFreeMB - max(minFreeDiskMB, h.Capacity.ReserveDiskMB)
		budgets = append(budgets, budget{h, maxVMs - active[h.ID], cpu, mem, disk})
	}
	sort.Slice(running, func(i, j int) bool { return running[i].Resources().MemoryMB > running[j].Resources().MemoryMB })
	plan := []model.DrainMove{}
	visits := 0
	var place func(int) bool
	place = func(i int) bool {
		if i == len(running) {
			return true
		}
		visits++
		if visits > 100000 {
			return false
		}
		r := running[i].Resources()
		for j := range budgets {
			b := &budgets[j]
			if b.slots < 1 || b.cpu < r.VCPUs || b.mem < r.MemoryMB || b.disk < r.DiskMB {
				continue
			}
			b.slots--
			b.cpu -= r.VCPUs
			b.mem -= r.MemoryMB
			b.disk -= r.DiskMB
			plan = append(plan, model.DrainMove{VMID: running[i].ID, HostID: b.h.ID, Resources: r})
			if place(i + 1) {
				return true
			}
			plan = plan[:len(plan)-1]
			b.slots++
			b.cpu += r.VCPUs
			b.mem += r.MemoryMB
			b.disk += r.DiskMB
		}
		return false
	}
	if !place(0) {
		return nil, &CapacityError{"the whole host cannot be evacuated with CPU, memory and disk reserves intact"}
	}
	return plan, nil
}

// DrainHost plans a drain and, if every VM fits, reserves the destinations
// and starts draining the host. Placement is locked meanwhile so the plan
// stays true.
func (e *Engine) DrainHost(ctx context.Context, id string) (*model.Host, []model.DrainMove, error) {
	lock, err := e.LockPlacement(ctx)
	if err != nil {
		return nil, nil, err
	}
	defer lock.Release()
	h, err := e.store.GetHost(lock.ctx, id)
	if store.IsNotFound(err) {
		return nil, nil, httpx.NotFound("host not found")
	}
	if err != nil {
		return nil, nil, err
	}
	if h.State == model.HostDraining {
		return h, nil, nil
	}
	plan, err := e.PlanDrain(lock.ctx, h)
	if err != nil {
		return nil, nil, httpx.Conflict(err.Error())
	}
	if err := e.store.ReserveDrain(lock.ctx, id, plan); err != nil {
		return nil, nil, err
	}
	e.Emit(lock.ctx, "host.draining", "", "", id, nil)
	return h, plan, nil
}

// drainHosts (leader) migrates VMs off draining hosts, one at a time per host.
func (e *Engine) drainHosts(ctx context.Context) {
	hosts, err := e.store.ListHosts(ctx)
	if err != nil {
		return
	}
	for _, h := range hosts {
		if h.State != model.HostDraining {
			continue
		}
		vms, err := e.store.ListVMs(ctx, store.VMFilter{HostID: h.ID,
			States: []model.VMState{model.VMRunning, model.VMPaused, model.VMPending, model.VMMigrating, model.VMRecovering}})
		if err != nil {
			continue
		}
		if len(vms) == 0 {
			logErr("marking "+h.Name+" drained", e.store.SetHostState(ctx, h.ID, model.HostDrained))
			e.Emit(ctx, "host.drained", "", "", h.ID, nil)
			log.Printf("host %s drained", h.Name)
			continue
		}
		v := vms[0]
		if v.State != model.VMRunning && v.State != model.VMPaused {
			continue
		}
		target := ""
		if v.State == model.VMRunning {
			if target, err = e.store.ReservationTarget(ctx, v.ID, h.ID); err != nil {
				log.Printf("drain %s: missing reservation for %s; re-plan before moving", h.Name, v.ID)
				continue
			}
		}
		if _, err := e.MigrateVM(ctx, v, target, "drain"); err != nil {
			log.Printf("drain %s: moving %s: %v", h.Name, short(v.ID), err)
		} else {
			logErr("releasing drain reservation", e.store.DeleteReservation(ctx, v.ID))
		}
	}
}
