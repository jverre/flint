package server

// Who did what: events carry the actor that caused them, and every API call
// that changes something (and every sign-in) is appended to a hash-chained
// audit log (store/events.go).

import (
	"bufio"
	"context"
	"errors"
	"log"
	"net"
	"net/http"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/jverre/flint/cluster/internal/auth"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// statusRecorder notes the response status, keeping the writer's abilities
// (flushing for streams, hijacking for WebSockets).
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	if r.status == 0 {
		r.status = code
	}
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	return r.ResponseWriter.Write(b)
}

func (r *statusRecorder) Flush() {
	if f, ok := r.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func (r *statusRecorder) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	h, ok := r.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, errors.New("hijacking not supported")
	}
	if r.status == 0 {
		r.status = http.StatusSwitchingProtocols
	}
	return h.Hijack()
}

func (r *statusRecorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }

// audited: API calls that change something, and terminals opened.
func audited(r *http.Request) bool {
	if strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
		return strings.HasSuffix(r.URL.Path, "/terminal")
	}
	return r.Method != http.MethodGet && r.Method != http.MethodHead && r.Method != http.MethodOptions
}

// withAudit runs h as p and, for audited requests, records the call afterwards.
func (s *Server) withAudit(w http.ResponseWriter, r *http.Request, p *auth.Principal, h http.HandlerFunc) {
	reqID := uuid.NewString()[:16]
	w.Header().Set("X-Request-Id", reqID)
	rec := &statusRecorder{ResponseWriter: w}
	ctx := auth.WithPrincipal(r.Context(), p)
	h(rec, r.WithContext(ctx))
	if !audited(r) {
		return
	}
	tenant := ""
	if rest, ok := strings.CutPrefix(r.URL.Path, "/vms/"); ok {
		if id, _, _ := strings.Cut(rest, "/"); id != "" {
			if v, err := s.store.GetVM(r.Context(), id); err == nil {
				tenant = v.Tenant
			}
		}
	}
	actor, name := auth.ActorOf(ctx)
	status := rec.status
	if status == 0 {
		status = http.StatusOK
	}
	if err := s.store.AppendAudit(context.WithoutCancel(r.Context()), store.AuditEntry{Actor: actor, ActorName: name, Method: r.Method,
		Path: r.URL.Path, Status: status, Tenant: tenant, RequestID: reqID}, model.NowMs()); err != nil {
		log.Printf("audit: recording %s %s: %v", r.Method, r.URL.Path, err)
	}
}

func (s *Server) handleAuditList(w http.ResponseWriter, r *http.Request) error {
	since, _ := strconv.ParseInt(r.URL.Query().Get("since"), 10, 64)
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	if limit <= 0 || limit > 5000 {
		limit = 500
	}
	es, err := s.store.AuditEntries(r.Context(), since, limit)
	if err != nil {
		return err
	}
	next := since
	if len(es) > 0 {
		next = es[len(es)-1].Seq
	}
	return httpx.JSON(w, map[string]any{"entries": es, "next": next})
}

func (s *Server) handleAuditVerify(w http.ResponseWriter, r *http.Request) error {
	res, err := s.store.VerifyAudit(r.Context())
	if err != nil {
		return err
	}
	return httpx.JSON(w, res)
}
