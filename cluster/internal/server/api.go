package server

import (
	"database/sql"
	"github.com/jverre/flint/cluster/internal/webui"

	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httputil"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
)

func (s *Server) apiHandler() http.Handler {
	mux := http.NewServeMux()
	auth := func(h http.HandlerFunc) http.HandlerFunc { return s.requireAuth(false, h) }
	admin := func(h http.HandlerFunc) http.HandlerFunc { return s.requireAuth(true, h) }

	mux.HandleFunc("GET /health", s.handleHealth)

	// Sign-in with identity providers (sso.go); public by nature.
	mux.HandleFunc("GET /auth/providers", s.handleSSOProviders)
	mux.HandleFunc("GET /auth/login/{id}", s.handleSSOLogin)
	mux.HandleFunc("GET /auth/callback/{id}", s.handleSSOCallback)
	mux.HandleFunc("POST /auth/cli/exchange", s.handleSSOCLIExchange)
	mux.HandleFunc("GET /auth/me", s.handleSSOMe)
	mux.HandleFunc("POST /auth/logout", s.handleSSOLogout)

	// VMs: the daemon's API, served for the whole cluster.
	mux.HandleFunc("POST /vms", auth(s.idempotent(s.handleCreateVM)))
	mux.HandleFunc("GET /vms", auth(s.handleListVMs))
	mux.HandleFunc("GET /vms/{id}", auth(s.handleGetVM))
	mux.HandleFunc("DELETE /vms/{id}", auth(s.idempotent(s.handleDeleteVM)))
	mux.HandleFunc("POST /vms/{id}/pause", auth(s.idempotent(s.handlePause)))
	mux.HandleFunc("POST /vms/{id}/resume", auth(s.idempotent(s.handleResume)))
	mux.HandleFunc("POST /vms/{id}/fork", auth(s.idempotent(s.handleFork)))
	mux.HandleFunc("POST /vms/{id}/checkpoint", auth(s.idempotent(s.handleCheckpoint)))
	mux.HandleFunc("POST /vms/{id}/migrate", auth(s.idempotent(s.handleMigrate)))
	mux.HandleFunc("PATCH /vms/{id}", auth(s.handlePatchVM))
	mux.HandleFunc("PUT /vms/{id}/network-policy", auth(s.handlePutNetworkPolicy))
	mux.HandleFunc("/vms/{id}/mounts", auth(notInCluster("volume mounts")))
	mux.HandleFunc("/vms/{id}/mounts/{rest...}", auth(notInCluster("volume mounts")))
	// Everything else on a VM goes to its host: exec, processes, files,
	// metrics, logs and terminal WebSockets.
	mux.HandleFunc("/vms/{id}/{rest...}", auth(s.handleGateway))

	mux.HandleFunc("GET /events", auth(s.handleEventsWS))
	mux.HandleFunc("GET /v1/events", auth(s.handleEventsList))

	mux.HandleFunc("POST /templates/build", auth(s.handleBuildTemplate))
	mux.HandleFunc("GET /templates", auth(s.handleListTemplates))
	mux.HandleFunc("GET /templates/{id}", auth(s.handleGetTemplate))
	mux.HandleFunc("DELETE /templates/{id}", auth(s.handleDeleteTemplate))

	mux.HandleFunc("/volumes", auth(notInCluster("volumes")))
	mux.HandleFunc("/volumes/{rest...}", auth(notInCluster("volumes")))
	mux.HandleFunc("/config", auth(notInCluster("per-daemon config")))

	// Cluster administration.
	mux.HandleFunc("GET /v1/hosts", admin(s.handleListHosts))
	mux.HandleFunc("GET /v1/hosts/{id}", admin(s.handleGetHost))
	mux.HandleFunc("PATCH /v1/hosts/{id}", admin(s.handlePatchHost))
	mux.HandleFunc("POST /v1/hosts/{id}/cordon", admin(s.handleCordon(true)))
	mux.HandleFunc("POST /v1/hosts/{id}/uncordon", admin(s.handleCordon(false)))
	mux.HandleFunc("POST /v1/hosts/{id}/drain", admin(s.handleDrain))
	mux.HandleFunc("DELETE /v1/hosts/{id}", admin(s.handleRemoveHost))
	mux.HandleFunc("POST /v1/join-tokens", admin(s.handleCreateJoinToken))
	mux.HandleFunc("POST /v1/tokens", admin(s.handleCreateToken))
	mux.HandleFunc("GET /v1/tokens", admin(s.handleListTokens))
	mux.HandleFunc("DELETE /v1/tokens/{id}", admin(s.handleDeleteToken))
	mux.HandleFunc("GET /v1/snapshots", admin(s.handleListSnapshots))
	mux.HandleFunc("PUT /v1/tenants/{tenant}/quota", admin(s.handlePutQuota))
	mux.HandleFunc("GET /v1/tenants/{tenant}/quota", auth(s.handleGetQuota))

	mux.HandleFunc("POST /v1/vmsets", auth(s.handleCreateVMSet))
	mux.HandleFunc("GET /v1/vmsets", auth(s.handleListVMSets))
	mux.HandleFunc("GET /v1/vmsets/{id}", auth(s.handleGetVMSet))
	mux.HandleFunc("PUT /v1/vmsets/{id}", auth(s.handleUpdateVMSet))
	mux.HandleFunc("DELETE /v1/vmsets/{id}", auth(s.handleDeleteVMSet))

	// Admin web UI (static; the API calls it makes are authenticated as usual).
	mux.Handle("GET /ui/", webui.Handler())
	mux.Handle("GET /ui", http.RedirectHandler("/ui/", http.StatusMovedPermanently))
	mux.Handle("GET /{$}", http.RedirectHandler("/ui/", http.StatusFound))
	return mux
}

