package server

// Sign-in with identity providers (SSO) for the people who run Flint: the
// web UI and the CLI. Same config file and HTTP contract as the single
// daemon (src/flint/daemon/sso.py; docs/sso.mdx): providers, and rules that
// map people to admin or tenant-pinned service access. Authorization code
// flow with PKCE, state and nonce (golang.org/x/oauth2); ID tokens are
// verified with github.com/coreos/go-oidc.
//
// Replicas are stateless: the sign-in state rides in a cookie signed with a
// key kept in the database, a session is a token row, and the CLI's
// one-time codes are rows any replica can redeem.

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"log"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/jverre/flint/cluster/internal/pki"
	"golang.org/x/oauth2"
)

const (
	sessionCookie = "flint_session"
	loginCookie   = "flint_login"
	loginTTL      = 10 * time.Minute
	cliCodeTTL    = 2 * time.Minute
)

type ssoProviderConfig struct {
	ID              string   `json:"id"`
	Type            string   `json:"type"`
	Label           string   `json:"label"`
	ClientID        string   `json:"client_id"`
	ClientSecret    string   `json:"client_secret"`
	ClientSecretEnv string   `json:"client_secret_env"`
	Issuer          string   `json:"issuer"`
	Tenant          string   `json:"tenant"`
	Domain          string   `json:"domain"`
	URL             string   `json:"url"`
	APIURL          string   `json:"api_url"`
	Scopes          []string `json:"scopes"`
	GroupsClaim     string   `json:"groups_claim"`
}

type ssoRule struct {
	Kind        string   `json:"kind"`
	Tenants     []string `json:"tenants"`
	Provider    string   `json:"provider"`
	Email       string   `json:"email"`
	EmailDomain string   `json:"email_domain"`
	Group       string   `json:"group"`
	GithubOrg   string   `json:"github_org"`
	GithubTeam  string   `json:"github_team"`
}

type ssoConfig struct {
	PublicURL    string              `json:"public_url"`
	SessionHours float64             `json:"session_hours"`
	Providers    []ssoProviderConfig `json:"providers"`
	Rules        []ssoRule           `json:"rules"`
}

// identity is who signed in, as far as the rules are concerned.
type identity struct {
	Provider, Subject, Email, Login string
	EmailVerified                   bool
	Groups, GithubOrgs, GithubTeams []string
}

func (i identity) name() string {
	for _, v := range []string{i.Email, i.Login, i.Subject} {
		if v != "" {
			return i.Provider + ":" + v
		}
	}
	return i.Provider
}

