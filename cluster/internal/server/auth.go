package server

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/jverre/flint/cluster/internal/pki"
)

// Credential kinds.
const (
	KindAdmin   = "admin"   // operators: everything, incl. hosts and tokens
	KindService = "service" // the builder's backend: VMs, templates, VM sets (optionally pinned to tenants)
)

// Principal is an authenticated API caller.
type Principal struct {
	TokenID string
	Kind    string
	Tenants []string // empty = any tenant (service tokens)
}

func (p *Principal) IsAdmin() bool { return p.Kind == KindAdmin }

// CanTenant reports whether the caller may act on a tenant's resources.
func (p *Principal) CanTenant(t string) bool {
	if p.IsAdmin() || len(p.Tenants) == 0 {
		return true
	}
	for _, x := range p.Tenants {
		if x == t {
			return true
		}
	}
	return false
}

// Tokens look like flint_<id>_<secret>; only sha256(secret) is stored.
func formatToken(id, secret string) string { return "flint_" + id + "_" + secret }

func parseToken(tok string) (id, secret string, ok bool) {
	if !strings.HasPrefix(tok, "flint_") {
		return "", "", false
	}
	rest := strings.TrimPrefix(tok, "flint_")
	i := strings.Index(rest, "_")
	if i <= 0 {
		return "", "", false
	}
	return rest[:i], rest[i+1:], true
}

type tokenRow struct {
	kind, hash string
	tenants    []string
	loaded     time.Time
}

var tokenCache sync.Map // id -> tokenRow

// createToken stores a new credential and returns its string form (shown once).
func (s *Server) createToken(ctx context.Context, kind, name string, tenants []string) (string, string, error) {
	id := pki.RandomSecret(6)
	id = strings.NewReplacer("_", "x", "-", "y").Replace(id)
	secret := pki.RandomSecret(24)
	if tenants == nil {
		tenants = []string{}
	}
	_, err := s.db.Exec(ctx, `INSERT INTO tokens (id, kind, secret_hash, tenants, name, created_at) VALUES (?, ?, ?, ?, ?, ?)`,
		id, kind, pki.HashSecret(secret), mustJSON(tenants), name, nowMs())
	return id, formatToken(id, secret), err
}

// ensureAdminToken registers a bootstrap admin token given at startup.
func (s *Server) ensureAdminToken(ctx context.Context, tok string) error {
	id, secret, ok := parseToken(tok)
	if !ok {
		// Accept any string: derive a stable id from it.
		id, secret = "bootstrap", tok
	}
	_, err := s.db.Exec(ctx, `INSERT INTO tokens (id, kind, secret_hash, tenants, name, created_at) VALUES (?, ?, ?, '[]', 'bootstrap', ?)
		ON CONFLICT (id) DO UPDATE SET secret_hash = excluded.secret_hash, kind = excluded.kind`, id, KindAdmin, pki.HashSecret(secret), nowMs())
	return err
}

// errBadToken means the request carries no valid token. Any other error from
// authenticate means we couldn't check it (the database is unreachable), which
// is the server's problem, not the caller's.
var errBadToken = errors.New("missing or invalid API token")

func (s *Server) authenticate(r *http.Request) (*Principal, error) {
	h := r.Header.Get("Authorization")
	tok := strings.TrimSpace(strings.TrimPrefix(h, "Bearer "))
	if tok == "" || tok == h {
		// WebSocket clients that can't set headers may pass ?token=.
		tok = r.URL.Query().Get("token")
	}
	if tok == "" {
		return nil, errBadToken
	}
	id, secret, ok := parseToken(tok)
	if !ok {
		id, secret = "bootstrap", tok
	}
	var row tokenRow
	if v, ok := tokenCache.Load(id); ok && time.Since(v.(tokenRow).loaded) < 10*time.Second {
		row = v.(tokenRow)
	} else {
		var tenants string
		err := s.db.QueryRow(r.Context(), `SELECT kind, secret_hash, tenants FROM tokens WHERE id = ?`, id).Scan(&row.kind, &row.hash, &tenants)
		if isNotFound(err) {
			return nil, errBadToken
		}
		if err != nil {
			return nil, err
		}
		json.Unmarshal([]byte(tenants), &row.tenants)
		row.loaded = time.Now()
		tokenCache.Store(id, row)
	}
	if subtle.ConstantTimeCompare([]byte(row.hash), []byte(pki.HashSecret(secret))) != 1 {
		return nil, errBadToken
	}
	return &Principal{TokenID: id, Kind: row.kind, Tenants: row.tenants}, nil
}

type ctxKey int

const principalKey ctxKey = 0

func principal(r *http.Request) *Principal {
	p, _ := r.Context().Value(principalKey).(*Principal)
	return p
}

// requireAuth wraps API handlers; admin=true restricts to admin tokens.
func (s *Server) requireAuth(admin bool, h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p, err := s.authenticate(r)
		if errors.Is(err, errBadToken) {
			writeErr(w, http.StatusUnauthorized, err.Error())
			return
		}
		if err != nil {
			log.Printf("auth: checking token: %v", err)
			writeErr(w, http.StatusServiceUnavailable, "cannot verify API token right now")
			return
		}
		if admin && !p.IsAdmin() {
			writeErr(w, http.StatusForbidden, "admin token required")
			return
		}
		h(w, r.WithContext(context.WithValue(r.Context(), principalKey, p)))
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

// writeErr uses FastAPI's {"detail": ...} shape so SDK error handling is shared.
func writeErr(w http.ResponseWriter, status int, detail string) {
	writeJSON(w, status, map[string]any{"detail": detail})
}