func notInCluster(what string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		writeErr(w, http.StatusNotImplemented, what+" are host-local and not available through the cluster API yet")
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	hosts, err := s.listHosts(r.Context())
	if err != nil {
		writeJSON(w, 503, map[string]any{"status": "error", "detail": err.Error()})
		return
	}
	ready := 0
	backends := map[string]bool{}
	for _, h := range hosts {
		if h.State == HostReady && nowMs()-h.LastHeartbeat < s.cfg.LeaseTTL.Milliseconds() {
			ready++
			backends[h.Backend] = true
		}
	}
	kind := "cluster"
	if len(backends) == 1 {
		for b := range backends {
			kind = b
		}
	}
	writeJSON(w, 200, map[string]any{
		"status": "ok", "role": "control-plane", "replica": s.cfg.ReplicaID, "leader": s.isLeader(),
		"hosts": len(hosts), "hosts_ready": ready, "backend_kind": kind,
		// The SDK and tests read this to know sandboxes can be created.
		"golden_snapshot_ready": ready > 0,
	})
}

var tenantRe = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$`)

func (s *Server) handleCreateVM(w http.ResponseWriter, r *http.Request) {
	p := principal(r)
	q := r.URL.Query()
	var body struct {
		Tenant        string            `json:"tenant"`
		TemplateID    string            `json:"template_id"`
		NetworkPolicy json.RawMessage   `json:"network_policy"`
		Availability  string            `json:"availability"`
		Labels        map[string]string `json:"labels"`
		AllowInternet *bool             `json:"allow_internet_access"`
	}
	if b, _ := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20)); len(b) > 0 {
		if err := json.Unmarshal(b, &body); err != nil {
			writeErr(w, 400, "Invalid JSON in request body")
			return
		}
	}
	tenant := firstNonEmpty(body.Tenant, q.Get("tenant"))
	if tenant == "" {
		writeErr(w, 400, "tenant is required (query parameter or body field)")
		return
	}
	if !tenantRe.MatchString(tenant) {
		writeErr(w, 400, "invalid tenant")
		return
	}
	if !p.CanTenant(tenant) {
		writeErr(w, 403, "this token can't create VMs for tenant "+tenant)
		return
	}
	tmpl := firstNonEmpty(body.TemplateID, q.Get("template_id"), "default")
	if tmpl != "default" {
		t, err := s.getTemplate(r.Context(), tmpl)
		if err != nil {
			writeErr(w, 404, "template not found: "+tmpl)
			return
		}
		if t.Status != "ready" {
			writeErr(w, 409, "template "+tmpl+" is "+t.Status)
			return
		}
	}
	avail := firstNonEmpty(body.Availability, q.Get("availability"), AvailEphemeral)
	if avail != AvailEphemeral && avail != AvailRestartable && avail != AvailDurable {
		writeErr(w, 400, "availability must be ephemeral, restartable or durable")
		return
	}
	internet := true
	if v := q.Get("allow_internet_access"); v != "" {
		internet, _ = strconv.ParseBool(v)
	}
	if body.AllowInternet != nil {
		internet = *body.AllowInternet
	}
	if err := s.checkQuota(r.Context(), tenant, 1); err != nil {
		code, detail := statusFor(err)
		writeErr(w, code, detail)
		return
	}
	v := &VM{ID: uuid.NewString(), Tenant: tenant, State: VMPending, TemplateID: tmpl, Availability: avail,
		Spec: VMSpec{AllowInternet: internet, NetworkPolicy: body.NetworkPolicy}, Labels: body.Labels}
	if len(body.NetworkPolicy) == 0 || string(body.NetworkPolicy) == "null" {
		v.Spec.NetworkPolicy = nil
	}
	info, err := s.createVM(r.Context(), v, placeReq{})
	if err != nil {
		code, detail := statusFor(err)
		writeErr(w, code, "VM creation failed: "+detail)
		return
	}
	writeJSON(w, 200, map[string]any{"vm": s.vmDict(v, info)})
}

func firstNonEmpty(xs ...string) string {
	for _, x := range xs {
		if x != "" {
			return x
		}
	}
	return ""
}

// vmForRequest loads the path's VM and checks the caller may use it.
func (s *Server) vmForRequest(w http.ResponseWriter, r *http.Request) (*VM, bool) {
	v, err := s.getVM(r.Context(), r.PathValue("id"))
	if err != nil {
		writeErr(w, 404, "VM not found")
		return nil, false
	}
	if !principal(r).CanTenant(v.Tenant) {
		writeErr(w, 404, "VM not found")
		return nil, false
	}
	return v, true
}

func (s *Server) handleListVMs(w http.ResponseWriter, r *http.Request) {
	p := principal(r)
	tenant := r.URL.Query().Get("tenant")
	var vms []*VM
	var err error
	if tenant != "" {
		vms, err = s.listVMs(r.Context(), `tenant = ?`, tenant)
	} else {
		vms, err = s.listVMs(r.Context(), "")
	}
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	out := []map[string]any{}
	for _, v := range vms {
		if p.CanTenant(v.Tenant) {
			out = append(out, s.vmDict(v, nil))
		}
	}
	writeJSON(w, 200, map[string]any{"vms": out})
}

func (s *Server) handleGetVM(w http.ResponseWriter, r *http.Request) {
	v, ok := s.vmForRequest(w, r)
	if !ok {
		return
	}
	var live map[string]any
	if v.State == VMRunning {
		if h, ok := s.hostIfUsable(r.Context(), v.HostID); ok {
			if info, err := s.getOnHost(r.Context(), h, v.ID); err == nil {
				live = info
			}
		}
	}
	writeJSON(w, 200, map[string]any{"vm": s.vmDict(v, live)})
}

func (s *Server) handleDeleteVM(w http.ResponseWriter, r *http.Request) {
	v, ok := s.vmForRequest(w, r)
	if !ok {
		return
	}
	if err := s.deleteVM(r.Context(), v); err != nil {
		code, detail := statusFor(err)
		writeErr(w, code, detail)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) handlePause(w http.ResponseWriter, r *http.Request) {
	v, ok := s.vmForRequest(w, r)
	if !ok {
		return
	}
	if err := s.pauseVM(r.Context(), v); err != nil {
		code, detail := statusFor(err)
		writeErr(w, code, "Pause failed: "+detail)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) handleResume(w http.ResponseWriter, r *http.Request) {
	v, ok := s.vmForRequest(w, r)
	if !ok {
		return
	}
	nv, err := s.resumeVM(r.Context(), v)
	if err != nil {
		code, detail := statusFor(err)
		writeErr(w, code, "Resume failed: "+detail)
		return
	}
	writeJSON(w, 200, map[string]any{"vm": s.vmDict(nv, nil)})
}

func (s *Server) handleFork(w http.ResponseWriter, r *http.Request) {
	v, ok := s.vmForRequest(w, r)
	if !ok {
		return
	}
	var body struct {
		Count int `json:"count"`
	}
	json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body)
	if body.Count <= 0 {
		body.Count = 1
	}
	if body.Count > 100 {
		writeErr(w, 400, "count must be at most 100")
		return
	}
	children, err := s.forkVM(r.Context(), v, body.Count)
	if err != nil {
		code, detail := statusFor(err)
		writeErr(w, code, "Fork failed: "+detail)
		return
	}
	out := []map[string]any{}
	for _, c := range children {
		out = append(out, s.vmDict(c, nil))
	}
	writeJSON(w, 200, map[string]any{"vms": out})
}

func (s *Server) handleCheckpoint(w http.ResponseWriter, r *http.Request) {
	v, ok := s.vmForRequest(w, r)
	if !ok {
		return
	}
	if v.State != VMRunning {
		writeErr(w, 409, "VM is not running")
		return
	}
	if err := s.checkpointVM(r.Context(), v, vmLockWait); err != nil {
		code, detail := statusFor(err)
		writeErr(w, code, "Checkpoint failed: "+detail)
		return
	}
	nv, _ := s.getVM(r.Context(), v.ID)
	writeJSON(w, 200, map[string]any{"vm": s.vmDict(nv, nil), "snapshot_id": nv.CheckpointID})
}

func (s *Server) handleMigrate(w http.ResponseWriter, r *http.Request) {
	v, ok := s.vmForRequest(w, r)
	if !ok {
		return
	}
	var body struct {
		HostID string `json:"host_id"`
	}
	json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body)
	if body.HostID != "" && !principal(r).IsAdmin() {
		writeErr(w, 403, "choosing the target host needs an admin token")
		return
	}
	nv, err := s.migrateVM(r.Context(), v, body.HostID, "requested")
	if err != nil {
		code, detail := statusFor(err)
		writeErr(w, code, "Migrate failed: "+detail)
		return
	}
	writeJSON(w, 200, map[string]any{"vm": s.vmDict(nv, nil)})
}

// handlePatchVM records the timeout in the spec (so it survives moves) and
// applies it on the host.
func (s *Server) handlePatchVM(w http.ResponseWriter, r *http.Request) {
	v, ok := s.vmForRequest(w, r)
	if !ok {
		return
	}
	raw, _ := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<16))
	var body struct {
		TimeoutSeconds *float64 `json:"timeout_seconds"`
		TimeoutPolicy  string   `json:"timeout_policy"`
	}
	if err := json.Unmarshal(raw, &body); err != nil {
		writeErr(w, 400, "invalid body")
		return
	}
	if body.TimeoutSeconds != nil {
		v.Spec.TimeoutSecs = *body.TimeoutSeconds
		v.Spec.TimeoutPolicy = firstNonEmpty(body.TimeoutPolicy, "kill")
		s.updateVM(r.Context(), v.ID, vmUpdate{Spec: &v.Spec})
	}
	if h, ok := s.hostIfUsable(r.Context(), v.HostID); ok && v.State == VMRunning {
		var out any
		if err := s.agentDo(r.Context(), h, http.MethodPatch, "/vms/"+v.ID, json.RawMessage(raw), &out, 30*time.Second); err != nil {
			code, detail := statusFor(err)
			writeErr(w, code, detail)
			return
		}
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

// handlePutNetworkPolicy keeps the policy in the spec so a recovered or moved
// VM gets it again, then applies it on the host.
func (s *Server) handlePutNetworkPolicy(w http.ResponseWriter, r *http.Request) {
	v, ok := s.vmForRequest(w, r)
	if !ok {
		return
	}
	raw, _ := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if !json.Valid(raw) {
		writeErr(w, 400, "Invalid JSON in request body")
		return
	}
	h, ok := s.hostIfUsable(r.Context(), v.HostID)
	if !ok || v.State != VMRunning {
		writeErr(w, 409, "VM is not running")
		return
	}
	var out any
	if err := s.agentDo(r.Context(), h, http.MethodPut, "/vms/"+v.ID+"/network-policy", json.RawMessage(raw), &out, 30*time.Second); err != nil {
		code, detail := statusFor(err)
		writeErr(w, code, detail)
		return
	}
	v.Spec.NetworkPolicy = keepUnchanged(raw, v.Spec.NetworkPolicy)
	s.updateVM(r.Context(), v.ID, vmUpdate{Spec: &v.Spec})
	writeJSON(w, 200, map[string]any{"ok": true})
}

// handleGateway reverse-proxies a VM request (HTTP or WebSocket) to its host.
func (s *Server) handleGateway(w http.ResponseWriter, r *http.Request) {
	v, ok := s.vmForRequest(w, r)
	if !ok {
		return
	}
	if v.State != VMRunning {
		writeErr(w, 409, fmt.Sprintf("VM is %s", apiState(v.State)))
		return
	}
	h, ok := s.hostIfUsable(r.Context(), v.HostID)
	if !ok {
		writeErr(w, 503, "the VM's host is unreachable")
		return
	}
	target, err := url.Parse(h.AgentURL)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
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
			pr.Out.Host = target.Host
		},
		Transport:     s.agents.Transport,
		FlushInterval: -1,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			writeErr(w, 502, "host unreachable: "+err.Error())
		},
	}
	proxy.ServeHTTP(w, r)
}

// ── templates ──────────────────────────────────────────────────────────────

// slugify matches flint.core._template_build._slugify, so IDs agree with hosts.
var slugRe = regexp.MustCompile(`[^a-z0-9]+`)

func slugify(name string) string {
	s := strings.Trim(slugRe.ReplaceAllString(strings.ToLower(name), "-"), "-")
	if s == "" {
		s = "template"
	}
	return s
}

// handleBuildTemplate builds on one host. When done, the host publishes the
// result as a snapshot; other hosts of the same class restore it on demand.
func (s *Server) handleBuildTemplate(w http.ResponseWriter, r *http.Request) {
	p := principal(r)
	var body struct {
		Name       string `json:"name"`
		Dockerfile string `json:"dockerfile"`
		RootfsMB   int    `json:"rootfs_size_mb"`
		Tenant     string `json:"tenant"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20)).Decode(&body); err != nil || body.Name == "" || body.Dockerfile == "" {
		writeErr(w, 400, "name and dockerfile are required")
		return
	}
	if body.Tenant != "" && !p.CanTenant(body.Tenant) {
		writeErr(w, 403, "not allowed for this tenant")
		return
	}
	if body.RootfsMB == 0 {
		body.RootfsMB = 500
	}
	id := slugify(body.Name)
	if id == "default" {
		writeErr(w, 400, "cannot rebuild the default template")
		return
	}
	cands, err := s.candidates(r.Context(), placeReq{TemplateID: "default"})
	if err != nil {
		writeErr(w, 503, err.Error())
		return
	}
	h := cands[0]
	now := nowMs()
	if _, err := s.db.Exec(r.Context(), `INSERT INTO templates (id, name, tenant, dockerfile, rootfs_mb, status, build_host, error, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, 'building', ?, '', ?, ?) ON CONFLICT (id) DO UPDATE SET name = excluded.name, dockerfile = excluded.dockerfile,
		rootfs_mb = excluded.rootfs_mb, status = 'building', build_host = excluded.build_host, error = '', updated_at = excluded.updated_at`,
		id, body.Name, body.Tenant, body.Dockerfile, body.RootfsMB, h.ID, now, now); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	// A rebuild replaces the template's snapshots.
	s.db.Exec(r.Context(), `DELETE FROM snapshots WHERE kind = 'template' AND template_id = ?`, id)
	var out map[string]any
	if err := s.agentDo(r.Context(), h, http.MethodPost, "/templates/build",
		map[string]any{"name": body.Name, "dockerfile": body.Dockerfile, "rootfs_size_mb": body.RootfsMB}, &out, 60*time.Second); err != nil {
		s.db.Exec(r.Context(), `UPDATE templates SET status = 'error', error = ? WHERE id = ?`, err.Error(), id)
		code, detail := statusFor(err)
		writeErr(w, code, detail)
		return
	}
	writeJSON(w, 200, map[string]any{"template_id": id, "status": "building", "build_host": h.ID})
}

