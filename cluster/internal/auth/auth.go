// Package auth is who may call the API: credentials (API tokens and sign-in
// sessions), the principal a request acts as, and what it may touch.
package auth

import (
	"context"
	"crypto/subtle"
	"errors"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/pki"
	"github.com/jverre/flint/cluster/internal/store"
)

// Kind is a credential's kind.
type Kind string

const (
	KindAdmin   Kind = "admin"   // operators: everything, incl. hosts and tokens
	KindService Kind = "service" // the builder's backend: VMs, templates, VM sets (optionally pinned to tenants)
)

// Principal is an authenticated API caller.
type Principal struct {
	TokenID  string
	Kind     Kind
	Tenants  []string // empty = any tenant (service tokens)
	Identity string   // "provider:email" for an SSO session
	Name     string   // the token's name
}

func (p *Principal) IsAdmin() bool { return p.Kind == KindAdmin }

// SeesAllTenants reports whether the caller is pinned to no tenants.
func (p *Principal) SeesAllTenants() bool { return p.IsAdmin() || len(p.Tenants) == 0 }

// CanTenant reports whether the caller may act on a tenant's resources.
func (p *Principal) CanTenant(t string) bool {
	if p.SeesAllTenants() {
		return true
	}
	for _, x := range p.Tenants {
		if x == t {
			return true
		}
	}
	return false
}

// ResolveTenant is the tenant a new resource belongs to: the one asked for
// if allowed, else the caller's only tenant, else "default".
func (p *Principal) ResolveTenant(requested string) (string, error) {
	if requested != "" {
		if !model.ValidTenant(requested) {
			return "", httpx.BadRequest("invalid tenant")
		}
		if !p.CanTenant(requested) {
			return "", httpx.Forbidden("this token can't act for tenant " + requested)
		}
		return requested, nil
	}
	if !p.SeesAllTenants() {
		if len(p.Tenants) == 1 {
			return p.Tenants[0], nil
		}
		return "", httpx.BadRequest("tenant is required: this token acts for " + strings.Join(p.Tenants, ", "))
	}
	return "default", nil
}

type ctxKey struct{}

// WithPrincipal is ctx acting as p.
func WithPrincipal(ctx context.Context, p *Principal) context.Context {
	return context.WithValue(ctx, ctxKey{}, p)
}

// FromContext is the principal ctx acts as, or nil (background work).
func FromContext(ctx context.Context) *Principal {
	p, _ := ctx.Value(ctxKey{}).(*Principal)
	return p
}

// ActorOf is who ctx acts as, for events and the audit log: its token (or
// sign-in session), or the control plane itself for background work.
func ActorOf(ctx context.Context) (actor, name string) {
	if p := FromContext(ctx); p != nil {
		name = p.Identity
		if name == "" {
			name = p.Name
		}
		return "token:" + p.TokenID, name
	}
	return "system", ""
}

// Tokens look like flint_<id>_<secret>; only sha256(secret) is stored.
func FormatToken(id, secret string) string { return "flint_" + id + "_" + secret }

func ParseToken(tok string) (id, secret string, ok bool) {
	rest, found := strings.CutPrefix(tok, "flint_")
	if !found {
		return "", "", false
	}
	i := strings.Index(rest, "_")
	if i <= 0 {
		return "", "", false
	}
	return rest[:i], rest[i+1:], true
}

func newTokenID() string { return strings.NewReplacer("_", "x", "-", "y").Replace(pki.RandomSecret(6)) }

// Errors from Authenticate. Any other error means the token couldn't be
// checked (the database is unreachable): the server's problem, not the caller's.
var (
	ErrBadToken    = errors.New("missing or invalid API token")
	ErrCrossOrigin = errors.New("cross-origin request refused")
)

// SessionCookie holds a browser's sign-in session token.
const SessionCookie = "flint_session"

// cacheTTL bounds how long a revoked token keeps working on other replicas.
const cacheTTL = 10 * time.Second

type cached struct {
	tok    *store.Token
	loaded time.Time
}

// Authenticator checks and issues credentials.
type Authenticator struct {
	store *store.Store
	cache sync.Map // id -> cached
}

func New(st *store.Store) *Authenticator { return &Authenticator{store: st} }

// CreateToken stores a new credential and returns its ID and string form
// (shown once).
func (a *Authenticator) CreateToken(ctx context.Context, kind Kind, name string, tenants []string) (string, string, error) {
	id, secret := newTokenID(), pki.RandomSecret(24)
	err := a.store.InsertToken(ctx, &store.Token{ID: id, Kind: string(kind), SecretHash: pki.HashSecret(secret), Tenants: tenants, Name: name})
	return id, FormatToken(id, secret), err
}

