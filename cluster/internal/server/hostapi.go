package server

import (
	"context"
	"encoding/json"
	"log"
	"net"
	"net/http"
	"net/url"
	"time"

	"github.com/google/uuid"
	"github.com/jverre/flint/cluster/internal/pki"
)

// hostCertTTL: hosts renew at half-life (flint-cluster snapd does it).
const hostCertTTL = 7 * 24 * time.Hour

func (s *Server) hostHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/join", s.handleJoin)
	mux.HandleFunc("POST /v1/hosts/self/heartbeat", s.hostOnly(s.handleHeartbeat))
	mux.HandleFunc("POST /v1/hosts/self/events", s.hostOnly(s.handleHostEvents))
	mux.HandleFunc("POST /v1/hosts/self/renew", s.hostOnly(s.handleRenew))
	mux.HandleFunc("POST /v1/hosts/self/snapshots", s.hostOnly(s.handleHostSnapshot))
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, map[string]string{"status": "ok"}) })
	return mux
}

type hostCtxKey struct{}

func (s *Server) hostOnly(h func(http.ResponseWriter, *http.Request, *Host)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id, ok := hostFromRequest(r)
		if !ok {
			writeErr(w, http.StatusUnauthorized, "host certificate required")
			return
		}
		host, err := s.getHost(r.Context(), id)
		if err != nil {
			writeErr(w, http.StatusForbidden, "unknown or removed host")
			return
		}
		h(w, r, host)
	}
}

type joinReq struct {
	Token     string            `json:"token"`
	Name      string            `json:"name"`
	CSR       string            `json:"csr_pem"`
	AgentURL  string            `json:"agent_url"`
	SnapdURL  string            `json:"snapd_url"`
	Zone      string            `json:"zone"`
	Labels    map[string]string `json:"labels"`
	Backend   string            `json:"backend"`
	HostClass string            `json:"host_class"`
}

func hostnameOf(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	return u.Hostname()
}

func (s *Server) handleJoin(w http.ResponseWriter, r *http.Request) {
	var req joinReq
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeErr(w, 400, "invalid join request")
		return
	}
	tok, err := pki.DecodeJoinToken(req.Token)
	if err != nil {
		writeErr(w, 400, err.Error())
		return
	}
	if req.AgentURL == "" || req.SnapdURL == "" || req.CSR == "" {
		writeErr(w, 400, "agent_url, snapd_url and csr_pem are required")
		return
	}
	ctx := r.Context()
	var hash, zone, labels string
	var expires int64
	err = s.db.QueryRow(ctx, `SELECT secret_hash, zone, labels, expires_at FROM join_tokens WHERE id = ? AND used_at IS NULL`, tok.ID).
		Scan(&hash, &zone, &labels, &expires)
	if err != nil || hash != pki.HashSecret(tok.Secret) || nowMs() > expires {
		writeErr(w, http.StatusForbidden, "join token is invalid, expired or already used")
		return
	}
	hostID := "h-" + uuid.NewString()[:13]
	// Single use: only one joiner can flip used_at.
	res, err := s.db.Exec(ctx, `UPDATE join_tokens SET used_at = ?, host_id = ? WHERE id = ? AND used_at IS NULL`, nowMs(), hostID, tok.ID)
	if n, _ := res.RowsAffected(); err != nil || n != 1 {
		writeErr(w, http.StatusForbidden, "join token is invalid, expired or already used")
		return
	}
	names := []string{hostnameOf(req.AgentURL), hostnameOf(req.SnapdURL)}
	cert, err := s.hostsCA.IssueFromCSR([]byte(req.CSR), hostID, names, hostCertTTL)
	if err != nil {
		writeErr(w, 400, "csr: "+err.Error())
		return
	}
	if req.Zone != "" {
		zone = req.Zone
	}
	lbl := map[string]string{}
	json.Unmarshal([]byte(labels), &lbl)
	for k, v := range req.Labels {
		lbl[k] = v
	}
	name := req.Name
	if name == "" {
		name = hostID
	}
	now := nowMs()
	if _, err := s.db.Exec(ctx, `INSERT INTO hosts (id, name, agent_url, snapd_url, zone, labels, backend, host_class, state, state_since, last_heartbeat, created_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, hostID, name, req.AgentURL, req.SnapdURL, zone, mustJSON(lbl), req.Backend, req.HostClass,
		HostJoining, now, now, now); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	s.emit(ctx, "host.joined", "", "", hostID, map[string]any{"name": name, "zone": zone})
	log.Printf("host %s (%s) joined from %s", name, hostID, req.AgentURL)
	writeJSON(w, 200, map[string]any{
		"host_id":      hostID,
		"cert_pem":     string(cert),
		"hosts_ca_pem": string(s.hostsCA.CertPEM),
		"cp_ca_pem":    string(s.cpCA.CertPEM),
		"lease_ttl_ms": s.cfg.LeaseTTL.Milliseconds(),
	})
}

func (s *Server) handleRenew(w http.ResponseWriter, r *http.Request, h *Host) {
	var req struct {
		CSR string `json:"csr_pem"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeErr(w, 400, "invalid request")
		return
	}
	cert, err := s.hostsCA.IssueFromCSR([]byte(req.CSR), h.ID, []string{hostnameOf(h.AgentURL), hostnameOf(h.SnapdURL)}, hostCertTTL)
	if err != nil {
		writeErr(w, 400, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"cert_pem": string(cert)})
}

