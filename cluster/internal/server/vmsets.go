package server

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"sort"

	"github.com/google/uuid"
)

func (spec *VMSetSpec) normalize() {
	if spec.TemplateID == "" {
		spec.TemplateID = "default"
	}
	if spec.Availability == "" {
		spec.Availability = AvailRestartable
	}
	if spec.Spread == "" {
		spec.Spread = "zone"
	}
	if spec.MaxSkew <= 0 {
		spec.MaxSkew = 1
	}
	if spec.MaxUnavail <= 0 {
		spec.MaxUnavail = 1
	}
}

func validSpec(spec VMSetSpec) string {
	switch {
	case spec.Replicas < 0 || spec.Replicas > 1000:
		return "replicas must be between 0 and 1000"
	case spec.Standby < 0 || spec.Standby > 100:
		return "standby must be between 0 and 100"
	case spec.Spread != "zone" && spec.Spread != "host":
		return "spread must be zone or host"
	case spec.Availability != AvailEphemeral && spec.Availability != AvailRestartable && spec.Availability != AvailDurable:
		return "invalid availability"
	}
	return ""
}

// active members count towards replicas; standbys are paused spares.
func isActiveState(st string) bool {
	return st == VMPending || st == VMRunning || st == VMMigrating || st == VMRecovering
}

// reconcileVMSets converges every VM set to its spec.
func (s *Server) reconcileVMSets(ctx context.Context) {
	sets, err := s.listVMSets(ctx, true)
	if err != nil {
		return
	}
	for _, vs := range sets {
		if err := s.reconcileVMSet(ctx, vs); err != nil {
			log.Printf("vmset %s: %v", vs.Name, err)
		}
	}
}

func (s *Server) reconcileVMSet(ctx context.Context, vs *VMSet) error {
	members, err := s.listVMs(ctx, `vmset_id = ?`, vs.ID)
	if err != nil {
		return err
	}
	if vs.Deleted {
		for _, m := range members {
			s.deleteVM(ctx, m)
		}
		_, err := s.db.Exec(ctx, `DELETE FROM vmsets WHERE id = ?`, vs.ID)
		return err
	}
	spec := vs.Spec
	var active, standby []*VM
	for _, m := range members {
		switch {
		case m.State == VMLost || m.State == VMError:
			// Replaced below; the failed member is removed (evicted from its
			// host when the host returns).
			s.deleteVM(ctx, m)
			s.emit(ctx, "vmset.member_replaced", vs.Tenant, m.ID, m.HostID, map[string]any{"vmset_id": vs.ID, "vm_id": m.ID})
		case m.Role == "standby":
			standby = append(standby, m)
		case isActiveState(m.State):
			active = append(active, m)
		case m.State == VMPaused:
			standby = append(standby, m) // a paused active member counts as a spare
		}
	}

	// Too few running: promote standbys first (a snapshot restore), then create.
	for len(active) < spec.Replicas {
		var promoted *VM
		for len(standby) > 0 && promoted == nil {
			sb := standby[0]
			standby = standby[1:]
			if sb.State != VMPaused {
				continue
			}
			nv, err := s.resumeVM(ctx, sb)
			if err != nil {
				log.Printf("vmset %s: promoting standby %s: %v", vs.Name, short(sb.ID), err)
				continue
			}
			s.updateVM(ctx, nv.ID, vmUpdate{Role: str("")})
			s.emit(ctx, "vmset.standby_promoted", vs.Tenant, nv.ID, nv.HostID, map[string]any{"vmset_id": vs.ID, "vm_id": nv.ID})
			promoted = nv
		}
		if promoted == nil {
			m, err := s.createVMSetMember(ctx, vs, active, "")
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
			s.deleteVM(ctx, m)
		}
		active = active[:spec.Replicas]
	}
	// Standbys: create (then pause) or trim.
	for len(standby) < spec.Standby {
		m, err := s.createVMSetMember(ctx, vs, append(active, standby...), "standby")
		if err != nil {
			return err
		}
		if err := s.pauseVM(ctx, m); err != nil {
			return err
		}
		m, _ = s.getVM(ctx, m.ID)
		standby = append(standby, m)
	}
	for len(standby) > spec.Standby {
		s.deleteVM(ctx, standby[len(standby)-1])
		standby = standby[:len(standby)-1]
	}
	return s.rollVMSet(ctx, vs, active, standby)
}