func (s *Server) templateDict(t *Template) map[string]any {
	return map[string]any{"template_id": t.ID, "name": t.Name, "status": t.Status, "tenant": t.Tenant,
		"build_host": t.BuildHost, "error": t.Error, "created_at": float64(t.CreatedAt) / 1000}
}

func defaultTemplate() map[string]any {
	return map[string]any{"template_id": "default", "name": "Default (Alpine)", "status": "ready"}
}

func (s *Server) handleListTemplates(w http.ResponseWriter, r *http.Request) {
	ts, err := s.listTemplates(r.Context())
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	out := []map[string]any{defaultTemplate()}
	for _, t := range ts {
		if t.Tenant == "" || principal(r).CanTenant(t.Tenant) {
			out = append(out, s.templateDict(t))
		}
	}
	writeJSON(w, 200, map[string]any{"templates": out})
}

func (s *Server) handleGetTemplate(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "default" {
		writeJSON(w, 200, map[string]any{"template": defaultTemplate()})
		return
	}
	t, err := s.getTemplate(r.Context(), id)
	if err != nil || (t.Tenant != "" && !principal(r).CanTenant(t.Tenant)) {
		writeErr(w, 404, "Template not found")
		return
	}
	d := s.templateDict(t)
	snaps, _ := s.listSnapshots(r.Context(), `kind = 'template' AND template_id = ?`, id)
	classes := []string{}
	for _, sn := range snaps {
		classes = append(classes, sn.HostClass)
	}
	d["host_classes"] = classes
	writeJSON(w, 200, map[string]any{"template": d})
}

