package server

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"log"
	"net"
	"net/http"
	"net/url"
	"time"

	"github.com/google/uuid"
	"github.com/jverre/flint/cluster/internal/hostagent"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/pki"
	"github.com/jverre/flint/cluster/internal/store"
)

// hostCertTTL: hosts renew at half-life (flint-cluster snapd does it).
const hostCertTTL = 7 * 24 * time.Hour

// hostHandler is the API host agents and snapd call, over mTLS.
func (s *Server) hostHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/join", httpx.Handle(s.handleJoin))
	mux.HandleFunc("POST /v1/hosts/self/heartbeat", s.hostOnly(s.handleHeartbeat))
	mux.HandleFunc("POST /v1/hosts/self/network-release", s.hostOnly(s.handleNetworkRelease))
	mux.HandleFunc("POST /v1/hosts/self/handover", s.hostOnly(s.handleHandover))
	mux.HandleFunc("POST /v1/hosts/self/events", s.hostOnly(s.handleHostEvents))
	mux.HandleFunc("POST /v1/hosts/self/renew", s.hostOnly(s.handleRenew))
	mux.HandleFunc("POST /v1/hosts/self/snapshots", s.hostOnly(s.handleHostSnapshot))
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		httpx.WriteJSON(w, 200, map[string]string{"status": "ok"})
	})
	return mux
}

// hostHandlerFunc handles a request from an authenticated host.
type hostHandlerFunc func(w http.ResponseWriter, r *http.Request, h *model.Host) error

func (s *Server) hostOnly(h hostHandlerFunc) http.HandlerFunc {
	return httpx.Handle(func(w http.ResponseWriter, r *http.Request) error {
		id, ok := hostFromRequest(r)
		if !ok {
			return httpx.NewError(http.StatusUnauthorized, "host certificate required")
		}
		host, err := s.store.GetHost(r.Context(), id)
		if store.IsNotFound(err) {
			return httpx.Forbidden("unknown or removed host")
		}
		if err != nil {
			return err
		}
		return h(w, r, host)
	})
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

var errBadJoinToken = httpx.Forbidden(store.ErrJoinTokenInvalid.Error())

func (s *Server) handleJoin(w http.ResponseWriter, r *http.Request) error {
	var req joinReq
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		return httpx.BadRequest("invalid join request")
	}
	tok, err := pki.DecodeJoinToken(req.Token)
	if err != nil {
		return httpx.BadRequest(err.Error())
	}
	if req.AgentURL == "" || req.SnapdURL == "" || req.CSR == "" {
		return httpx.BadRequest("agent_url, snapd_url and csr_pem are required")
	}
	ctx := r.Context()
	zone, labels := "", map[string]string{}
	hostID := "h-" + uuid.NewString()[:13]
	if tok.ID == "static" {
		if s.cfg.JoinSecret == "" || subtle.ConstantTimeCompare([]byte(tok.Secret), []byte(s.cfg.JoinSecret)) != 1 {
			return errBadJoinToken
		}
	} else if zone, labels, err = s.store.UseJoinToken(ctx, tok.ID, pki.HashSecret(tok.Secret), hostID); err != nil {
		if errors.Is(err, store.ErrJoinTokenInvalid) {
			return errBadJoinToken
		}
		return err
	}
	cert, err := s.hostsCA.IssueFromCSR([]byte(req.CSR), hostID, []string{hostnameOf(req.AgentURL), hostnameOf(req.SnapdURL)}, hostCertTTL)
	if err != nil {
		return httpx.BadRequest("csr: " + err.Error())
	}
	if req.Zone != "" {
		zone = req.Zone
	}
	for k, v := range req.Labels {
		labels[k] = v
	}
	name := req.Name
	if name == "" {
		name = hostID
	}
	now := model.NowMs()
	if err := s.store.InsertHost(ctx, &model.Host{ID: hostID, Name: name, AgentURL: req.AgentURL, SnapdURL: req.SnapdURL, Zone: zone,
		Labels: labels, Backend: req.Backend, HostClass: req.HostClass, State: model.HostJoining, StateSince: now, LastHeartbeat: now,
		CreatedAt: now}); err != nil {
		return err
	}
	s.eng.Emit(ctx, "host.joined", "", "", hostID, map[string]any{"name": name, "zone": zone})
	log.Printf("host %s (%s) joined from %s", name, hostID, req.AgentURL)
	return httpx.JSON(w, map[string]any{
		"host_id":      hostID,
		"cert_pem":     string(cert),
		"hosts_ca_pem": string(s.hostsCA.CertPEM),
		"cp_ca_pem":    string(s.cpCA.CertPEM),
		"lease_ttl_ms": s.cfg.LeaseTTL.Milliseconds(),
	})
}