// rollVMSet replaces members on an old template, MaxUnavail at a time,
// surging a new member before removing an old one.
func (s *Server) rollVMSet(ctx context.Context, vs *VMSet, active, standby []*VM) error {
	for _, m := range active {
		if m.State != VMRunning {
			return nil // wait until the set is healthy
		}
	}
	replaced := 0
	for _, m := range active {
		if m.TemplateID == vs.Spec.TemplateID || replaced >= vs.Spec.MaxUnavail {
			continue
		}
		if _, err := s.createVMSetMember(ctx, vs, active, ""); err != nil {
			return err
		}
		s.deleteVM(ctx, m)
		s.emit(ctx, "vmset.member_updated", vs.Tenant, m.ID, m.HostID, map[string]any{"vmset_id": vs.ID, "vm_id": m.ID, "template_id": vs.Spec.TemplateID})
		replaced++
	}
	for _, m := range standby {
		if m.TemplateID != vs.Spec.TemplateID {
			s.deleteVM(ctx, m) // recreated on the next pass
		}
	}
	return nil
}

// createVMSetMember places a new member, spreading members across zones or hosts.
func (s *Server) createVMSetMember(ctx context.Context, vs *VMSet, siblings []*VM, role string) (*VM, error) {
	if err := s.checkQuota(ctx, vs.Tenant, 1); err != nil {
		return nil, err
	}
	counts := map[string]int{}
	hosts, _ := s.listHosts(ctx)
	byID := map[string]*Host{}
	for _, h := range hosts {
		byID[h.ID] = h
	}
	for _, m := range siblings {
		if h := byID[m.HostID]; h != nil {
			counts[spreadDomain(h, vs.Spec.Spread)]++
		}
	}
	internet := true
	if vs.Spec.AllowInternet != nil {
		internet = *vs.Spec.AllowInternet
	}
	v := &VM{ID: uuid.NewString(), Tenant: vs.Tenant, State: VMPending, TemplateID: vs.Spec.TemplateID,
		Availability: vs.Spec.Availability, Spec: VMSpec{AllowInternet: internet}, VMSetID: vs.ID, Role: role,
		Labels: map[string]string{"vmset": vs.Name}}
	if _, err := s.createVM(ctx, v, placeReq{Spread: vs.Spec.Spread, SpreadCounts: counts}); err != nil {
		return nil, err
	}
	s.emit(ctx, "vmset.member_created", vs.Tenant, v.ID, v.HostID, map[string]any{"vmset_id": vs.ID, "vm_id": v.ID, "role": role})
	return s.getVM(ctx, v.ID)
}

// ── API ───────────────────────────────────────────────────────────────────

func (s *Server) vmsetDict(ctx context.Context, vs *VMSet, withMembers bool) map[string]any {
	members, _ := s.listVMs(ctx, `vmset_id = ?`, vs.ID)
	hosts, _ := s.listHosts(ctx)
	zoneOf := map[string]string{}
	for _, h := range hosts {
		zoneOf[h.ID] = h.Zone
	}
	running, standby := 0, 0
	byZone := map[string]int{}
	var ms []map[string]any
	for _, m := range members {
		if m.Role == "standby" {
			standby++
		} else if m.State == VMRunning {
			running++
			byZone[zoneOf[m.HostID]]++
		}
		if withMembers {
			ms = append(ms, map[string]any{"vm_id": m.ID, "state": apiState(m.State), "role": m.Role, "host_id": m.HostID,
				"zone": zoneOf[m.HostID], "template_id": m.TemplateID})
		}
	}
	d := map[string]any{"id": vs.ID, "name": vs.Name, "tenant": vs.Tenant, "spec": vs.Spec, "generation": vs.Generation,
		"status": map[string]any{"running": running, "standby": standby, "by_zone": byZone, "ready": running >= vs.Spec.Replicas}}
	if withMembers {
		if ms == nil {
			ms = []map[string]any{}
		}
		d["members"] = ms
	}
	return d
}

