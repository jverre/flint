package server

import (
	"context"
	"fmt"
	"sort"
	"strings"
)

// placeReq describes what a new (or moved) VM needs from a host.
type placeReq struct {
	ReservationVM string
	Resources     Resources
	TemplateID    string
	// SnapshotID: the VM restores from this snapshot, so the host must be of
	// the snapshot's host class; hosts that cache it score higher.
	SnapshotID string
	HostClass  string
	Backend    string
	PreferHost string
	Exclude    map[string]bool
	// Spread: count of the VM's siblings per domain ("zone" or "host").
	Spread       string
	SpreadCounts map[string]int
}

// Placement limits, per host.
const (
	minFreeMemMB  = 256
	minFreeDiskMB = 1024
)

// hostLoad is the scheduler's view of one host.
type hostLoad struct {
	host   *Host
	active int
}

// eligibility explains why a host can't take a VM ("" if it can).
func (s *Server) eligibility(h *Host, active int, req placeReq, tmplSnaps map[string]bool) string {
	switch {
	case req.Exclude[h.ID]:
		return "excluded"
	case h.State != HostReady:
		return "host " + h.State
	case h.HandoverToken != "":
		return "agent handover"
	case h.Cordoned:
		return "cordoned"
	case nowMs()-h.LastHeartbeat > s.cfg.LeaseTTL.Milliseconds():
		return "heartbeat stale"
	case req.Backend != "" && h.Backend != req.Backend:
		return "backend " + h.Backend
	case req.HostClass != "" && h.HostClass != req.HostClass:
		return "different host class"
	case h.maxVMs() > 0 && active >= h.maxVMs():
		return fmt.Sprintf("full (%d/%d VMs)", active, h.maxVMs())
	case h.Capacity.VCPUs > 0 && active*max(1, h.Capacity.VMVCPUs)+max(1, req.Resources.VCPUs)+h.Capacity.ReserveVCPUs > h.Capacity.VCPUs:
		return "low CPU"
	case h.Capacity.MemAvailableMB > 0 && h.Capacity.MemAvailableMB < max(minFreeMemMB, h.Capacity.ReserveMemoryMB)+req.Resources.MemoryMB:
		return "low memory"
	case h.Capacity.DiskFreeMB > 0 && h.Capacity.DiskFreeMB < max(minFreeDiskMB, h.Capacity.ReserveDiskMB)+req.Resources.DiskMB:
		return "low disk"
	}
	if req.TemplateID != "" && req.TemplateID != "default" && req.SnapshotID == "" {
		if !hostHasTemplate(h, req.TemplateID) && !tmplSnaps[h.HostClass] {
			return "template not available for this host class"
		}
	}
	return ""
}

func hostHasTemplate(h *Host, id string) bool {
	for _, t := range h.Templates {
		if t.ID == id && t.Status == "ready" {
			return true
		}
	}
	return false
}

func contains(xs []string, x string) bool {
	for _, y := range xs {
		if y == x {
			return true
		}
	}
	return false
}

func spreadDomain(h *Host, spread string) string {
	if spread == "zone" {
		return h.Zone
	}
	return h.ID
}

