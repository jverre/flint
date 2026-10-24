package server

import (
	"encoding/json"
	"net/http"
	"time"
)

const maxHandover = 120 * time.Second

// During an explicit, bounded upgrade snapd renews only the frozen VM set.
// It cannot reconcile VM state, adopt ownership or revive an expired lease.
func (s *Server) handleHandover(w http.ResponseWriter, r *http.Request, h *Host) {
	var req struct {
		Token string   `json:"token"`
		VMs   []string `json:"vms"`
	}
	if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req) != nil || len(req.Token) < 32 || len(req.Token) > 128 {
		writeErr(w, 400, "invalid handover")
		return
	}
	if h.HandoverToken == "" {
		guard, err := s.lockVM(r.Context(), "_placement", vmLockWait)
		if err != nil {
			writeErr(w, 409, err.Error())
			return
		}
		defer guard.Release()
		current, err := s.getHost(r.Context(), h.ID)
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		h = current
	}
	now := nowMs()
	if h.LastHeartbeat <= now-s.cfg.LeaseTTL.Milliseconds() || (h.HandoverToken != "" && (h.HandoverToken != req.Token || h.HandoverUntil <= now)) {
		writeErr(w, 409, "handover or host lease expired")
		return
	}
	vms, err := s.listVMs(r.Context(), `host_id = ? AND state IN (?, ?, ?, ?)`, h.ID, VMRunning, VMPending, VMMigrating, VMRecovering)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	busy, err := s.busyVMs(r.Context())
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	frozen := map[string]bool{}
	for _, id := range req.VMs {
		frozen[id] = true
	}
	for _, v := range vms {
		if busy[v.ID] || v.State != VMRunning {
			writeErr(w, 409, "VM operation in progress")
			return
		}
		if v.Availability == AvailDurable || v.Availability == AvailRestartable {
			if !frozen[v.ID] {
				writeErr(w, 409, "frozen VM set incomplete")
				return
			}
			delete(frozen, v.ID)
		}
	}
	if len(frozen) != 0 {
		writeErr(w, 409, "VM ownership changed")
		return
	}
	until := h.HandoverUntil
	if h.HandoverToken == "" {
		until = now + maxHandover.Milliseconds()
	}
	result, err := s.db.Exec(r.Context(), `UPDATE hosts SET handover_token = ?, handover_until = ?, last_heartbeat = ? WHERE id = ? AND handover_token = ? AND last_heartbeat > ?`,
		req.Token, until, now, h.ID, h.HandoverToken, now-s.cfg.LeaseTTL.Milliseconds())
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	n, _ := result.RowsAffected()
	if n != 1 {
		writeErr(w, 409, "handover raced with another owner")
		return
	}
	ttl := s.cfg.LeaseTTL.Milliseconds()
	if until-now < ttl {
		ttl = until - now
	}
	writeJSON(w, 200, map[string]any{"lease_ttl_ms": ttl, "handover_until_ms": until})
}