func (s *Server) handleCreateVMSet(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Name   string    `json:"name"`
		Tenant string    `json:"tenant"`
		Spec   VMSetSpec `json:"spec"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body); err != nil || body.Name == "" || body.Tenant == "" {
		writeErr(w, 400, "name, tenant and spec are required")
		return
	}
	if !principal(r).CanTenant(body.Tenant) {
		writeErr(w, 403, "not allowed for this tenant")
		return
	}
	body.Spec.normalize()
	if msg := validSpec(body.Spec); msg != "" {
		writeErr(w, 400, msg)
		return
	}
	if !s.templateUsable(w, r, body.Spec.TemplateID, body.Tenant) {
		return
	}
	now := nowMs()
	vs := &VMSet{ID: "vs-" + uuid.NewString()[:13], Tenant: body.Tenant, Name: body.Name, Spec: body.Spec, Generation: 1, CreatedAt: now, UpdatedAt: now}
	if _, err := s.db.Exec(r.Context(), `INSERT INTO vmsets (id, tenant, name, spec, generation, deleted, created_at, updated_at) VALUES (?, ?, ?, ?, 1, 0, ?, ?)`,
		vs.ID, vs.Tenant, vs.Name, mustJSON(vs.Spec), now, now); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"vmset": s.vmsetDict(r.Context(), vs, true)})
}

// templateUsable answers 404 unless the set's tenant may boot the template.
func (s *Server) templateUsable(w http.ResponseWriter, r *http.Request, id, tenant string) bool {
	if id == "default" {
		return true
	}
	if _, err := s.templateFor(r.Context(), id, tenant); err != nil {
		code, detail := statusFor(err)
		writeErr(w, code, detail)
		return false
	}
	return true
}

func (s *Server) vmsetForRequest(w http.ResponseWriter, r *http.Request) (*VMSet, bool) {
	vs, err := s.getVMSet(r.Context(), r.PathValue("id"))
	if err != nil || vs.Deleted || !principal(r).CanTenant(vs.Tenant) {
		writeErr(w, 404, "VM set not found")
		return nil, false
	}
	return vs, true
}

func (s *Server) handleListVMSets(w http.ResponseWriter, r *http.Request) {
	sets, err := s.listVMSets(r.Context(), false)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	out := []map[string]any{}
	for _, vs := range sets {
		if principal(r).CanTenant(vs.Tenant) {
			out = append(out, s.vmsetDict(r.Context(), vs, false))
		}
	}
	writeJSON(w, 200, map[string]any{"vmsets": out})
}

func (s *Server) handleGetVMSet(w http.ResponseWriter, r *http.Request) {
	vs, ok := s.vmsetForRequest(w, r)
	if !ok {
		return
	}
	writeJSON(w, 200, map[string]any{"vmset": s.vmsetDict(r.Context(), vs, true)})
}

func (s *Server) handleUpdateVMSet(w http.ResponseWriter, r *http.Request) {
	vs, ok := s.vmsetForRequest(w, r)
	if !ok {
		return
	}
	var body struct {
		Spec VMSetSpec `json:"spec"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body); err != nil {
		writeErr(w, 400, "invalid spec")
		return
	}
	body.Spec.normalize()
	if msg := validSpec(body.Spec); msg != "" {
		writeErr(w, 400, msg)
		return
	}
	if !s.templateUsable(w, r, body.Spec.TemplateID, vs.Tenant) {
		return
	}
	if _, err := s.db.Exec(r.Context(), `UPDATE vmsets SET spec = ?, generation = generation + 1, updated_at = ? WHERE id = ?`,
		mustJSON(body.Spec), nowMs(), vs.ID); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	vs, _ = s.getVMSet(r.Context(), vs.ID)
	writeJSON(w, 200, map[string]any{"vmset": s.vmsetDict(r.Context(), vs, true)})
}

func (s *Server) handleDeleteVMSet(w http.ResponseWriter, r *http.Request) {
	vs, ok := s.vmsetForRequest(w, r)
	if !ok {
		return
	}
	s.db.Exec(r.Context(), `UPDATE vmsets SET deleted = 1, updated_at = ? WHERE id = ?`, nowMs(), vs.ID)
	writeJSON(w, 200, map[string]any{"ok": true})
}