func (s *Server) handleDeleteTemplate(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "default" {
		writeErr(w, 400, "Cannot delete default template")
		return
	}
	t, err := s.getTemplate(r.Context(), id)
	if err != nil || (t.Tenant != "" && !principal(r).CanTenant(t.Tenant)) {
		writeErr(w, 404, "Template not found")
		return
	}
	hosts, _ := s.listHosts(r.Context())
	for _, h := range hosts {
		if hostHasTemplate(h, id) || h.ID == t.BuildHost {
			if hh, ok := s.hostIfUsable(r.Context(), h.ID); ok {
				s.agentDo(r.Context(), hh, http.MethodDelete, "/templates/"+id, nil, nil, 60*time.Second)
			}
		}
	}
	s.db.Exec(r.Context(), `DELETE FROM snapshots WHERE kind = 'template' AND template_id = ?`, id)
	s.db.Exec(r.Context(), `DELETE FROM templates WHERE id = ?`, id)
	writeJSON(w, 200, map[string]any{"ok": true})
}

// ── hosts, tokens ──────────────────────────────────────────────────────────

func (s *Server) handleListHosts(w http.ResponseWriter, r *http.Request) {
	hosts, err := s.listHosts(r.Context())
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	active, _ := s.activeCounts(r.Context())
	out := []map[string]any{}
	for _, h := range hosts {
		out = append(out, hostDict(h, active[h.ID]))
	}
	writeJSON(w, 200, map[string]any{"hosts": out})
}