// hostVM is a VM as a host reports it.
type hostVM struct {
	ID              string `json:"vm_id"`
	State           string `json:"state"`
	PauseSnapshotID string `json:"pause_snapshot_id,omitempty"`
}

type heartbeatReq struct {
	Version   string         `json:"version"`
	Backend   string         `json:"backend"`
	HostClass string         `json:"host_class"`
	Capacity  Capacity       `json:"capacity"`
	VMs       []hostVM       `json:"vms"`
	Cached    []string       `json:"cached_snapshots"`
	Templates []HostTemplate `json:"templates"`
	Fenced    []string       `json:"fenced"`
}

// handleHeartbeat renews the host's lease and reconciles its VMs against the
// catalog. The answer lists VMs the host must delete: ones the catalog has
// moved elsewhere or deleted while the host was away.
func (s *Server) handleHeartbeat(w http.ResponseWriter, r *http.Request, h *Host) {
	var req heartbeatReq
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<20)).Decode(&req); err != nil {
		writeErr(w, 400, "invalid heartbeat")
		return
	}
	ctx := r.Context()
	now := nowMs()
	if req.Cached == nil {
		req.Cached = []string{}
	}
	if req.Templates == nil {
		req.Templates = []HostTemplate{}
	}
	if _, err := s.db.Exec(ctx, `UPDATE hosts SET capacity = ?, cached = ?, templates = ?, version = ?, backend = ?, host_class = ?, last_heartbeat = ? WHERE id = ?`,
		mustJSON(req.Capacity), mustJSON(req.Cached), mustJSON(req.Templates), req.Version, req.Backend, req.HostClass, now, h.ID); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	switch h.State {
	case HostJoining, HostUnreachable, HostIsolated, HostDown:
		s.setHostState(ctx, h.ID, HostReady)
		s.emit(ctx, "host.ready", "", "", h.ID, map[string]any{"previous": h.State})
		log.Printf("host %s is back (was %s)", h.Name, h.State)
	}
	s.syncTemplates(ctx, h, req.Templates)

	evict := []string{}
	reported := map[string]hostVM{}
	for _, hv := range req.VMs {
		reported[hv.ID] = hv
		v, err := s.getVM(ctx, hv.ID)
		if err == nil && v.State == VMMigrating {
			continue // both hosts hold it until the move commits or rolls back
		}
		if err != nil || v.HostID != h.ID {
			// Deleted, or now owned by another host (it was recovered or moved
			// while this host was away): the stale copy must go.
			if err == nil || isNotFound(err) {
				evict = append(evict, hv.ID)
			}
			continue
		}
		s.reconcileReportedVM(ctx, h, v, hv)
	}
	// VMs the catalog places here that the host no longer has were deleted
	// locally (timeout policy, error cleanup).
	mine, _ := s.listVMs(ctx, `host_id = ? AND state = ?`, h.ID, VMRunning)
	for _, v := range mine {
		if _, ok := reported[v.ID]; ok || now-v.UpdatedAt < 20000 {
			continue
		}
		if v.VMSetID != "" {
			s.updateVM(ctx, v.ID, vmUpdate{State: str(VMLost), Error: str("disappeared from its host")})
			continue
		}
		s.deleteVMRow(ctx, v.ID)
		log.Printf("vm %s no longer on host %s; removed from catalog", short(v.ID), h.Name)
	}
	writeJSON(w, 200, map[string]any{
		"host_id":      h.ID,
		"lease_ttl_ms": s.cfg.LeaseTTL.Milliseconds(),
		"evict":        evict,
	})
}

