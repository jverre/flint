package server

// Who did what: events carry the actor that caused them, and every API call
// that changes something (and every sign-in) is appended to a hash-chained
// audit log, like the single daemon's (src/flint/daemon/eventlog.py):
//
//	hash = sha256(prev_hash, seq, ts_ms, actor, actor_name, method, path,
//	              status, tenant, request_id)   // joined by \x1f, UTF-8
//
// The first entry's prev_hash is 64 zeros. Request bodies are never recorded.
// Replicas append under a database lock, so the chain has no gaps or forks.

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/jverre/flint/cluster/internal/db"
)

const genesisHash = "0000000000000000000000000000000000000000000000000000000000000000"

type auditEntry struct {
	Seq       int64  `json:"seq"`
	TsMs      int64  `json:"ts_ms"`
	Actor     string `json:"actor"`
	ActorName string `json:"actor_name"`
	Method    string `json:"method"`
	Path      string `json:"path"`
	Status    int    `json:"status"`
	Tenant    string `json:"tenant"`
	RequestID string `json:"request_id"`
	PrevHash  string `json:"prev_hash"`
	Hash      string `json:"hash"`
}

func (e auditEntry) chainHash() string {
	parts := []string{e.PrevHash, strconv.FormatInt(e.Seq, 10), strconv.FormatInt(e.TsMs, 10), e.Actor, e.ActorName,
		e.Method, e.Path, strconv.Itoa(e.Status), e.Tenant, e.RequestID}
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x1f")))
	return hex.EncodeToString(sum[:])
}

// actorOf is who a request acts as: its token (or sign-in session), or the
// control plane itself for background work.
func actorOf(ctx context.Context) (string, string) {
	if p, ok := ctx.Value(principalKey).(*Principal); ok && p != nil {
		return "token:" + p.TokenID, firstNonEmpty(p.Identity, p.Name)
	}
	return "system", ""
}

// appendAudit adds e to the chain, filling in seq, ts and the hashes.
func (s *Server) appendAudit(ctx context.Context, e auditEntry) error {
	return s.db.Tx(ctx, func(tx *db.Tx) error {
		// Serialize appends: take the write lock before reading the chain's end.
		lock := `SELECT pg_advisory_xact_lock(74657)`
		if s.db.Dialect != "postgres" {
			// SQLite can't upgrade a read lock while another writer waits; writing first waits instead.
			lock = `INSERT INTO meta (key, value) VALUES ('audit_lock', '') ON CONFLICT (key) DO UPDATE SET value = ''`
		}
		if _, err := tx.Exec(ctx, lock); err != nil {
			return err
		}
		var prevSeq int64
		prevHash := genesisHash
		err := tx.QueryRow(ctx, `SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1`).Scan(&prevSeq, &prevHash)
		if err != nil && !isNotFound(err) {
			return err
		}
		e.Seq, e.TsMs, e.PrevHash = prevSeq+1, nowMs(), prevHash
		e.Hash = e.chainHash()
		_, err = tx.Exec(ctx, `INSERT INTO audit (seq, ts_ms, actor, actor_name, method, path, status, tenant, request_id, prev_hash, hash)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			e.Seq, e.TsMs, e.Actor, e.ActorName, e.Method, e.Path, e.Status, e.Tenant, e.RequestID, e.PrevHash, e.Hash)
		return err
	})
}

func (s *Server) auditEntries(ctx context.Context, since int64, limit int) ([]auditEntry, error) {
	rows, err := s.db.Query(ctx, `SELECT seq, ts_ms, actor, actor_name, method, path, status, tenant, request_id, prev_hash, hash
		FROM audit WHERE seq > ? ORDER BY seq LIMIT ?`, since, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []auditEntry{}
	for rows.Next() {
		var e auditEntry
		if err := rows.Scan(&e.Seq, &e.TsMs, &e.Actor, &e.ActorName, &e.Method, &e.Path, &e.Status, &e.Tenant,
			&e.RequestID, &e.PrevHash, &e.Hash); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

func (s *Server) verifyAudit(ctx context.Context) (map[string]any, error) {
	var prev *auditEntry
	count := 0
	var since int64
	for {
		batch, err := s.auditEntries(ctx, since, 5000)
		if err != nil {
			return nil, err
		}
		for i := range batch {
			e := batch[i]
			if prev != nil && (e.PrevHash != prev.Hash || e.Seq != prev.Seq+1) {
				return map[string]any{"ok": false, "count": count, "broken_at": e.Seq, "why": "doesn't follow the entry before it"}, nil
			}
			if e.chainHash() != e.Hash {
				return map[string]any{"ok": false, "count": count, "broken_at": e.Seq, "why": "hash doesn't match its fields"}, nil
			}
			prev, count = &e, count+1
		}
		if len(batch) < 5000 {
			last := genesisHash
			if prev != nil {
				last = prev.Hash
			}
			return map[string]any{"ok": true, "count": count, "last_hash": last}, nil
		}
		since = batch[len(batch)-1].Seq
	}
}

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

// withAudit runs h and, for audited requests, records the call afterwards.
func (s *Server) withAudit(w http.ResponseWriter, r *http.Request, p *Principal, h http.HandlerFunc) {
	reqID := uuid.NewString()[:16]
	w.Header().Set("X-Request-Id", reqID)
	rec := &statusRecorder{ResponseWriter: w}
	ctx := context.WithValue(context.WithValue(r.Context(), principalKey, p), requestIDKey, reqID)
	h(rec, r.WithContext(ctx))
	if !audited(r) {
		return
	}
	tenant := ""
	if strings.HasPrefix(r.URL.Path, "/vms/") {
		if id, _, _ := strings.Cut(strings.TrimPrefix(r.URL.Path, "/vms/"), "/"); id != "" {
			if v, err := s.getVM(r.Context(), id); err == nil {
				tenant = v.Tenant
			}
		}
	}
	actor, name := actorOf(ctx)
	status := rec.status
	if status == 0 {
		status = http.StatusOK
	}
	if err := s.appendAudit(context.WithoutCancel(r.Context()), auditEntry{Actor: actor, ActorName: name, Method: r.Method,
		Path: r.URL.Path, Status: status, Tenant: tenant, RequestID: reqID}); err != nil {
		log.Printf("audit: recording %s %s: %v", r.Method, r.URL.Path, err)
	}
}

const requestIDKey ctxKey = 1

func (s *Server) handleAuditList(w http.ResponseWriter, r *http.Request) {
	since, _ := strconv.ParseInt(r.URL.Query().Get("since"), 10, 64)
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	if limit <= 0 || limit > 5000 {
		limit = 500
	}
	es, err := s.auditEntries(r.Context(), since, limit)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	next := since
	if len(es) > 0 {
		next = es[len(es)-1].Seq
	}
	writeJSON(w, 200, map[string]any{"entries": es, "next": next})
}

func (s *Server) handleAuditVerify(w http.ResponseWriter, r *http.Request) {
	res, err := s.verifyAudit(r.Context())
	if err != nil {
		writeErr(w, 500, fmt.Sprint(err))
		return
	}
	writeJSON(w, 200, res)
}
