package server

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"

	"github.com/jverre/flint/cluster/internal/db"
)

type Resources struct {
	VCPUs    int   `json:"vcpus"`
	MemoryMB int64 `json:"memory_mb"`
	DiskMB   int64 `json:"disk_mb"`
}

func vmResources(v *VM) Resources {
	var out Resources
	b, _ := json.Marshal(v.Info["resources"])
	json.Unmarshal(b, &out)
	return out
}

type drainMove struct {
	VMID      string    `json:"vm_id"`
	HostID    string    `json:"host_id"`
	Resources Resources `json:"resources"`
}

// planDrain proves that every running VM has a simultaneous compatible home.
// It is read-only: no VM is paused and no host is cordoned on a failed plan.
func (s *Server) planDrain(ctx context.Context, src *Host) ([]drainMove, error) {
	vms, err := s.listVMs(ctx, `host_id = ?`, src.ID)
	if err != nil {
		return nil, err
	}
	busy, err := s.busyVMs(ctx)
	if err != nil {
		return nil, err
	}
	running := []*VM{}
	for _, v := range vms {
		if busy[v.ID] {
			return nil, fmt.Errorf("VM %s has an operation in progress", v.ID)
		}
		switch v.State {
		case VMRunning:
			r := vmResources(v)
			if r.VCPUs <= 0 || r.MemoryMB <= 0 || r.DiskMB <= 0 {
				return nil, fmt.Errorf("VM %s has unknown resource requirements; wait for a fresh host report", v.ID)
			}
			running = append(running, v)
		case VMPaused:
			sn, err := s.getSnapshot(ctx, v.PauseID)
			if err != nil || !sn.Uploaded {
				return nil, fmt.Errorf("paused VM %s has no uploaded snapshot", v.ID)
			}
		case VMPending, VMMigrating, VMRecovering:
			return nil, fmt.Errorf("VM %s is %s", v.ID, v.State)
		}
	}
	hosts, err := s.listHosts(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.subtractDrainReservations(ctx, hosts, ""); err != nil {
		return nil, err
	}
	active, err := s.activeCounts(ctx)
	if err != nil {
		return nil, err
	}
	type budget struct {
		h          *Host
		slots, cpu int
		mem, disk  int64
	}
	budgets := []budget{}
	for _, h := range hosts {
		req := placeReq{HostClass: src.HostClass, Backend: src.Backend, Exclude: map[string]bool{src.ID: true}}
		if s.eligibility(h, active[h.ID], req, nil) != "" {
			continue
		}
		if h.Capacity.VCPUs <= 0 || h.Capacity.MemAvailableMB <= 0 || h.Capacity.DiskFreeMB <= 0 || h.maxVMs() <= 0 {
			continue // Unknown is not spare capacity.
		}
		cpu := h.Capacity.VCPUs - h.Capacity.ReserveVCPUs - active[h.ID]*max(1, h.Capacity.VMVCPUs)
		mem := h.Capacity.MemAvailableMB - max(minFreeMemMB, h.Capacity.ReserveMemoryMB)
		disk := h.Capacity.DiskFreeMB - max(minFreeDiskMB, h.Capacity.ReserveDiskMB)
		budgets = append(budgets, budget{h, h.maxVMs() - active[h.ID], cpu, mem, disk})
	}
	sort.Slice(running, func(i, j int) bool { return vmResources(running[i]).MemoryMB > vmResources(running[j]).MemoryMB })
	plan := []drainMove{}
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
		r := vmResources(running[i])
		for j := range budgets {
			b := &budgets[j]
			if b.slots < 1 || b.cpu < r.VCPUs || b.mem < r.MemoryMB || b.disk < r.DiskMB {
				continue
			}
			b.slots--
			b.cpu -= r.VCPUs
			b.mem -= r.MemoryMB
			b.disk -= r.DiskMB
			plan = append(plan, drainMove{running[i].ID, b.h.ID, r})
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
		return nil, fmt.Errorf("capacity_exhausted: the whole host cannot be evacuated with CPU, memory and disk reserves intact")
	}
	return plan, nil
}

func (s *Server) reserveDrain(ctx context.Context, id string, plan []drainMove) error {
	return s.db.Tx(ctx, func(tx *db.Tx) error {
		if _, err := tx.Exec(ctx, `DELETE FROM drain_reservations WHERE source_host = ?`, id); err != nil {
			return err
		}
		for _, m := range plan {
			if _, err := tx.Exec(ctx, `INSERT INTO drain_reservations (vm_id,source_host,target_host,vcpus,memory_mb,disk_mb) VALUES (?,?,?,?,?,?)`,
				m.VMID, id, m.HostID, m.Resources.VCPUs, m.Resources.MemoryMB, m.Resources.DiskMB); err != nil {
				return err
			}
		}
		_, err := tx.Exec(ctx, `UPDATE hosts SET cordoned = 1, state = ?, state_since = ? WHERE id = ?`, HostDraining, nowMs(), id)
		return err
	})
}