func (s *Server) handleRenew(w http.ResponseWriter, r *http.Request, h *model.Host) error {
	var req struct {
		CSR string `json:"csr_pem"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		return httpx.BadRequest("invalid request")
	}
	cert, err := s.hostsCA.IssueFromCSR([]byte(req.CSR), h.ID, []string{hostnameOf(h.AgentURL), hostnameOf(h.SnapdURL)}, hostCertTTL)
	if err != nil {
		return httpx.BadRequest(err.Error())
	}
	return httpx.JSON(w, map[string]any{"cert_pem": string(cert)})
}

// handleHeartbeat renews the host's lease and reconciles its VMs against the
// catalog. The answer lists VMs the host must delete: ones the catalog has
// moved elsewhere or deleted while the host was away.
func (s *Server) handleHeartbeat(w http.ResponseWriter, r *http.Request, h *model.Host) error {
	var hb hostagent.Heartbeat
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<20)).Decode(&hb); err != nil {
		return httpx.BadRequest("invalid heartbeat")
	}
	if h.HandoverToken != "" && hb.HandoverToken != h.HandoverToken {
		return httpx.Conflict("snapd owns lease renewal during agent handover")
	}
	ctx := r.Context()
	err := s.store.RecordHeartbeat(ctx, h.ID, h.HandoverToken, store.Heartbeat{PatchState: hb.PatchState, Capacity: hb.Capacity,
		Cached: hb.Cached, Templates: hb.Templates, Version: hb.Version, Backend: hb.Backend, HostClass: hb.HostClass, At: model.NowMs()})
	if errors.Is(err, store.ErrHandoverRaced) {
		return httpx.Conflict(err.Error())
	}
	if err != nil {
		return err
	}
	evict := s.eng.ReconcileHeartbeat(ctx, h, &hb)
	return httpx.JSON(w, map[string]any{
		"host_id":      h.ID,
		"lease_ttl_ms": s.cfg.LeaseTTL.Milliseconds(),
		"evict":        evict,
	})
}

const maxHandover = 120 * time.Second

// handleHandover: during an explicit, bounded agent upgrade snapd renews
// only the frozen VM set. It cannot reconcile VM state, adopt ownership or
// revive an expired lease.
func (s *Server) handleHandover(w http.ResponseWriter, r *http.Request, h *model.Host) error {
	var req struct {
		Token string   `json:"token"`
		VMs   []string `json:"vms"`
	}
	if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req) != nil || len(req.Token) < 32 || len(req.Token) > 128 {
		return httpx.BadRequest("invalid handover")
	}
	ctx := r.Context()
	if h.HandoverToken == "" {
		// Starting one: hold placement so no operation starts on the host meanwhile.
		guard, err := s.eng.LockPlacement(ctx)
		if err != nil {
			return err
		}
		defer guard.Release()
		if h, err = s.store.GetHost(ctx, h.ID); err != nil {
			return err
		}
	}
	now := model.NowMs()
	leaseFloor := now - s.cfg.LeaseTTL.Milliseconds()
	if h.LastHeartbeat <= leaseFloor || (h.HandoverToken != "" && (h.HandoverToken != req.Token || h.HandoverUntil <= now)) {
		return httpx.Conflict("handover or host lease expired")
	}
	vms, err := s.store.ListVMs(ctx, store.VMFilter{HostID: h.ID, States: model.ActiveVMStates})
	if err != nil {
		return err
	}
	busy, err := s.eng.BusyVMs(ctx)
	if err != nil {
		return err
	}
	frozen := map[string]bool{}
	for _, id := range req.VMs {
		frozen[id] = true
	}
	for _, v := range vms {
		if busy[v.ID] || v.State != model.VMRunning {
			return httpx.Conflict("VM operation in progress")
		}
		if v.Availability.Recoverable() {
			if !frozen[v.ID] {
				return httpx.Conflict("frozen VM set incomplete")
			}
			delete(frozen, v.ID)
		}
	}
	if len(frozen) != 0 {
		return httpx.Conflict("VM ownership changed")
	}
	until := h.HandoverUntil
	if h.HandoverToken == "" {
		until = now + maxHandover.Milliseconds()
	}
	err = s.store.ClaimHandover(ctx, h.ID, h.HandoverToken, req.Token, until, now, leaseFloor)
	if errors.Is(err, store.ErrHandoverLost) {
		return httpx.Conflict(err.Error())
	}
	if err != nil {
		return err
	}
	return httpx.JSON(w, map[string]any{"lease_ttl_ms": min(s.cfg.LeaseTTL.Milliseconds(), until-now), "handover_until_ms": until})
}

// handleHostEvents ingests a batch of the host's own events.
func (s *Server) handleHostEvents(w http.ResponseWriter, r *http.Request, h *model.Host) error {
	var req struct {
		Events []map[string]any `json:"events"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<20)).Decode(&req); err != nil {
		return httpx.BadRequest("invalid events")
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
			if v, err := s.store.GetVM(ctx, vmID); err == nil {
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
		ts := model.NowMs()
		if f, ok := e["ts"].(float64); ok {
			ts = int64(f * 1000)
		}
		// The host knows who caused it when the gateway told it (X-Flint-Actor).
		actor, _ := e["actor"].(string)
		name, _ := e["actor_name"].(string)
		if actor == "" || actor == "control-plane" || actor == "system" {
			actor, name = "host:"+h.ID, ""
			e["actor"] = actor
			delete(e, "actor_name")
		}
		// A failed insert is logged, not retried: the host would resend the whole batch.
		if err := s.store.InsertEvent(ctx, store.Event{TS: ts, Type: typ, Tenant: tenant, VMID: vmID, HostID: h.ID, Actor: actor,
			ActorName: name, Payload: e}); err != nil {
			log.Printf("host %s event %s: %v", h.Name, typ, err)
		}
	}
	return httpx.OK(w)
}

