package server

import (
	"cmp"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/jverre/flint/cluster/internal/auth"
	"github.com/jverre/flint/cluster/internal/engine"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// checkTenant checks a request names a tenant the caller may act for.
func checkTenant(p *auth.Principal, tenant string) error {
	switch {
	case tenant == "":
		return httpx.BadRequest("tenant is required")
	case !model.ValidTenant(tenant):
		return httpx.BadRequest("invalid tenant")
	case !p.CanTenant(tenant):
		return httpx.Forbidden("this token can't act for tenant " + tenant)
	}
	return nil
}

// parseBool reads a boolean query parameter the way the daemon (FastAPI) does.
func parseBool(v string) (bool, error) {
	switch strings.ToLower(v) {
	case "yes", "on":
		return true, nil
	case "no", "off":
		return false, nil
	}
	return strconv.ParseBool(v)
}

// vmParamsFromQuery fills what the body left out from the query string: the
// daemon's API takes these as query parameters too.
func vmParamsFromQuery(p *model.VMParams, q url.Values) error {
	p.TemplateID = cmp.Or(p.TemplateID, q.Get("template_id"))
	p.Availability = cmp.Or(p.Availability, model.Availability(q.Get("availability")))
	if v := q.Get("allow_internet_access"); v != "" && p.AllowInternet == nil {
		b, err := parseBool(v)
		if err != nil {
			return httpx.BadRequest("allow_internet_access must be true or false")
		}
		p.AllowInternet = &b
	}
	return nil
}

func (s *Server) handleCreateVM(w http.ResponseWriter, r *http.Request) error {
	ctx := r.Context()
	var body struct {
		Tenant string            `json:"tenant"`
		Labels map[string]string `json:"labels"`
		model.VMParams
	}
	if err := httpx.Decode(w, r, 1<<20, &body, "Invalid JSON in request body"); err != nil {
		return err
	}
	tenant := cmp.Or(body.Tenant, r.URL.Query().Get("tenant"))
	if err := checkTenant(auth.FromContext(ctx), tenant); err != nil {
		return err
	}
	if err := vmParamsFromQuery(&body.VMParams, r.URL.Query()); err != nil {
		return err
	}
	body.WithDefaults(model.AvailEphemeral)
	if err := body.Validate(); err != nil {
		return httpx.BadRequest(err.Error())
	}
	if err := s.eng.CheckBootable(ctx, body.TemplateID, tenant); err != nil {
		return err
	}
	if err := s.eng.CheckQuota(ctx, tenant, 1); err != nil {
		return err
	}
	v := &model.VM{ID: uuid.NewString(), Tenant: tenant, State: model.VMPending, TemplateID: body.TemplateID,
		Availability: body.Availability, Spec: body.Spec(), Labels: body.Labels}
	info, err := s.eng.CreateVM(ctx, v, engine.PlaceRequest{})
	if err != nil {
		return httpx.Prefix("VM creation failed: ", err)
	}
	return httpx.JSON(w, map[string]any{"vm": vmDict(v, info)})
}

var errVMNotFound = httpx.NotFound("VM not found")

// vmForRequest loads the path's VM and checks the caller may use it.
func (s *Server) vmForRequest(r *http.Request) (*model.VM, error) {
	v, err := s.store.GetVM(r.Context(), r.PathValue("id"))
	if store.IsNotFound(err) {
		return nil, errVMNotFound
	}
	if err != nil {
		return nil, err
	}
	if !auth.FromContext(r.Context()).CanTenant(v.Tenant) {
		return nil, errVMNotFound
	}
	return v, nil
}

func (s *Server) handleListVMs(w http.ResponseWriter, r *http.Request) error {
	p := auth.FromContext(r.Context())
	vms, err := s.store.ListVMs(r.Context(), store.VMFilter{Tenant: r.URL.Query().Get("tenant")})
	if err != nil {
		return err
	}
	out := []map[string]any{}
	for _, v := range vms {
		if p.CanTenant(v.Tenant) {
			out = append(out, vmDict(v, nil))
		}
	}
	return httpx.JSON(w, map[string]any{"vms": out})
}

func (s *Server) handleGetVM(w http.ResponseWriter, r *http.Request) error {
	v, err := s.vmForRequest(r)
	if err != nil {
		return err
	}
	var live map[string]any
	if v.State == model.VMRunning {
		if h, ok := s.eng.HostIfUsable(r.Context(), v.HostID); ok {
			if info, err := s.agents.Get(r.Context(), h, v.ID); err == nil {
				live = info
			}
		}
	}
	return httpx.JSON(w, map[string]any{"vm": vmDict(v, live)})
}

func (s *Server) handleDeleteVM(w http.ResponseWriter, r *http.Request) error {
	v, err := s.vmForRequest(r)
	if err != nil {
		return err
	}
	if err := s.eng.DeleteVM(r.Context(), v); err != nil {
		return err
	}
	return httpx.OK(w)
}

func (s *Server) handlePause(w http.ResponseWriter, r *http.Request) error {
	v, err := s.vmForRequest(r)
	if err != nil {
		return err
	}
	if err := s.eng.PauseVM(r.Context(), v); err != nil {
		return httpx.Prefix("Pause failed: ", err)
	}
	return httpx.OK(w)
}

func (s *Server) handleResume(w http.ResponseWriter, r *http.Request) error {
	v, err := s.vmForRequest(r)
	if err != nil {
		return err
	}
	nv, err := s.eng.ResumeVM(r.Context(), v)
	if err != nil {
		return httpx.Prefix("Resume failed: ", err)
	}
	return httpx.JSON(w, map[string]any{"vm": vmDict(nv, nil)})
}

func (s *Server) handleFork(w http.ResponseWriter, r *http.Request) error {
	v, err := s.vmForRequest(r)
	if err != nil {
		return err
	}
	var body struct {
		Count int `json:"count"`
	}
	if err := httpx.Decode(w, r, 1<<16, &body, "Invalid JSON in request body"); err != nil {
		return err
	}
	if body.Count <= 0 {
		body.Count = 1
	}
	if body.Count > 100 {
		return httpx.BadRequest("count must be at most 100")
	}
	children, err := s.eng.ForkVM(r.Context(), v, body.Count)
	if err != nil {
		return httpx.Prefix("Fork failed: ", err)
	}
	out := []map[string]any{}
	for _, c := range children {
		out = append(out, vmDict(c, nil))
	}
	return httpx.JSON(w, map[string]any{"vms": out})
}

func (s *Server) handleCheckpoint(w http.ResponseWriter, r *http.Request) error {
	v, err := s.vmForRequest(r)
	if err != nil {
		return err
	}
	if v.State != model.VMRunning {
		return httpx.Conflict("VM is not running")
	}
	nv, err := s.eng.CheckpointVM(r.Context(), v, engine.LockWait)
	if err != nil {
		return httpx.Prefix("Checkpoint failed: ", err)
	}
	return httpx.JSON(w, map[string]any{"vm": vmDict(nv, nil), "snapshot_id": nv.CheckpointID})
}

func (s *Server) handleMigrate(w http.ResponseWriter, r *http.Request) error {
	v, err := s.vmForRequest(r)
	if err != nil {
		return err
	}
	var body struct {
		HostID string `json:"host_id"`
	}
	if err := httpx.Decode(w, r, 1<<16, &body, "Invalid JSON in request body"); err != nil {
		return err
	}
	if body.HostID != "" && !auth.FromContext(r.Context()).IsAdmin() {
		return httpx.Forbidden("choosing the target host needs an admin token")
	}
	nv, err := s.eng.MigrateVM(r.Context(), v, body.HostID, "requested")
	if err != nil {
		return httpx.Prefix("Migrate failed: ", err)
	}
	return httpx.JSON(w, map[string]any{"vm": vmDict(nv, nil)})
}

// handlePatchVM changes a VM's timeout: recorded in its spec (so it survives
// moves) and applied on its host.
func (s *Server) handlePatchVM(w http.ResponseWriter, r *http.Request) error {
	v, err := s.vmForRequest(r)
	if err != nil {
		return err
	}
	var p model.VMParams
	if err := httpx.Decode(w, r, 1<<16, &p, "invalid body"); err != nil {
		return err
	}
	if !p.OnlyTimeout() {
		return httpx.BadRequest("only timeout_seconds and timeout_policy can be changed on a VM")
	}
	if err := p.Validate(); err != nil {
		return httpx.BadRequest(err.Error())
	}
	if p.TimeoutSecs == nil {
		return httpx.OK(w) // nothing to change
	}
	v.Spec.TimeoutSecs, v.Spec.TimeoutPolicy = *p.TimeoutSecs, cmp.Or(p.TimeoutPolicy, model.TimeoutKill)
	if err := s.store.UpdateVM(r.Context(), v.ID, store.VMUpdate{Spec: &v.Spec}); err != nil {
		return err
	}
	if h, ok := s.eng.HostIfUsable(r.Context(), v.HostID); ok && v.State == model.VMRunning {
		if err := s.agents.SetTimeout(r.Context(), h, v.ID, v.Spec.TimeoutSecs, v.Spec.TimeoutPolicy); err != nil {
			return err
		}
	}
	return httpx.OK(w)
}

// handlePutNetworkPolicy applies a policy on the VM's host, then keeps it in
// the spec so a recovered or moved VM gets it again.
func (s *Server) handlePutNetworkPolicy(w http.ResponseWriter, r *http.Request) error {
	v, err := s.vmForRequest(r)
	if err != nil {
		return err
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil || !json.Valid(raw) {
		return httpx.BadRequest("Invalid JSON in request body")
	}
	h, ok := s.eng.HostIfUsable(r.Context(), v.HostID)
	if !ok || v.State != model.VMRunning {
		return httpx.Conflict("VM is not running")
	}
	if err := s.agents.PutNetworkPolicy(r.Context(), h, v.ID, raw); err != nil {
		return err
	}
	v.Spec.NetworkPolicy = keepUnchanged(raw, v.Spec.NetworkPolicy)
	if err := s.store.UpdateVM(r.Context(), v.ID, store.VMUpdate{Spec: &v.Spec}); err != nil {
		return err
	}
	return httpx.OK(w)
}

// handleGateway reverse-proxies a VM request (HTTP or WebSocket) to its host.
func (s *Server) handleGateway(w http.ResponseWriter, r *http.Request) error {
	v, err := s.vmForRequest(r)
	if err != nil {
		return err
	}
	if v.State != model.VMRunning {
		return httpx.Conflict("VM is " + v.State.APIName())
	}
	h, ok := s.eng.HostIfUsable(r.Context(), v.HostID)
	if !ok {
		return httpx.Unavailable("the VM's host is unreachable")
	}
	target, err := url.Parse(h.AgentURL)
	if err != nil {
		return err
	}
	proxy := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(target)
			pr.Out.URL.Path = r.URL.Path
			pr.Out.URL.RawPath = r.URL.RawPath
			q := pr.Out.URL.Query()
			q.Del("token")
			pr.Out.URL.RawQuery = q.Encode()
			pr.Out.Header.Del("Authorization")
			pr.Out.Header.Del("Cookie") // a browser's session is the control plane's, not the host's
			// The host records who the call is for in its events (vm.exec, ...).
			actor, name := auth.ActorOf(r.Context())
			pr.Out.Header.Set("X-Flint-Actor", actor)
			pr.Out.Header.Set("X-Flint-Actor-Name", name)
			pr.Out.Host = target.Host
		},
		Transport:     s.agents.HTTP.Transport,
		FlushInterval: -1,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			httpx.WriteDetail(w, http.StatusBadGateway, "host unreachable: "+err.Error())
		},
	}
	proxy.ServeHTTP(w, r)
	return nil
}

// vmDict is the API view of a VM: the host's last view of it plus cluster fields.
func vmDict(v *model.VM, live map[string]any) map[string]any {
	d := map[string]any{}
	for k, val := range v.Info {
		d[k] = val
	}
	for k, val := range live {
		d[k] = val
	}
	d["vm_id"] = v.ID
	d["state"] = v.State.APIName()
	d["cluster_state"] = v.State
	d["template_id"] = v.TemplateID
	d["tenant"] = v.Tenant
	d["host_id"] = v.HostID
	d["availability"] = v.Availability
	d["labels"] = v.Labels
	if v.VMSetID != "" {
		d["vmset_id"] = v.VMSetID
		d["role"] = v.Role
	}
	if v.CheckpointAt > 0 {
		d["checkpoint_at"] = float64(v.CheckpointAt) / 1000
	}
	if v.Error != "" {
		d["error"] = v.Error
	}
	if _, ok := d["created_at"]; !ok {
		d["created_at"] = float64(v.CreatedAt) / 1000
	}
	if v.State != model.VMRunning {
		d["agent_healthy"] = false
	}
	return d
}