var (
	ssoIDRE     = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
	ssoTenantRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$`)
	ssoLabels   = map[string]string{"google": "Google", "microsoft": "Microsoft", "auth0": "Auth0", "okta": "Okta",
		"gitlab": "GitLab", "github": "GitHub", "oidc": "SSO"}
)

func (p *ssoProviderConfig) issuer() (string, error) {
	need := func(v, key string) (string, error) {
		if v == "" {
			return "", fmt.Errorf("provider %q (%s) needs %q", p.ID, p.Type, key)
		}
		return v, nil
	}
	switch p.Type {
	case "google":
		return "https://accounts.google.com", nil
	case "microsoft":
		t, err := need(p.Tenant, "tenant")
		return "https://login.microsoftonline.com/" + t + "/v2.0", err
	case "auth0":
		d, err := need(p.Domain, "domain")
		return "https://" + d + "/", err
	case "okta":
		d, err := need(p.Domain, "domain")
		return "https://" + d, err
	case "gitlab":
		if p.URL != "" {
			return p.URL, nil
		}
		return "https://gitlab.com", nil
	case "oidc":
		return need(p.Issuer, "issuer")
	}
	return "", fmt.Errorf("provider %q: unknown type %q; use one of oidc, google, microsoft, auth0, okta, gitlab, github", p.ID, p.Type)
}

// parseSSOConfig checks a config the way the daemon does, so one file works for both.
func parseSSOConfig(data []byte) (*ssoConfig, error) {
	var c ssoConfig
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		return nil, fmt.Errorf("SSO config: %w", err)
	}
	seen := map[string]bool{}
	for i := range c.Providers {
		p := &c.Providers[i]
		if p.ID == "" {
			p.ID = p.Type
		}
		if !ssoIDRE.MatchString(p.ID) {
			return nil, fmt.Errorf("provider id %q must be letters, digits, '-' or '_'", p.ID)
		}
		if seen[p.ID] {
			return nil, fmt.Errorf("provider %q is defined twice", p.ID)
		}
		seen[p.ID] = true
		if p.ClientSecret == "" && p.ClientSecretEnv != "" {
			p.ClientSecret = os.Getenv(p.ClientSecretEnv)
		}
		if p.ClientID == "" || p.ClientSecret == "" {
			return nil, fmt.Errorf("provider %q needs client_id and client_secret (or client_secret_env, set)", p.ID)
		}
		if p.Label == "" {
			p.Label = ssoLabels[p.Type]
		}
		if p.GroupsClaim == "" {
			p.GroupsClaim = "groups"
		}
		if p.Type == "github" {
			if p.URL == "" {
				p.URL = "https://github.com"
				if p.APIURL == "" {
					p.APIURL = "https://api.github.com"
				}
			}
			if p.APIURL == "" {
				p.APIURL = strings.TrimRight(p.URL, "/") + "/api/v3"
			}
			p.URL, p.APIURL = strings.TrimRight(p.URL, "/"), strings.TrimRight(p.APIURL, "/")
			if len(p.Scopes) == 0 {
				p.Scopes = []string{"read:user", "user:email", "read:org"}
			}
			continue
		}
		iss, err := p.issuer()
		if err != nil {
			return nil, err
		}
		p.Issuer = iss
		if len(p.Scopes) == 0 {
			p.Scopes = []string{oidc.ScopeOpenID, "email", "profile"}
		}
		hasOpenID := false
		for _, s := range p.Scopes {
			hasOpenID = hasOpenID || s == oidc.ScopeOpenID
		}
		if !hasOpenID {
			return nil, fmt.Errorf("provider %q: scopes must include 'openid'", p.ID)
		}
	}
	for i, r := range c.Rules {
		n := i + 1
		if r.Kind != KindAdmin && r.Kind != KindService {
			return nil, fmt.Errorf("rule %d: 'kind' must be 'admin' or 'service'", n)
		}
		if r.Kind == KindAdmin && len(r.Tenants) > 0 {
			return nil, fmt.Errorf("rule %d: admins act for every tenant; only service rules take 'tenants'", n)
		}
		if r.Email == "" && r.EmailDomain == "" && r.Group == "" && r.GithubOrg == "" && r.GithubTeam == "" {
			return nil, fmt.Errorf("rule %d matches everyone; give it an email, email_domain, group, github_org or github_team", n)
		}
		if r.Provider != "" && !seen[r.Provider] {
			return nil, fmt.Errorf("rule %d: no provider %q", n, r.Provider)
		}
		for _, t := range r.Tenants {
			if !ssoTenantRE.MatchString(t) {
				return nil, fmt.Errorf("rule %d: invalid tenant %q", n, t)
			}
		}
	}
	if len(c.Providers) > 0 && len(c.Rules) == 0 {
		return nil, errors.New("there are providers but no rules, so nobody could sign in")
	}
	if len(c.Providers) > 0 && c.PublicURL == "" {
		return nil, errors.New("set public_url: the address people reach the control plane on")
	}
	c.PublicURL = strings.TrimRight(c.PublicURL, "/")
	if c.SessionHours <= 0 {
		c.SessionHours = 12
	}
	return &c, nil
}

func containsFold(list []string, v string) bool {
	for _, x := range list {
		if strings.EqualFold(x, v) {
			return true
		}
	}
	return false
}

// matchRule returns the first rule id meets: every condition it has must hold.
func matchRule(rules []ssoRule, id identity) *ssoRule {
	email := ""
	if id.EmailVerified {
		email = strings.ToLower(id.Email)
	}
	for i := range rules {
		r := &rules[i]
		switch {
		case r.Provider != "" && r.Provider != id.Provider,
			r.Email != "" && strings.ToLower(r.Email) != email,
			r.EmailDomain != "" && (email == "" || email[strings.LastIndex(email, "@")+1:] != strings.ToLower(strings.TrimPrefix(r.EmailDomain, "@"))),
			r.Group != "" && !contains(id.Groups, r.Group),
			r.GithubOrg != "" && !containsFold(id.GithubOrgs, r.GithubOrg),
			r.GithubTeam != "" && !containsFold(id.GithubTeams, r.GithubTeam):
			continue
		}
		return r
	}
	return nil
}

// ── Runtime ──────────────────────────────────────────────────────────────

type ssoProvider struct {
	cfg      ssoProviderConfig
	mu       sync.Mutex
	oauth    *oauth2.Config
	verifier *oidc.IDTokenVerifier
}

type ssoRuntime struct {
	cfg       *ssoConfig
	providers map[string]*ssoProvider
	key       []byte // signs the login cookie; shared by every replica
	http      *http.Client
}

// loadSSO reads the config at path ("" or missing file: no SSO).
func (s *Server) loadSSO(ctx context.Context, path string) error {
	if path == "" {
		return nil
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	cfg, err := parseSSOConfig(data)
	if err != nil || len(cfg.Providers) == 0 {
		return err
	}
	key, err := s.db.SetMetaIfAbsent(ctx, "sso_cookie_key", pki.RandomSecret(32))
	if err != nil {
		return err
	}
	rt := &ssoRuntime{cfg: cfg, providers: map[string]*ssoProvider{}, key: []byte(key),
		http: &http.Client{Timeout: 15 * time.Second}}
	for _, p := range cfg.Providers {
		rt.providers[p.ID] = &ssoProvider{cfg: p}
		log.Printf("SSO: %s (%s); its redirect URI is %s", p.Label, p.ID, rt.redirectURI(p.ID))
	}
	s.sso = rt
	return nil
}

func (rt *ssoRuntime) redirectURI(pid string) string {
	return rt.cfg.PublicURL + "/auth/callback/" + pid
}

func (rt *ssoRuntime) secure() bool { return strings.HasPrefix(rt.cfg.PublicURL, "https://") }

// client sets up a provider on first use (OIDC discovery), so the control
// plane starts even when a provider is unreachable.
func (rt *ssoRuntime) client(ctx context.Context, pid string) (*ssoProvider, error) {
	p := rt.providers[pid]
	if p == nil {
		return nil, fmt.Errorf("no provider %q", pid)
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.oauth != nil {
		return p, nil
	}
	cfg := &oauth2.Config{ClientID: p.cfg.ClientID, ClientSecret: p.cfg.ClientSecret,
		RedirectURL: rt.redirectURI(pid), Scopes: p.cfg.Scopes}
	if p.cfg.Type == "github" {
		cfg.Endpoint = oauth2.Endpoint{AuthURL: p.cfg.URL + "/login/oauth/authorize", TokenURL: p.cfg.URL + "/login/oauth/access_token"}
	} else {
		provider, err := oidc.NewProvider(oidc.ClientContext(ctx, rt.http), p.cfg.Issuer)
		if err != nil {
			return nil, fmt.Errorf("%s: discovery: %w", p.cfg.Label, err)
		}
		cfg.Endpoint = provider.Endpoint()
		p.verifier = provider.Verifier(&oidc.Config{ClientID: p.cfg.ClientID})
	}
	p.oauth = cfg
	return p, nil
}

// loginState rides in the signed flint_login cookie between login and callback.
type loginState struct {
	Provider     string `json:"p"`
	State        string `json:"s"`
	Nonce        string `json:"n"`
	Verifier     string `json:"v"`
	Next         string `json:"x"`
	CLIPort      int    `json:"cp,omitempty"`
	CLIChallenge string `json:"cc,omitempty"`
	Expires      int64  `json:"e"`
}

func (rt *ssoRuntime) sign(v loginState) string {
	data, _ := json.Marshal(v)
	payload := base64.RawURLEncoding.EncodeToString(data)
	m := hmac.New(sha256.New, rt.key)
	m.Write([]byte(payload))
	return payload + "." + base64.RawURLEncoding.EncodeToString(m.Sum(nil))
}

func (rt *ssoRuntime) open(cookie string) (loginState, bool) {
	var v loginState
	payload, mac, ok := strings.Cut(cookie, ".")
	if !ok {
		return v, false
	}
	m := hmac.New(sha256.New, rt.key)
	m.Write([]byte(payload))
	want := base64.RawURLEncoding.EncodeToString(m.Sum(nil))
	if subtle.ConstantTimeCompare([]byte(mac), []byte(want)) != 1 {
		return v, false
	}
	data, err := base64.RawURLEncoding.DecodeString(payload)
	if err != nil || json.Unmarshal(data, &v) != nil || v.Expires < time.Now().Unix() {
		return v, false
	}
	return v, true
}

func randomString() string {
	b := make([]byte, 24)
	rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

// safeNext keeps the post-sign-in redirect on this server.
func safeNext(p string) string {
	if p == "" || !strings.HasPrefix(p, "/") || strings.HasPrefix(p, "//") || strings.Contains(p, `\`) {
		return "/ui/"
	}
	return p
}

func ssoPage(w http.ResponseWriter, status int, title, text string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	fmt.Fprintf(w, `<!doctype html><meta charset="utf-8"><title>%s</title>`+
		`<body style="font:14px system-ui;background:#151922;color:#eef1f7;display:grid;place-items:center;height:100vh;margin:0">`+
		`<div style="max-width:420px"><h1 style="font-size:18px">%s</h1><p>%s</p>`+
		`<p><a style="color:#9cc1ff" href="/ui/">Back to Flint</a></p></div>`, html.EscapeString(title), html.EscapeString(title), html.EscapeString(text))
}

func (s *Server) handleSSOProviders(w http.ResponseWriter, r *http.Request) {
	out := []map[string]string{}
	if s.sso != nil {
		for _, p := range s.sso.cfg.Providers {
			out = append(out, map[string]string{"id": p.ID, "label": p.Label, "type": p.Type})
		}
	}
	writeJSON(w, 200, map[string]any{"providers": out})
}

func (s *Server) handleSSOLogin(w http.ResponseWriter, r *http.Request) {
	if s.sso == nil {
		writeErr(w, 404, "SSO isn't configured")
		return
	}
	pid := r.PathValue("id")
	p, err := s.sso.client(r.Context(), pid)
	if err != nil {
		writeErr(w, 404, err.Error())
		return
	}
	q := r.URL.Query()
	st := loginState{Provider: pid, State: randomString(), Nonce: randomString(), Verifier: oauth2.GenerateVerifier(),
		Next: safeNext(q.Get("next")), Expires: time.Now().Add(loginTTL).Unix()}
	if port := q.Get("cli_port"); port != "" {
		fmt.Sscanf(port, "%d", &st.CLIPort)
		st.CLIChallenge = q.Get("cli_challenge")
		if st.CLIPort < 1024 || st.CLIPort > 65535 || len(st.CLIChallenge) < 20 || len(st.CLIChallenge) > 128 {
			writeErr(w, 400, "Bad cli_port or cli_challenge")
			return
		}
	}
	http.SetCookie(w, &http.Cookie{Name: loginCookie, Value: s.sso.sign(st), Path: "/auth/", MaxAge: int(loginTTL.Seconds()),
		HttpOnly: true, Secure: s.sso.secure(), SameSite: http.SameSiteLaxMode})
	opts := []oauth2.AuthCodeOption{oauth2.S256ChallengeOption(st.Verifier)}
	if p.cfg.Type != "github" {
		opts = append(opts, oidc.Nonce(st.Nonce))
	}
	http.Redirect(w, r, p.oauth.AuthCodeURL(st.State, opts...), http.StatusFound)
}

func (s *Server) handleSSOCallback(w http.ResponseWriter, r *http.Request) {
	if s.sso == nil {
		writeErr(w, 404, "SSO isn't configured")
		return
	}
	pid := r.PathValue("id")
	c, err := r.Cookie(loginCookie)
	st, ok := loginState{}, false
	if err == nil {
		st, ok = s.sso.open(c.Value)
	}
	http.SetCookie(w, &http.Cookie{Name: loginCookie, Path: "/auth/", MaxAge: -1})
	if !ok || st.Provider != pid || subtle.ConstantTimeCompare([]byte(st.State), []byte(r.URL.Query().Get("state"))) != 1 {
		ssoPage(w, 400, "Sign-in expired", "Start again from Flint.")
		return
	}
	p, err := s.sso.client(r.Context(), pid)
	if err != nil {
		ssoPage(w, 502, "Sign-in failed", err.Error())
		return
	}
	if e := r.URL.Query().Get("error"); e != "" {
		ssoPage(w, 400, "Sign-in failed", p.cfg.Label+" said: "+firstNonEmpty(r.URL.Query().Get("error_description"), e))
		return
	}
	ctx := oidc.ClientContext(r.Context(), s.sso.http)
	tok, err := p.oauth.Exchange(ctx, r.URL.Query().Get("code"), oauth2.VerifierOption(st.Verifier))
	if err != nil {
		log.Printf("SSO %s: code exchange: %v", pid, err)
		ssoPage(w, 400, "Sign-in failed", p.cfg.Label+" refused the sign-in.")
		return
	}
	var who identity
	if p.cfg.Type == "github" {
		who, err = githubIdentity(ctx, s.sso.http, p.cfg, tok)
	} else {
		who, err = oidcIdentity(ctx, p, tok, st.Nonce)
	}
	if err != nil {
		log.Printf("SSO %s: %v", pid, err)
		ssoPage(w, 502, "Sign-in failed", "Couldn't check who you are with "+p.cfg.Label+".")
		return
	}
	rule := matchRule(s.sso.cfg.Rules, who)
	if rule == nil {
		log.Printf("SSO %s: %s has no access (no rule matches)", pid, who.name())
		shown := firstNonEmpty(who.Email, who.Login, who.Subject)
		ssoPage(w, 403, "No access", shown+" isn't allowed to use this Flint. Ask its admin to add a rule for you.")
		return
	}
	ttl := time.Duration(s.sso.cfg.SessionHours * float64(time.Hour))
	log.Printf("SSO %s: %s signed in as %s %v", pid, who.name(), rule.Kind, rule.Tenants)
	if st.CLIPort != 0 {
		code := randomString()
		grant, _ := json.Marshal(map[string]any{"kind": rule.Kind, "tenants": nonNil(rule.Tenants), "identity": who.name(),
			"ttl_ms": ttl.Milliseconds()})
		if _, err := s.db.Exec(r.Context(), `DELETE FROM sso_codes WHERE expires_at < ?`, nowMs()); err != nil {
			log.Printf("SSO: pruning codes: %v", err)
		}
		if _, err := s.db.Exec(r.Context(), `INSERT INTO sso_codes (code_hash, grant_json, challenge, expires_at) VALUES (?, ?, ?, ?)`,
			pki.HashSecret(code), string(grant), st.CLIChallenge, nowMs()+cliCodeTTL.Milliseconds()); err != nil {
			ssoPage(w, 500, "Sign-in failed", err.Error())
			return
		}
		http.Redirect(w, r, fmt.Sprintf("http://127.0.0.1:%d/callback?%s", st.CLIPort, url.Values{"code": {code}}.Encode()), http.StatusFound)
		return
	}
	_, token, expires, err := s.createSession(r.Context(), rule.Kind, rule.Tenants, who.name(), ttl)
	if err != nil {
		ssoPage(w, 500, "Sign-in failed", err.Error())
		return
	}
	http.SetCookie(w, &http.Cookie{Name: sessionCookie, Value: token, Path: "/", Expires: time.UnixMilli(expires),
		HttpOnly: true, Secure: s.sso.secure(), SameSite: http.SameSiteLaxMode})
	http.Redirect(w, r, st.Next, http.StatusFound)
}

func nonNil(v []string) []string {
	if v == nil {
		return []string{}
	}
	return v
}

func oidcIdentity(ctx context.Context, p *ssoProvider, tok *oauth2.Token, nonce string) (identity, error) {
	raw, _ := tok.Extra("id_token").(string)
	if raw == "" {
		return identity{}, errors.New("no ID token in the token response")
	}
	idt, err := p.verifier.Verify(ctx, raw)
	if err != nil {
		return identity{}, fmt.Errorf("ID token: %w", err)
	}
	if subtle.ConstantTimeCompare([]byte(idt.Nonce), []byte(nonce)) != 1 {
		return identity{}, errors.New("ID token: nonce mismatch")
	}
	var claims map[string]any
	if err := idt.Claims(&claims); err != nil {
		return identity{}, err
	}
	str := func(k string) string { v, _ := claims[k].(string); return v }
	who := identity{Provider: p.cfg.ID, Subject: idt.Subject, Email: str("email"), Login: str("preferred_username")}
	switch v := claims["email_verified"].(type) {
	case bool:
		who.EmailVerified = v
	case string:
		who.EmailVerified = v == "true"
	}
	switch g := claims[p.cfg.GroupsClaim].(type) {
	case []any:
		for _, x := range g {
			who.Groups = append(who.Groups, fmt.Sprint(x))
		}
	case string:
		who.Groups = []string{g}
	}
	return who, nil
}

// githubIdentity reads the person, their verified primary email, orgs and teams from GitHub's API.
func githubIdentity(ctx context.Context, hc *http.Client, p ssoProviderConfig, tok *oauth2.Token) (identity, error) {
	get := func(path string, v any) error {
		req, _ := http.NewRequestWithContext(ctx, "GET", p.APIURL+path, nil)
		req.Header.Set("Authorization", "Bearer "+tok.AccessToken)
		req.Header.Set("Accept", "application/vnd.github+json")
		resp, err := hc.Do(req)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		if resp.StatusCode != 200 {
			return fmt.Errorf("GitHub %s: %s", path, resp.Status)
		}
		return json.NewDecoder(resp.Body).Decode(v)
	}
	var user struct {
		ID    int64  `json:"id"`
		Login string `json:"login"`
	}
	var emails []struct {
		Email             string `json:"email"`
		Primary, Verified bool
	}
	var orgs []struct {
		Login string `json:"login"`
	}
	var teams []struct {
		Slug string `json:"slug"`
		Org  struct {
			Login string `json:"login"`
		} `json:"organization"`
	}
	for _, c := range []struct {
		path string
		v    any
	}{{"/user", &user}, {"/user/emails", &emails}, {"/user/orgs?per_page=100", &orgs}, {"/user/teams?per_page=100", &teams}} {
		if err := get(c.path, c.v); err != nil {
			return identity{}, err
		}
	}
	who := identity{Provider: p.ID, Subject: fmt.Sprint(user.ID), Login: user.Login}
	for _, e := range emails {
		if e.Primary && e.Verified {
			who.Email, who.EmailVerified = e.Email, true
		}
	}
	for _, o := range orgs {
		who.GithubOrgs = append(who.GithubOrgs, o.Login)
	}
	for _, t := range teams {
		who.GithubTeams = append(who.GithubTeams, t.Org.Login+"/"+t.Slug)
	}
	return who, nil
}

func (s *Server) handleSSOCLIExchange(w http.ResponseWriter, r *http.Request) {
	var body struct{ Code, Verifier string }
	if json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&body) != nil || body.Code == "" {
		writeErr(w, 400, "Invalid or expired sign-in code")
		return
	}
	var grantJSON, challenge string
	var expires int64
	h := pki.HashSecret(body.Code)
	err := s.db.QueryRow(r.Context(), `SELECT grant_json, challenge, expires_at FROM sso_codes WHERE code_hash = ?`, h).
		Scan(&grantJSON, &challenge, &expires)
	if err != nil {
		writeErr(w, 400, "Invalid or expired sign-in code")
		return
	}
	// One try per code, whoever wins the race to delete it.
	res, err := s.db.Exec(r.Context(), `DELETE FROM sso_codes WHERE code_hash = ?`, h)
	if n, _ := res.RowsAffected(); err != nil || n != 1 || expires < nowMs() ||
		subtle.ConstantTimeCompare([]byte(challenge), []byte(oauth2.S256ChallengeFromVerifier(body.Verifier))) != 1 {
		writeErr(w, 400, "Invalid or expired sign-in code")
		return
	}
	var grant struct {
		Kind     string   `json:"kind"`
		Tenants  []string `json:"tenants"`
		Identity string   `json:"identity"`
		TTLms    int64    `json:"ttl_ms"`
	}
	json.Unmarshal([]byte(grantJSON), &grant)
	_, token, exp, err := s.createSession(r.Context(), grant.Kind, grant.Tenants, grant.Identity, time.Duration(grant.TTLms)*time.Millisecond)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"token": token, "kind": grant.Kind, "tenants": nonNil(grant.Tenants),
		"identity": grant.Identity, "expires_at": float64(exp) / 1000})
}

// callerToken is the request's bearer token, else its session cookie.
func callerToken(r *http.Request) string {
	if h := r.Header.Get("Authorization"); strings.HasPrefix(h, "Bearer ") {
		return strings.TrimSpace(strings.TrimPrefix(h, "Bearer "))
	}
	if c, err := r.Cookie(sessionCookie); err == nil {
		return c.Value
	}
	return ""
}

func (s *Server) handleSSOMe(w http.ResponseWriter, r *http.Request) {
	p, err := s.authenticateToken(r.Context(), callerToken(r))
	if err != nil {
		writeErr(w, 401, "Not signed in")
		return
	}
	writeJSON(w, 200, map[string]any{"kind": p.Kind, "tenants": nonNil(p.Tenants), "identity": p.Identity, "token_id": p.TokenID})
}

func (s *Server) handleSSOLogout(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Authorization") == "" && !sameOrigin(r) {
		writeErr(w, 403, "Cross-origin request refused")
		return
	}
	if p, err := s.authenticateToken(r.Context(), callerToken(r)); err == nil && p.Identity != "" {
		s.db.Exec(r.Context(), `DELETE FROM tokens WHERE id = ?`, p.TokenID)
		tokenCache.Delete(p.TokenID)
	}
	http.SetCookie(w, &http.Cookie{Name: sessionCookie, Path: "/", MaxAge: -1})
	writeJSON(w, 200, map[string]any{"ok": true})
}

// sameOrigin: the browser says the request comes from a page of this server.
// Browsers always send Origin on WebSockets and on requests that change
// something; comparing hosts (not schemes) works behind a TLS-terminating LB.
func sameOrigin(r *http.Request) bool {
	o := r.Header.Get("Origin")
	if o == "" {
		return false
	}
	u, err := url.Parse(o)
	return err == nil && strings.EqualFold(u.Host, r.Host)
}

// createSession stores an expiring token for someone who signed in.
func (s *Server) createSession(ctx context.Context, kind string, tenants []string, who string, ttl time.Duration) (string, string, int64, error) {
	id := strings.NewReplacer("_", "x", "-", "y").Replace(pki.RandomSecret(6))
	secret := pki.RandomSecret(24)
	exp := nowMs() + ttl.Milliseconds()
	if _, err := s.db.Exec(ctx, `DELETE FROM tokens WHERE expires_at IS NOT NULL AND expires_at < ?`, nowMs()); err != nil {
		return "", "", 0, err
	}
	_, err := s.db.Exec(ctx, `INSERT INTO tokens (id, kind, secret_hash, tenants, name, created_at, expires_at, identity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		id, kind, pki.HashSecret(secret), mustJSON(nonNil(tenants)), "sign-in "+who, nowMs(), exp, who)
	return id, formatToken(id, secret), exp, err
}