func hostDict(h *Host, active int) map[string]any {
	b, _ := json.Marshal(h)
	var d map[string]any
	json.Unmarshal(b, &d)
	d["active_vms"] = active
	return d
}

func (s *Server) handleGetHost(w http.ResponseWriter, r *http.Request) {
	h, err := s.getHost(r.Context(), r.PathValue("id"))
	if err != nil {
		writeErr(w, 404, "host not found")
		return
	}
	active, _ := s.activeCounts(r.Context())
	d := hostDict(h, active[h.ID])
	vms, _ := s.listVMs(r.Context(), `host_id = ?`, h.ID)
	ids := []string{}
	for _, v := range vms {
		ids = append(ids, v.ID)
	}
	d["vms"] = ids
	writeJSON(w, 200, map[string]any{"host": d})
}

// handlePatchHost: {"max_vms": n} caps the VMs placed on a host (0 = what it reports).
func (s *Server) handlePatchHost(w http.ResponseWriter, r *http.Request) {
	var body struct {
		MaxVMs *int `json:"max_vms"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body); err != nil || body.MaxVMs == nil || *body.MaxVMs < 0 {
		writeErr(w, 400, "body must be {\"max_vms\": <n >= 0>}")
		return
	}
	res, err := s.db.Exec(r.Context(), `UPDATE hosts SET max_vms = ? WHERE id = ?`, *body.MaxVMs, r.PathValue("id"))
	if n, _ := res.RowsAffected(); err != nil || n == 0 {
		writeErr(w, 404, "host not found")
		return
	}
	h, _ := s.getHost(r.Context(), r.PathValue("id"))
	active, _ := s.activeCounts(r.Context())
	writeJSON(w, 200, map[string]any{"host": hostDict(h, active[h.ID])})
}

func (s *Server) handleCordon(on bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		c := 0
		if on {
			c = 1
		}
		res, err := s.db.Exec(r.Context(), `UPDATE hosts SET cordoned = ? WHERE id = ?`, c, r.PathValue("id"))
		if n, _ := res.RowsAffected(); err != nil || n == 0 {
			writeErr(w, 404, "host not found")
			return
		}
		if !on {
			// Uncordoning a drained host returns it to service.
			s.db.Exec(r.Context(), `UPDATE hosts SET state = ?, state_since = ? WHERE id = ? AND state IN (?, ?)`,
				HostReady, nowMs(), r.PathValue("id"), HostDraining, HostDrained)
		}
		writeJSON(w, 200, map[string]any{"ok": true})
	}
}

func (s *Server) handleDrain(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if _, err := s.getHost(r.Context(), id); err != nil {
		writeErr(w, 404, "host not found")
		return
	}
	s.db.Exec(r.Context(), `UPDATE hosts SET cordoned = 1, state = ?, state_since = ? WHERE id = ?`, HostDraining, nowMs(), id)
	s.emit(r.Context(), "host.draining", "", "", id, nil)
	writeJSON(w, 200, map[string]any{"ok": true, "state": HostDraining})
}

func (s *Server) handleRemoveHost(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	vms, _ := s.listVMs(r.Context(), `host_id = ? AND state IN (?, ?, ?)`, id, VMRunning, VMPending, VMMigrating)
	if len(vms) > 0 && r.URL.Query().Get("force") != "true" {
		writeErr(w, 409, fmt.Sprintf("host still runs %d VMs; drain it first or pass force=true", len(vms)))
		return
	}
	s.db.Exec(r.Context(), `DELETE FROM hosts WHERE id = ?`, id)
	s.emit(r.Context(), "host.removed", "", "", id, nil)
	writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) handleCreateJoinToken(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Zone       string            `json:"zone"`
		Labels     map[string]string `json:"labels"`
		TTLSeconds int               `json:"ttl_seconds"`
	}
	json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body)
	if body.TTLSeconds <= 0 {
		body.TTLSeconds = 3600
	}
	tok, err := s.createJoinToken(r.Context(), body.Zone, body.Labels, time.Duration(body.TTLSeconds)*time.Second)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"token": tok, "expires_in_seconds": body.TTLSeconds})
}

func (s *Server) handleCreateToken(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Kind    string   `json:"kind"`
		Name    string   `json:"name"`
		Tenants []string `json:"tenants"`
	}
	json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&body)
	if body.Kind == "" {
		body.Kind = KindService
	}
	if body.Kind != KindService && body.Kind != KindAdmin {
		writeErr(w, 400, "kind must be service or admin")
		return
	}
	id, tok, err := s.createToken(r.Context(), body.Kind, body.Name, body.Tenants)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"id": id, "token": tok, "kind": body.Kind, "tenants": body.Tenants})
}

func (s *Server) handleListTokens(w http.ResponseWriter, r *http.Request) {
	rows, err := s.db.Query(r.Context(), `SELECT id, kind, tenants, name, created_at, expires_at, identity FROM tokens
		WHERE expires_at IS NULL OR expires_at > ? ORDER BY created_at`, nowMs())
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var id, kind, tenants, name, who string
		var created int64
		var expires sql.NullInt64
		rows.Scan(&id, &kind, &tenants, &name, &created, &expires, &who)
		var t []string
		json.Unmarshal([]byte(tenants), &t)
		tok := map[string]any{"id": id, "kind": kind, "tenants": t, "name": name, "created_at": float64(created) / 1000,
			"identity": who, "expires_at": nil}
		if expires.Valid {
			tok["expires_at"] = float64(expires.Int64) / 1000
		}
		out = append(out, tok)
	}
	writeJSON(w, 200, map[string]any{"tokens": out})
}

func (s *Server) handleDeleteToken(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	s.db.Exec(r.Context(), `DELETE FROM tokens WHERE id = ?`, id)
	tokenCache.Delete(id)
	writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) handleListSnapshots(w http.ResponseWriter, r *http.Request) {
	snaps, err := s.listSnapshots(r.Context(), "")
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if snaps == nil {
		snaps = []*Snapshot{}
	}
	writeJSON(w, 200, map[string]any{"snapshots": snaps})
}
