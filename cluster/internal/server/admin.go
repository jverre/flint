package server

import (
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/jverre/flint/cluster/internal/auth"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// ── Hosts ─────────────────────────────────────────────────────────────────

func hostDict(h *model.Host, active int) map[string]any {
	b, _ := json.Marshal(h)
	var d map[string]any
	json.Unmarshal(b, &d)
	d["active_vms"] = active
	return d
}

var errHostNotFound = httpx.NotFound("host not found")

func (s *Server) hostForRequest(r *http.Request) (*model.Host, error) {
	h, err := s.store.GetHost(r.Context(), r.PathValue("id"))
	if store.IsNotFound(err) {
		return nil, errHostNotFound
	}
	return h, err
}

func (s *Server) handleListHosts(w http.ResponseWriter, r *http.Request) error {
	hosts, err := s.store.ListHosts(r.Context())
	if err != nil {
		return err
	}
	active, err := s.store.ActiveVMsByHost(r.Context())
	if err != nil {
		return err
	}
	out := []map[string]any{}
	for _, h := range hosts {
		out = append(out, hostDict(h, active[h.ID]))
	}
	return httpx.JSON(w, map[string]any{"hosts": out})
}

func (s *Server) hostDetail(r *http.Request, h *model.Host) (map[string]any, error) {
	active, err := s.store.ActiveVMsByHost(r.Context())
	if err != nil {
		return nil, err
	}
	return hostDict(h, active[h.ID]), nil
}

func (s *Server) handleGetHost(w http.ResponseWriter, r *http.Request) error {
	h, err := s.hostForRequest(r)
	if err != nil {
		return err
	}
	d, err := s.hostDetail(r, h)
	if err != nil {
		return err
	}
	vms, err := s.store.ListVMs(r.Context(), store.VMFilter{HostID: h.ID})
	if err != nil {
		return err
	}
	ids := []string{}
	for _, v := range vms {
		ids = append(ids, v.ID)
	}
	d["vms"] = ids
	return httpx.JSON(w, map[string]any{"host": d})
}

// handlePatchHost: {"max_vms": n} caps the VMs placed on a host (0 = what it reports).
func (s *Server) handlePatchHost(w http.ResponseWriter, r *http.Request) error {
	var body struct {
		MaxVMs *int `json:"max_vms"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body); err != nil || body.MaxVMs == nil || *body.MaxVMs < 0 {
		return httpx.BadRequest(`body must be {"max_vms": <n >= 0>}`)
	}
	if err := s.store.SetHostMaxVMs(r.Context(), r.PathValue("id"), *body.MaxVMs); err != nil {
		if store.IsNotFound(err) {
			return errHostNotFound
		}
		return err
	}
	h, err := s.hostForRequest(r)
	if err != nil {
		return err
	}
	d, err := s.hostDetail(r, h)
	if err != nil {
		return err
	}
	return httpx.JSON(w, map[string]any{"host": d})
}

// handleCordon stops (or resumes) placing VMs on a host. Uncordoning a
// drained host returns it to service.
func (s *Server) handleCordon(on bool) httpx.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) error {
		if err := s.store.SetCordoned(r.Context(), r.PathValue("id"), on); err != nil {
			if store.IsNotFound(err) {
				return errHostNotFound
			}
			return err
		}
		return httpx.OK(w)
	}
}

func (s *Server) handleDrainPlan(w http.ResponseWriter, r *http.Request) error {
	h, err := s.hostForRequest(r)
	if err != nil {
		return err
	}
	plan, err := s.eng.PlanDrain(r.Context(), h)
	if err != nil {
		return httpx.Conflict(err.Error())
	}
	return httpx.JSON(w, map[string]any{"moves": plan})
}

func (s *Server) handleDrain(w http.ResponseWriter, r *http.Request) error {
	h, plan, err := s.eng.DrainHost(r.Context(), r.PathValue("id"))
	if err != nil {
		return err
	}
	out := map[string]any{"ok": true, "state": model.HostDraining}
	if h.State != model.HostDraining { // not already draining: this call planned it
		out["moves"] = plan
	}
	return httpx.JSON(w, out)
}

func (s *Server) handleRemoveHost(w http.ResponseWriter, r *http.Request) error {
	id := r.PathValue("id")
	vms, err := s.store.ListVMs(r.Context(), store.VMFilter{HostID: id, States: []model.VMState{model.VMRunning, model.VMPending, model.VMMigrating}})
	if err != nil {
		return err
	}
	if len(vms) > 0 && r.URL.Query().Get("force") != "true" {
		return httpx.Conflict(fmt.Sprintf("host still runs %d VMs; drain it first or pass force=true", len(vms)))
	}
	if err := s.store.DeleteHost(r.Context(), id); err != nil {
		return err
	}
	s.eng.Emit(r.Context(), "host.removed", "", "", id, nil)
	return httpx.OK(w)
}

func (s *Server) handleCreateJoinToken(w http.ResponseWriter, r *http.Request) error {
	var body struct {
		Zone       string            `json:"zone"`
		Labels     map[string]string `json:"labels"`
		TTLSeconds int               `json:"ttl_seconds"`
	}
	if err := httpx.Decode(w, r, 1<<16, &body, "Invalid JSON in request body"); err != nil {
		return err
	}
	if body.TTLSeconds <= 0 {
		body.TTLSeconds = 3600
	}
	tok, err := s.createJoinToken(r.Context(), body.Zone, body.Labels, time.Duration(body.TTLSeconds)*time.Second)
	if err != nil {
		return err
	}
	return httpx.JSON(w, map[string]any{"token": tok, "expires_in_seconds": body.TTLSeconds})
}

// ── API tokens ────────────────────────────────────────────────────────────

func (s *Server) handleCreateToken(w http.ResponseWriter, r *http.Request) error {
	var body struct {
		Kind    auth.Kind `json:"kind"`
		Name    string    `json:"name"`
		Tenants []string  `json:"tenants"`
	}
	if err := httpx.Decode(w, r, 1<<16, &body, "Invalid JSON in request body"); err != nil {
		return err
	}
	if body.Kind == "" {
		body.Kind = auth.KindService
	}
	if body.Kind != auth.KindService && body.Kind != auth.KindAdmin {
		return httpx.BadRequest("kind must be service or admin")
	}
	for _, t := range body.Tenants {
		if !model.ValidTenant(t) {
			return httpx.BadRequest("invalid tenant " + t)
		}
	}
	id, tok, err := s.auth.CreateToken(r.Context(), body.Kind, body.Name, body.Tenants)
	if err != nil {
		return err
	}
	return httpx.JSON(w, map[string]any{"id": id, "token": tok, "kind": body.Kind, "tenants": body.Tenants})
}

func (s *Server) handleListTokens(w http.ResponseWriter, r *http.Request) error {
	toks, err := s.store.ListTokens(r.Context(), model.NowMs())
	if err != nil {
		return err
	}
	out := []map[string]any{}
	for _, t := range toks {
		d := map[string]any{"id": t.ID, "kind": t.Kind, "tenants": t.Tenants, "name": t.Name, "created_at": float64(t.CreatedAt) / 1000,
			"identity": t.Identity, "expires_at": nil}
		if t.ExpiresAt != 0 {
			d["expires_at"] = float64(t.ExpiresAt) / 1000
		}
		out = append(out, d)
	}
	return httpx.JSON(w, map[string]any{"tokens": out})
}

func (s *Server) handleDeleteToken(w http.ResponseWriter, r *http.Request) error {
	if err := s.auth.Revoke(r.Context(), r.PathValue("id")); err != nil {
		return err
	}
	return httpx.OK(w)
}

func (s *Server) handleListSnapshots(w http.ResponseWriter, r *http.Request) error {
	snaps, err := s.store.ListSnapshots(r.Context(), store.SnapshotFilter{})
	if err != nil {
		return err
	}
	if snaps == nil {
		snaps = []*model.Snapshot{}
	}
	return httpx.JSON(w, map[string]any{"snapshots": snaps})
}

// ── Quotas ────────────────────────────────────────────────────────────────

func (s *Server) handlePutQuota(w http.ResponseWriter, r *http.Request) error {
	var body struct {
		MaxVMs *int `json:"max_vms"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body); err != nil || body.MaxVMs == nil || *body.MaxVMs < 0 {
		return httpx.BadRequest(`body must be {"max_vms": <n >= 0>} (0 = no limit)`)
	}
	tenant := r.PathValue("tenant")
	if !model.ValidTenant(tenant) {
		return httpx.BadRequest("invalid tenant")
	}
	if err := s.store.PutTenantQuota(r.Context(), tenant, *body.MaxVMs); err != nil {
		return err
	}
	return httpx.JSON(w, map[string]any{"tenant": tenant, "max_vms": *body.MaxVMs})
}

func (s *Server) handleGetQuota(w http.ResponseWriter, r *http.Request) error {
	tenant := r.PathValue("tenant")
	if !auth.FromContext(r.Context()).CanTenant(tenant) {
		return httpx.Forbidden("not allowed for this tenant")
	}
	max, err := s.store.TenantQuota(r.Context(), tenant)
	if err != nil {
		return err
	}
	used, err := s.store.CountTenantVMs(r.Context(), tenant)
	if err != nil {
		return err
	}
	return httpx.JSON(w, map[string]any{"tenant": tenant, "max_vms": max, "vms": used})
}
