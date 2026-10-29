package engine

import (
	"context"
	"fmt"
	"sort"
	"strings"

	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// PlaceRequest describes what a new (or moved) VM needs from a host.
type PlaceRequest struct {
	// ReservationVM may use room a drain or move reserved for it.
	ReservationVM string
	Resources     model.Resources
	TemplateID    string
	// SnapshotID: the VM restores from this snapshot, so the host must be of
	// the snapshot's host class; hosts that cache it score higher.
	SnapshotID string
	HostClass  string
	Backend    string
	PreferHost string
	Exclude    map[string]bool
	// Spread: count of the VM's siblings per domain.
	Spread       model.Spread
	SpreadCounts map[string]int
}

// Placement limits, per host.
const (
	minFreeMemMB  = 256
	minFreeDiskMB = 1024
)

// eligibility explains why a host can't take a VM ("" if it can).
func (e *Engine) eligibility(h *model.Host, active int, req PlaceRequest, tmplSnaps map[string]bool) string {
	maxVMs := h.EffectiveMaxVMs()
	switch {
	case req.Exclude[h.ID]:
		return "excluded"
	case h.State != model.HostReady:
		return "host " + string(h.State)
	case h.HandoverToken != "":
		return "agent handover"
	case h.Cordoned:
		return "cordoned"
	case model.NowMs()-h.LastHeartbeat > e.cfg.LeaseTTL.Milliseconds():
		return "heartbeat stale"
	case req.Backend != "" && h.Backend != req.Backend:
		return "backend " + h.Backend
	case req.HostClass != "" && h.HostClass != req.HostClass:
		return "different host class"
	case maxVMs > 0 && active >= maxVMs:
		return fmt.Sprintf("full (%d/%d VMs)", active, maxVMs)
	case h.Capacity.VCPUs > 0 && active*max(1, h.Capacity.VMVCPUs)+max(1, req.Resources.VCPUs)+h.Capacity.ReserveVCPUs > h.Capacity.VCPUs:
		return "low CPU"
	case h.Capacity.MemAvailableMB > 0 && h.Capacity.MemAvailableMB < max(minFreeMemMB, h.Capacity.ReserveMemoryMB)+req.Resources.MemoryMB:
		return "low memory"
	case h.Capacity.DiskFreeMB > 0 && h.Capacity.DiskFreeMB < max(minFreeDiskMB, h.Capacity.ReserveDiskMB)+req.Resources.DiskMB:
		return "low disk"
	}
	if req.TemplateID != "" && req.TemplateID != model.DefaultTemplate && req.SnapshotID == "" {
		if !h.HasTemplate(req.TemplateID) && !tmplSnaps[h.HostClass] {
			return "template not available for this host class"
		}
	}
	return ""
}

// Candidates returns eligible hosts, best first, or a *CapacityError
// explaining why none can take the VM.
func (e *Engine) Candidates(ctx context.Context, req PlaceRequest) ([]*model.Host, error) {
	hosts, err := e.store.ListHosts(ctx)
	if err != nil {
		return nil, err
	}
	if err := e.subtractReservations(ctx, hosts, req.ReservationVM); err != nil {
		return nil, err
	}
	active, err := e.store.ActiveVMsByHost(ctx)
	if err != nil {
		return nil, err
	}
	tmplSnaps := map[string]bool{}
	if req.TemplateID != "" && req.TemplateID != model.DefaultTemplate {
		snaps, err := e.store.ListSnapshots(ctx, store.SnapshotFilter{Kind: model.SnapshotTemplate, TemplateID: req.TemplateID, UploadedOnly: true})
		if err != nil {
			return nil, err
		}
		for _, sn := range snaps {
			tmplSnaps[sn.HostClass] = true
		}
	}
	type scored struct {
		h     *model.Host
		score float64
	}
	var ok []scored
	var why []string
	for _, h := range hosts {
		if reason := e.eligibility(h, active[h.ID], req, tmplSnaps); reason != "" {
			why = append(why, h.Name+": "+reason)
			continue
		}
		score := 0.0
		if req.SnapshotID != "" && h.Caches(req.SnapshotID) {
			score += 100
		}
		if req.PreferHost == h.ID {
			score += 60
		}
		if req.TemplateID != "" && (req.TemplateID == model.DefaultTemplate || h.HasTemplate(req.TemplateID)) {
			score += 40
		}
		if req.Spread != "" {
			score -= 50 * float64(req.SpreadCounts[req.Spread.Domain(h)])
		}
		if m := h.EffectiveMaxVMs(); m > 0 {
			score -= 30 * float64(active[h.ID]) / float64(m)
		}
		ok = append(ok, scored{h, score})
	}
	if len(ok) == 0 {
		if len(hosts) == 0 {
			return nil, &CapacityError{"no hosts have joined the cluster"}
		}
		return nil, &CapacityError{fmt.Sprintf("no host can take this VM (%s)", strings.Join(why, "; "))}
	}
	sort.SliceStable(ok, func(i, j int) bool {
		if ok[i].score != ok[j].score {
			return ok[i].score > ok[j].score
		}
		return ok[i].h.ID < ok[j].h.ID
	})
	out := make([]*model.Host, len(ok))
	for i, x := range ok {
		out[i] = x.h
	}
	return out, nil
}

// peersFor lists snapshot servers of hosts that cache a snapshot (plus its
// source host), so a restoring host fetches from them before the object store.
func (e *Engine) peersFor(ctx context.Context, snapshotID, exclude string) []string {
	hosts, err := e.store.ListHosts(ctx)
	if err != nil {
		return nil
	}
	src := ""
	if sn, err := e.store.GetSnapshot(ctx, snapshotID); err == nil {
		src = sn.SourceHost
	}
	var out []string
	for _, h := range hosts {
		if h.ID == exclude || h.SnapdURL == "" || h.State == model.HostDown {
			continue
		}
		if h.ID == src || h.Caches(snapshotID) {
			out = append(out, h.SnapdURL)
		}
	}
	return out
}

// subtractReservations takes room reserved for moving VMs out of the hosts'
// capacity. Reservations are durable and visible to every replica. A move
// may consume its own reservation; unrelated creates cannot.
func (e *Engine) subtractReservations(ctx context.Context, hosts []*model.Host, ownVM string) error {
	reserved, err := e.store.ReservedByHost(ctx, ownVM)
	if err != nil {
		return err
	}
	for _, r := range reserved {
		for _, h := range hosts {
			if h.ID != r.HostID {
				continue
			}
			// Collapse zero/negative into explicit exhaustion, never unknown.
			h.Capacity.MemAvailableMB = max(1, h.Capacity.MemAvailableMB-r.MemoryMB)
			h.Capacity.DiskFreeMB = max(1, h.Capacity.DiskFreeMB-r.DiskMB)
			h.Capacity.ReserveVCPUs += r.VCPUs
			if limit := h.EffectiveMaxVMs(); limit > 0 {
				h.MaxVMs = max(1, limit-r.Slots)
				if r.Slots >= limit {
					h.Cordoned = true
				}
			}
		}
	}
	return nil
}