// reconcileReportedVM folds a host's view of one of its VMs into the catalog.
func (s *Server) reconcileReportedVM(ctx context.Context, h *Host, v *VM, hv hostVM) {
	switch {
	case v.State == VMRunning && (hv.State == "Error" || hv.State == "Dead"):
		// The VMM died on a healthy host: recover in place if the class allows.
		if v.Availability == AvailEphemeral || v.VMSetID != "" {
			s.updateVM(ctx, v.ID, vmUpdate{State: str(VMLost), Error: str("VM process died")})
			s.emit(ctx, "vm.lost", v.Tenant, v.ID, h.ID, map[string]any{"vm_id": v.ID, "reason": "crashed"})
			if hh, ok := s.hostIfUsable(ctx, h.ID); ok {
				go s.deleteOnHost(context.Background(), hh, v.ID)
			}
			return
		}
		s.updateVM(ctx, v.ID, vmUpdate{State: str(VMRecovering), Error: str("crashed on " + h.Name)})
		s.emit(ctx, "vm.crashed", v.Tenant, v.ID, h.ID, map[string]any{"vm_id": v.ID})
		go func(v *VM) {
			v.State = VMRecovering
			s.recoverVM(context.Background(), v, h.ID, true)
		}(v)
	case v.State == VMLost && v.VMSetID == "" && (hv.State == "Running" || hv.State == "Started"):
		// Its host was only partitioned and the VM kept running: it's not lost after all.
		s.updateVM(ctx, v.ID, vmUpdate{State: str(VMRunning), Error: str("")})
		s.emit(ctx, "vm.found", v.Tenant, v.ID, h.ID, map[string]any{"vm_id": v.ID})
	case v.State == VMRunning && hv.State == "Paused":
		// Paused on the host by its own timeout policy.
		upd := vmUpdate{State: str(VMPaused)}
		if hv.PauseSnapshotID != "" {
			upd.PauseID = str(hv.PauseSnapshotID)
			s.insertSnapshot(ctx, &Snapshot{ID: hv.PauseSnapshotID, Kind: "pause", VMID: v.ID, Tenant: v.Tenant, Backend: h.Backend,
				HostClass: h.HostClass, SourceHost: h.ID, Uploaded: true})
		}
		s.updateVM(ctx, v.ID, upd)
	}
}