// candidates returns eligible hosts, best first, or an error explaining
// why none can take the VM.
func (s *Server) candidates(ctx context.Context, req placeReq) ([]*Host, error) {
	hosts, err := s.listHosts(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.subtractDrainReservations(ctx, hosts, req.ReservationVM); err != nil {
		return nil, err
	}
	active, err := s.activeCounts(ctx)
	if err != nil {
		return nil, err
	}
	tmplSnaps := map[string]bool{}
	if req.TemplateID != "" && req.TemplateID != "default" {
		snaps, err := s.listSnapshots(ctx, `kind = 'template' AND template_id = ? AND uploaded = 1`, req.TemplateID)
		if err != nil {
			return nil, err
		}
		for _, sn := range snaps {
			tmplSnaps[sn.HostClass] = true
		}
	}
	type scored struct {
		h     *Host
		score float64
	}
	var ok []scored
	var why []string
	for _, h := range hosts {
		if reason := s.eligibility(h, active[h.ID], req, tmplSnaps); reason != "" {
			why = append(why, h.Name+": "+reason)
			continue
		}
		score := 0.0
		if req.SnapshotID != "" && contains(h.Cached, req.SnapshotID) {
			score += 100
		}
		if req.PreferHost == h.ID {
			score += 60
		}
		if req.TemplateID != "" && (req.TemplateID == "default" || hostHasTemplate(h, req.TemplateID)) {
			score += 40
		}
		if req.Spread != "" {
			score -= 50 * float64(req.SpreadCounts[spreadDomain(h, req.Spread)])
		}
		if m := h.maxVMs(); m > 0 {
			score -= 30 * float64(active[h.ID]) / float64(m)
		}
		ok = append(ok, scored{h, score})
	}
	if len(ok) == 0 {
		if len(hosts) == 0 {
			return nil, fmt.Errorf("capacity_exhausted: no hosts have joined the cluster")
		}
		return nil, fmt.Errorf("capacity_exhausted: no host can take this VM (%s)", strings.Join(why, "; "))
	}
	sort.SliceStable(ok, func(i, j int) bool {
		if ok[i].score != ok[j].score {
			return ok[i].score > ok[j].score
		}
		return ok[i].h.ID < ok[j].h.ID
	})
	out := make([]*Host, len(ok))
	for i, x := range ok {
		out[i] = x.h
	}
	return out, nil
}

// activeCounts is VMs per host that use CPU and memory (paused ones don't).
func (s *Server) activeCounts(ctx context.Context) (map[string]int, error) {
	rows, err := s.db.Query(ctx, `SELECT host_id, COUNT(*) FROM vms WHERE host_id IS NOT NULL AND state IN (?, ?, ?, ?) GROUP BY host_id`,
		VMPending, VMRunning, VMMigrating, VMRecovering)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int{}
	for rows.Next() {
		var h string
		var n int
		if err := rows.Scan(&h, &n); err != nil {
			return nil, err
		}
		out[h] = n
	}
	return out, rows.Err()
}

// peersFor lists snapshot servers of hosts that cache a snapshot (plus its
// source host), so a restoring host fetches from them before the object store.
func (s *Server) peersFor(ctx context.Context, snapshotID, exclude string) []string {
	hosts, err := s.listHosts(ctx)
	if err != nil {
		return nil
	}
	src := ""
	if sn, err := s.getSnapshot(ctx, snapshotID); err == nil {
		src = sn.SourceHost
	}
	var out []string
	for _, h := range hosts {
		if h.ID == exclude || h.SnapdURL == "" || h.State == HostDown {
			continue
		}
		if h.ID == src || contains(h.Cached, snapshotID) {
			out = append(out, h.SnapdURL)
		}
	}
	return out
}

// Reservations are durable and visible to every control-plane replica. A move
// may consume its own reservation, while unrelated creates cannot consume it.
func (s *Server) subtractDrainReservations(ctx context.Context, hosts []*Host, ownVM string) error {
	rows, err := s.db.Query(ctx, `SELECT target_host,COUNT(*),SUM(vcpus),SUM(memory_mb),SUM(disk_mb) FROM drain_reservations WHERE vm_id <> ? GROUP BY target_host`, ownVM)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var id string
		var slots, cpu int
		var mem, disk int64
		if err := rows.Scan(&id, &slots, &cpu, &mem, &disk); err != nil {
			return err
		}
		for _, h := range hosts {
			if h.ID == id {
				// Collapse zero/negative into explicit exhaustion, never unknown.
				h.Capacity.MemAvailableMB = max(1, h.Capacity.MemAvailableMB-mem)
				h.Capacity.DiskFreeMB = max(1, h.Capacity.DiskFreeMB-disk)
				h.Capacity.ReserveVCPUs += cpu
				limit := h.maxVMs()
				if limit > 0 {
					h.MaxVMs = max(1, limit-slots)
					if slots >= limit {
						h.Cordoned = true
					}
				}
			}
		}
	}
	return rows.Err()
}