// handleHostSnapshot registers a snapshot a host made on its own (a template build).
func (s *Server) handleHostSnapshot(w http.ResponseWriter, r *http.Request, h *model.Host) error {
	var sn model.Snapshot
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&sn); err != nil || sn.ID == "" || sn.Kind != model.SnapshotTemplate {
		return httpx.BadRequest("invalid snapshot")
	}
	sn.Backend, sn.HostClass, sn.SourceHost = h.Backend, h.HostClass, h.ID
	if err := s.store.InsertSnapshot(r.Context(), &sn); err != nil {
		return err
	}
	return httpx.OK(w)
}

// handleNetworkRelease gates gateway garbage collection on authoritative
// catalog absence. Temporary agent outages and migration source cleanup
// must not discard live NAT state.
func (s *Server) handleNetworkRelease(w http.ResponseWriter, r *http.Request, h *model.Host) error {
	var req struct {
		VMID string `json:"vm_id"`
	}
	if json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16)).Decode(&req) != nil || req.VMID == "" {
		return httpx.BadRequest("VM ID required")
	}
	_, err := s.store.GetVM(r.Context(), req.VMID)
	if err != nil && !store.IsNotFound(err) {
		return err
	}
	return httpx.JSON(w, map[string]bool{"allowed": store.IsNotFound(err)})
}

// createJoinToken makes a single-use host join token.
func (s *Server) createJoinToken(ctx context.Context, zone string, labels map[string]string, ttl time.Duration) (string, error) {
	id := uuid.NewString()
	secret := pki.RandomSecret(24)
	if err := s.store.InsertJoinToken(ctx, id, pki.HashSecret(secret), zone, labels, model.NowMs()+ttl.Milliseconds()); err != nil {
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
