package server

import (
	"context"
	"net/http"

	"github.com/google/uuid"
	"github.com/jverre/flint/cluster/internal/auth"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// VM sets keep N replicas of a template running; the engine's reconciler
// converges them (engine/vmsets.go). These are their API.

// checkVMSetSpec normalises and checks a spec for tenant, the same way
// POST /vms checks a VM: valid fields, and a template the tenant may boot.
func (s *Server) checkVMSetSpec(ctx context.Context, spec *model.VMSetSpec, tenant string) error {
	spec.WithDefaults()
	if err := spec.Validate(); err != nil {
		return httpx.BadRequest(err.Error())
	}
	return s.eng.CheckBootable(ctx, spec.TemplateID, tenant)
}

func (s *Server) vmsetDict(ctx context.Context, vs *model.VMSet, withMembers bool) (map[string]any, error) {
	members, err := s.store.ListVMs(ctx, store.VMFilter{VMSetID: vs.ID})
	if err != nil {
		return nil, err
	}
	hosts, err := s.store.ListHosts(ctx)
	if err != nil {
		return nil, err
	}
	zoneOf := map[string]string{}
	for _, h := range hosts {
		zoneOf[h.ID] = h.Zone
	}
	running, standby := 0, 0
	byZone := map[string]int{}
	ms := []map[string]any{}
	for _, m := range members {
		if m.Role == model.RoleStandby {
			standby++
		} else if m.State == model.VMRunning {
			running++
			byZone[zoneOf[m.HostID]]++
		}
		ms = append(ms, map[string]any{"vm_id": m.ID, "state": m.State.APIName(), "role": m.Role, "host_id": m.HostID,
			"zone": zoneOf[m.HostID], "template_id": m.TemplateID})
	}
	d := map[string]any{"id": vs.ID, "name": vs.Name, "tenant": vs.Tenant, "spec": vs.Spec, "generation": vs.Generation,
		"status": map[string]any{"running": running, "standby": standby, "by_zone": byZone, "ready": running >= vs.Spec.Replicas}}
	if withMembers {
		d["members"] = ms
	}
	return d, nil
}

func (s *Server) writeVMSet(w http.ResponseWriter, r *http.Request, vs *model.VMSet) error {
	d, err := s.vmsetDict(r.Context(), vs, true)
	if err != nil {
		return err
	}
	return httpx.JSON(w, map[string]any{"vmset": d})
}

func (s *Server) handleCreateVMSet(w http.ResponseWriter, r *http.Request) error {
	var body struct {
		Name   string          `json:"name"`
		Tenant string          `json:"tenant"`
		Spec   model.VMSetSpec `json:"spec"`
	}
	if err := httpx.Decode(w, r, 1<<16, &body, "invalid VM set"); err != nil {
		return err
	}
	if body.Name == "" {
		return httpx.BadRequest("name, tenant and spec are required")
	}
	if err := checkTenant(auth.FromContext(r.Context()), body.Tenant); err != nil {
		return err
	}
	if err := s.checkVMSetSpec(r.Context(), &body.Spec, body.Tenant); err != nil {
		return err
	}
	vs := &model.VMSet{ID: "vs-" + uuid.NewString()[:13], Tenant: body.Tenant, Name: body.Name, Spec: body.Spec}
	if err := s.store.InsertVMSet(r.Context(), vs); err != nil {
		return err
	}
	return s.writeVMSet(w, r, vs)
}

var errVMSetNotFound = httpx.NotFound("VM set not found")

func (s *Server) vmsetForRequest(r *http.Request) (*model.VMSet, error) {
	vs, err := s.store.GetVMSet(r.Context(), r.PathValue("id"))
	if store.IsNotFound(err) {
		return nil, errVMSetNotFound
	}
	if err != nil {
		return nil, err
	}
	if vs.Deleted || !auth.FromContext(r.Context()).CanTenant(vs.Tenant) {
		return nil, errVMSetNotFound
	}
	return vs, nil
}

func (s *Server) handleListVMSets(w http.ResponseWriter, r *http.Request) error {
	sets, err := s.store.ListVMSets(r.Context(), false)
	if err != nil {
		return err
	}
	p := auth.FromContext(r.Context())
	out := []map[string]any{}
	for _, vs := range sets {
		if !p.CanTenant(vs.Tenant) {
			continue
		}
		d, err := s.vmsetDict(r.Context(), vs, false)
		if err != nil {
			return err
		}
		out = append(out, d)
	}
	return httpx.JSON(w, map[string]any{"vmsets": out})
}

func (s *Server) handleGetVMSet(w http.ResponseWriter, r *http.Request) error {
	vs, err := s.vmsetForRequest(r)
	if err != nil {
		return err
	}
	return s.writeVMSet(w, r, vs)
}

func (s *Server) handleUpdateVMSet(w http.ResponseWriter, r *http.Request) error {
	vs, err := s.vmsetForRequest(r)
	if err != nil {
		return err
	}
	var body struct {
		Spec model.VMSetSpec `json:"spec"`
	}
	if err := httpx.Decode(w, r, 1<<16, &body, "invalid spec"); err != nil {
		return err
	}
	if err := s.checkVMSetSpec(r.Context(), &body.Spec, vs.Tenant); err != nil {
		return err
	}
	if err := s.store.UpdateVMSetSpec(r.Context(), vs.ID, body.Spec); err != nil {
		return err
	}
	if vs, err = s.store.GetVMSet(r.Context(), vs.ID); err != nil {
		return err
	}
	return s.writeVMSet(w, r, vs)
}

func (s *Server) handleDeleteVMSet(w http.ResponseWriter, r *http.Request) error {
	vs, err := s.vmsetForRequest(r)
	if err != nil {
		return err
	}
	if err := s.store.MarkVMSetDeleted(r.Context(), vs.ID); err != nil {
		return err
	}
	return httpx.OK(w)
}