// syncTemplates records template snapshots hosts publish and finishes builds.
func (s *Server) syncTemplates(ctx context.Context, h *Host, ts []HostTemplate) {
	for _, t := range ts {
		if t.ID == "default" {
			continue
		}
		tmpl, err := s.getTemplate(ctx, t.ID)
		if err != nil {
			continue // a template only this host knows (built before it joined)
		}
		if t.SnapshotID != "" {
			if _, err := s.getSnapshot(ctx, t.SnapshotID); isNotFound(err) {
				s.insertSnapshot(ctx, &Snapshot{ID: t.SnapshotID, Kind: "template", TemplateID: t.ID, Tenant: tmpl.Tenant,
					Backend: h.Backend, HostClass: h.HostClass, SourceHost: h.ID, Uploaded: true})
			}
		}
		if tmpl.BuildHost == h.ID && tmpl.Status == "building" && (t.Status == "ready" && t.SnapshotID != "" || t.Status == "error") {
			s.db.Exec(ctx, `UPDATE templates SET status = ?, error = ?, updated_at = ? WHERE id = ?`, t.Status, t.Error, nowMs(), t.ID)
			s.emit(ctx, "template."+t.Status, tmpl.Tenant, "", h.ID, map[string]any{"template_id": t.ID})
		}
	}
}

// handleHostEvents ingests a batch of the host's own events.
func (s *Server) handleHostEvents(w http.ResponseWriter, r *http.Request, h *Host) {
	var req struct {
		Events []map[string]any `json:"events"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<20)).Decode(&req); err != nil {
		writeErr(w, 400, "invalid events")
		return
	}
	ctx := r.Context()
	for _, e := range req.Events {
		typ, _ := e["type"].(string)
		if typ == "" {
			continue
		}
		vmID, _ := e["vm_id"].(string)
		if vmID == "" {
			if vm, ok := e["vm"].(map[string]any); ok {
				vmID, _ = vm["vm_id"].(string)
			}
		}
		tenant := ""
		if vmID != "" {
			if v, err := s.getVM(ctx, vmID); err == nil {
				tenant = v.Tenant
				if v.HostID != h.ID {
					continue // a stale copy on a host that lost the VM
				}
			}
		}
		e["host_id"] = h.ID
		if tenant != "" {
			e["tenant"] = tenant
		}
		ts := nowMs()
		if f, ok := e["ts"].(float64); ok {
			ts = int64(f * 1000)
		}
		s.db.Exec(ctx, `INSERT INTO events (ts, type, tenant, vm_id, host_id, payload) VALUES (?, ?, ?, ?, ?, ?)`,
			ts, typ, tenant, vmID, h.ID, mustJSON(e))
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

// handleHostSnapshot registers a snapshot a host made on its own (a template build).
func (s *Server) handleHostSnapshot(w http.ResponseWriter, r *http.Request, h *Host) {
	var sn Snapshot
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&sn); err != nil || sn.ID == "" || sn.Kind != "template" {
		writeErr(w, 400, "invalid snapshot")
		return
	}
	sn.Backend, sn.HostClass, sn.SourceHost = h.Backend, h.HostClass, h.ID
	if err := s.insertSnapshot(r.Context(), &sn); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

// createJoinToken makes a single-use host join token.
func (s *Server) createJoinToken(ctx context.Context, zone string, labels map[string]string, ttl time.Duration) (string, error) {
	id := uuid.NewString()
	secret := pki.RandomSecret(24)
	if labels == nil {
		labels = map[string]string{}
	}
	if _, err := s.db.Exec(ctx, `INSERT INTO join_tokens (id, secret_hash, zone, labels, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)`,
		id, pki.HashSecret(secret), zone, mustJSON(labels), nowMs()+ttl.Milliseconds(), nowMs()); err != nil {
		return "", err
	}
	server := s.cfg.HostEndpoint
	if server == "" {
		_, port, _ := net.SplitHostPort(s.cfg.HostListen)
		server = "https://127.0.0.1:" + port
	}
	return pki.JoinToken{Server: server, ID: id, Secret: secret, Fingerprint: s.cpCA.Fingerprint()}.Encode(), nil
}

// CreateJoinToken is createJoinToken for the CLI (--print-join-token).
func (s *Server) CreateJoinToken(ctx context.Context, zone string, ttl time.Duration) (string, error) {
	return s.createJoinToken(ctx, zone, nil, ttl)
}
