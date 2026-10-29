package server

import (
	"net/http"

	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/webui"
)

// apiHandler is the public API. Handlers return their failure as an error
// (httpx.Handle turns it into an answer).
func (s *Server) apiHandler() http.Handler {
	mux := http.NewServeMux()
	// auth: any valid credential; admin: an admin one; mutating: may carry
	// an Idempotency-Key.
	auth := func(h httpx.HandlerFunc) http.HandlerFunc { return s.requireAuth(false, httpx.Handle(h)) }
	admin := func(h httpx.HandlerFunc) http.HandlerFunc { return s.requireAuth(true, httpx.Handle(h)) }
	mutating := func(h httpx.HandlerFunc) http.HandlerFunc { return s.requireAuth(false, s.idempotent(httpx.Handle(h))) }

	mux.HandleFunc("GET /health", httpx.Handle(s.handleHealth))

	// Sign-in with identity providers; public by nature.
	s.sso.Routes(mux)

	// VMs: the daemon's API, served for the whole cluster.
	mux.HandleFunc("POST /vms", mutating(s.handleCreateVM))
	mux.HandleFunc("GET /vms", auth(s.handleListVMs))
	mux.HandleFunc("GET /vms/{id}", auth(s.handleGetVM))
	mux.HandleFunc("DELETE /vms/{id}", mutating(s.handleDeleteVM))
	mux.HandleFunc("POST /vms/{id}/pause", mutating(s.handlePause))
	mux.HandleFunc("POST /vms/{id}/resume", mutating(s.handleResume))
	mux.HandleFunc("POST /vms/{id}/fork", mutating(s.handleFork))
	mux.HandleFunc("POST /vms/{id}/checkpoint", mutating(s.handleCheckpoint))
	mux.HandleFunc("POST /vms/{id}/migrate", mutating(s.handleMigrate))
	mux.HandleFunc("PATCH /vms/{id}", auth(s.handlePatchVM))
	mux.HandleFunc("PUT /vms/{id}/network-policy", auth(s.handlePutNetworkPolicy))
	mux.HandleFunc("/vms/{id}/mounts", auth(notInCluster("volume mounts")))
	mux.HandleFunc("/vms/{id}/mounts/{rest...}", auth(notInCluster("volume mounts")))
	// Everything else on a VM goes to its host: exec, processes, files,
	// metrics, logs and terminal WebSockets.
	mux.HandleFunc("/vms/{id}/{rest...}", auth(s.handleGateway))

	mux.HandleFunc("GET /events", auth(s.handleEventsWS))
	mux.HandleFunc("GET /v1/events", auth(s.handleEventsList))
	mux.HandleFunc("GET /vms/{id}/timeline", auth(s.handleTimeline))
	mux.HandleFunc("GET /v1/audit", admin(s.handleAuditList))
	mux.HandleFunc("GET /v1/audit/verify", admin(s.handleAuditVerify))

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
	mux.HandleFunc("GET /v1/hosts/{id}/drain-plan", admin(s.handleDrainPlan))
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

func notInCluster(what string) httpx.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) error {
		return httpx.NewError(http.StatusNotImplemented, what+" are host-local and not available through the cluster API yet")
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) error {
	hosts, err := s.store.ListHosts(r.Context())
	if err != nil {
		httpx.WriteJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "error", "detail": err.Error()})
		return nil
	}
	ready := 0
	backends := map[string]bool{}
	for _, h := range hosts {
		if s.eng.HostUsable(h) {
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
	return httpx.JSON(w, map[string]any{
		"status": "ok", "role": "control-plane", "replica": s.cfg.ReplicaID, "leader": s.isLeader(),
		"hosts": len(hosts), "hosts_ready": ready, "backend_kind": kind,
		// The SDK and tests read this to know sandboxes can be created.
		"golden_snapshot_ready": ready > 0,
	})
}
