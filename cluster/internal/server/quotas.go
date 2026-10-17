package server

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
)

// quotaError is what the API answers when a tenant is at its limit.
type quotaError struct {
	tenant          string
	max, used, want int
}

func (e *quotaError) Error() string {
	return fmt.Sprintf("tenant %s is at its quota: %d of %d VMs, %d more requested", e.tenant, e.used, e.max, e.want)
}

// checkQuota refuses want more VMs for a tenant over its max_vms (0 = no limit).
// Every VM in the catalog counts, paused ones included: they hold a snapshot
// and can be resumed at any time.
func (s *Server) checkQuota(ctx context.Context, tenant string, want int) error {
	var max int
	if err := s.db.QueryRow(ctx, `SELECT max_vms FROM tenant_quotas WHERE tenant = ?`, tenant).Scan(&max); err != nil || max <= 0 {
		return nil
	}
	var used int
	if err := s.db.QueryRow(ctx, `SELECT COUNT(*) FROM vms WHERE tenant = ? AND state <> ?`, tenant, VMLost).Scan(&used); err != nil {
		return err
	}
	if used+want > max {
		return &quotaError{tenant, max, used, want}
	}
	return nil
}

func (s *Server) handlePutQuota(w http.ResponseWriter, r *http.Request) {
	var body struct {
		MaxVMs *int `json:"max_vms"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body); err != nil || body.MaxVMs == nil || *body.MaxVMs < 0 {
		writeErr(w, 400, `body must be {"max_vms": <n >= 0>} (0 = no limit)`)
		return
	}
	tenant := r.PathValue("tenant")
	if _, err := s.db.Exec(r.Context(), `INSERT INTO tenant_quotas (tenant, max_vms, updated_at) VALUES (?, ?, ?)
		ON CONFLICT (tenant) DO UPDATE SET max_vms = excluded.max_vms, updated_at = excluded.updated_at`, tenant, *body.MaxVMs, nowMs()); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"tenant": tenant, "max_vms": *body.MaxVMs})
}

func (s *Server) handleGetQuota(w http.ResponseWriter, r *http.Request) {
	tenant := r.PathValue("tenant")
	if !principal(r).CanTenant(tenant) {
		writeErr(w, 403, "not allowed for this tenant")
		return
	}
	var max, used int
	s.db.QueryRow(r.Context(), `SELECT max_vms FROM tenant_quotas WHERE tenant = ?`, tenant).Scan(&max)
	s.db.QueryRow(r.Context(), `SELECT COUNT(*) FROM vms WHERE tenant = ? AND state <> ?`, tenant, VMLost).Scan(&used)
	writeJSON(w, 200, map[string]any{"tenant": tenant, "max_vms": max, "vms": used})
}