// CreateSession stores an expiring token for someone who signed in.
func (a *Authenticator) CreateSession(ctx context.Context, kind Kind, tenants []string, who string, ttl time.Duration) (id, token string, expires int64, err error) {
	now := model.NowMs()
	if err := a.store.DeleteExpiredTokens(ctx, now); err != nil {
		return "", "", 0, err
	}
	id, secret := newTokenID(), pki.RandomSecret(24)
	expires = now + ttl.Milliseconds()
	err = a.store.InsertToken(ctx, &store.Token{ID: id, Kind: string(kind), SecretHash: pki.HashSecret(secret), Tenants: tenants,
		Name: "sign-in " + who, Identity: who, CreatedAt: now, ExpiresAt: expires})
	return id, FormatToken(id, secret), expires, err
}

// EnsureAdminToken registers a bootstrap admin token given at startup.
func (a *Authenticator) EnsureAdminToken(ctx context.Context, tok string) error {
	id, secret, ok := ParseToken(tok)
	if !ok {
		// Accept any string: derive a stable id from it.
		id, secret = "bootstrap", tok
	}
	return a.store.PutBootstrapToken(ctx, id, string(KindAdmin), pki.HashSecret(secret))
}

// Revoke deletes a credential.
func (a *Authenticator) Revoke(ctx context.Context, id string) error {
	a.cache.Delete(id)
	return a.store.DeleteToken(ctx, id)
}

// ForgetCached drops cached credentials, as their TTL would.
func (a *Authenticator) ForgetCached() {
	a.cache.Range(func(k, _ any) bool { a.cache.Delete(k); return true })
}

// Authenticate finds the request's credential: a bearer token, a ?token= on
// WebSockets (clients that can't set headers), or a browser session cookie.
func (a *Authenticator) Authenticate(r *http.Request) (*Principal, error) {
	h := r.Header.Get("Authorization")
	tok := strings.TrimSpace(strings.TrimPrefix(h, "Bearer "))
	if tok == "" || tok == h {
		tok = ""
		if isWebSocket(r) {
			tok = r.URL.Query().Get("token")
		}
	}
	if tok == "" {
		if c, err := r.Cookie(SessionCookie); err == nil && c.Value != "" {
			// A browser session: refuse what another site's page could make the browser send.
			unsafe := isWebSocket(r) || (r.Method != http.MethodGet && r.Method != http.MethodHead && r.Method != http.MethodOptions)
			if unsafe && !SameOrigin(r) {
				return nil, ErrCrossOrigin
			}
			tok = c.Value
		}
	}
	return a.AuthenticateToken(r.Context(), tok)
}

func isWebSocket(r *http.Request) bool {
	return strings.EqualFold(r.Header.Get("Upgrade"), "websocket")
}

// AuthenticateToken checks an API token (or SSO session token).
func (a *Authenticator) AuthenticateToken(ctx context.Context, tok string) (*Principal, error) {
	if tok == "" {
		return nil, ErrBadToken
	}
	id, secret, ok := ParseToken(tok)
	if !ok {
		id, secret = "bootstrap", tok
	}
	var row *store.Token
	if v, ok := a.cache.Load(id); ok && time.Since(v.(cached).loaded) < cacheTTL {
		row = v.(cached).tok
	} else {
		t, err := a.store.GetToken(ctx, id)
		if store.IsNotFound(err) {
			return nil, ErrBadToken
		}
		if err != nil {
			return nil, err
		}
		row = t
		a.cache.Store(id, cached{t, time.Now()})
	}
	if subtle.ConstantTimeCompare([]byte(row.SecretHash), []byte(pki.HashSecret(secret))) != 1 {
		return nil, ErrBadToken
	}
	if row.ExpiresAt != 0 && row.ExpiresAt < model.NowMs() {
		return nil, ErrBadToken
	}
	return &Principal{TokenID: id, Kind: Kind(row.Kind), Tenants: row.Tenants, Identity: row.Identity, Name: row.Name}, nil
}

// SameOrigin: the browser says the request comes from a page of this server.
// Browsers always send Origin on WebSockets and on requests that change
// something; comparing hosts (not schemes) works behind a TLS-terminating LB.
func SameOrigin(r *http.Request) bool {
	o := r.Header.Get("Origin")
	if o == "" {
		return false
	}
	u, err := url.Parse(o)
	return err == nil && strings.EqualFold(u.Host, r.Host)
}

// CallerToken is the request's bearer token, else its session cookie.
func CallerToken(r *http.Request) string {
	if h := r.Header.Get("Authorization"); strings.HasPrefix(h, "Bearer ") {
		return strings.TrimSpace(strings.TrimPrefix(h, "Bearer "))
	}
	if c, err := r.Cookie(SessionCookie); err == nil {
		return c.Value
	}
	return ""
}
