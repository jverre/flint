package server

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"golang.org/x/oauth2"
)

// fakeIdP is a minimal OpenID provider: discovery, JWKS, an authorize
// endpoint where the test says who signs in (?login=), and a token endpoint
// that checks PKCE and returns a signed ID token.
type fakeIdP struct {
	*httptest.Server
	key    *rsa.PrivateKey
	mu     sync.Mutex
	codes  map[string]url.Values // code -> the authorize request, plus login
	people map[string]map[string]any
}

func newFakeIdP(t *testing.T) *fakeIdP {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	f := &fakeIdP{key: key, codes: map[string]url.Values{}, people: map[string]map[string]any{
		"admin":      {"email": "admin@acme.test", "email_verified": true},
		"support":    {"email": "s@other.test", "email_verified": true, "groups": []string{"acme-support"}},
		"unverified": {"email": "admin@acme.test", "email_verified": false},
	}}
	mux := http.NewServeMux()
	f.Server = httptest.NewServer(mux)
	t.Cleanup(f.Close)
	mux.HandleFunc("/.well-known/openid-configuration", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"issuer": f.URL, "authorization_endpoint": f.URL + "/authorize",
			"token_endpoint": f.URL + "/token", "jwks_uri": f.URL + "/jwks", "response_types_supported": []string{"code"},
			"subject_types_supported": []string{"public"}, "id_token_signing_alg_values_supported": []string{"RS256"}})
	})
	mux.HandleFunc("/jwks", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, jose.JSONWebKeySet{Keys: []jose.JSONWebKey{{Key: &key.PublicKey, KeyID: "k1", Algorithm: "RS256", Use: "sig"}}})
	})
	mux.HandleFunc("/authorize", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		code := randomString()
		f.mu.Lock()
		f.codes[code] = q
		f.mu.Unlock()
		http.Redirect(w, r, q.Get("redirect_uri")+"?"+url.Values{"code": {code}, "state": {q.Get("state")}}.Encode(), http.StatusFound)
	})
	mux.HandleFunc("/token", func(w http.ResponseWriter, r *http.Request) {
		r.ParseForm()
		f.mu.Lock()
		q := f.codes[r.Form.Get("code")]
		delete(f.codes, r.Form.Get("code"))
		f.mu.Unlock()
		if q == nil || q.Get("code_challenge_method") != "S256" ||
			q.Get("code_challenge") != oauth2.S256ChallengeFromVerifier(r.Form.Get("code_verifier")) {
			writeJSON(w, 400, map[string]any{"error": "invalid_grant"})
			return
		}
		claims := map[string]any{"iss": f.URL, "aud": q.Get("client_id"), "sub": q.Get("login"), "nonce": q.Get("nonce"),
			"iat": time.Now().Unix(), "exp": time.Now().Add(time.Hour).Unix()}
		for k, v := range f.people[q.Get("login")] {
			claims[k] = v
		}
		signer, _ := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: jose.JSONWebKey{Key: key, KeyID: "k1"}},
			(&jose.SignerOptions{}).WithType("JWT"))
		payload, _ := json.Marshal(claims)
		jws, _ := signer.Sign(payload)
		idToken, _ := jws.CompactSerialize()
		writeJSON(w, 200, map[string]any{"access_token": "at", "token_type": "Bearer", "expires_in": 3600, "id_token": idToken})
	})
	return f
}

// ssoReplicas are two control-plane replicas with SSO through the fake provider.
func ssoReplicas(t *testing.T, idp *fakeIdP) []*Server {
	t.Helper()
	cfg := map[string]any{
		"public_url": "http://flint.test",
		"providers":  []map[string]any{{"id": "acme", "type": "oidc", "issuer": idp.URL, "client_id": "flint", "client_secret": "s"}},
		"rules": []map[string]any{{"email": "admin@acme.test", "kind": "admin"},
			{"group": "acme-support", "kind": "service", "tenants": []string{"acme"}}},
	}
	path := filepath.Join(t.TempDir(), "sso.json")
	data, _ := json.Marshal(cfg)
	os.WriteFile(path, data, 0o600)
	rs := replicas(t, 2)
	for _, s := range rs {
		if err := s.loadSSO(context.Background(), path); err != nil {
			t.Fatal(err)
		}
	}
	return rs
}

func do(s *Server, method, target string, cookies []*http.Cookie, hdr map[string]string, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, "http://flint.test"+target, strings.NewReader(body))
	for _, c := range cookies {
		req.AddCookie(c)
	}
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	w := httptest.NewRecorder()
	s.apiHandler().ServeHTTP(w, req)
	return w
}

// signIn runs a browser sign-in starting on replica a and coming back to replica b.
func signIn(t *testing.T, a, b *Server, login, query string) *httptest.ResponseRecorder {
	t.Helper()
	start := do(a, "GET", "/auth/login/acme"+query, nil, nil, "")
	if start.Code != 302 {
		t.Fatalf("login: %d %s", start.Code, start.Body)
	}
	noRedirect := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := noRedirect.Get(start.Header().Get("Location") + "&login=" + login)
	if err != nil {
		t.Fatal(err)
	}
	back, _ := url.Parse(resp.Header.Get("Location"))
	return do(b, "GET", back.RequestURI(), start.Result().Cookies(), nil, "")
}

func session(t *testing.T, w *httptest.ResponseRecorder) []*http.Cookie {
	for _, c := range w.Result().Cookies() {
		if c.Name == sessionCookie && c.Value != "" {
			if !c.HttpOnly || c.SameSite != http.SameSiteLaxMode {
				t.Errorf("session cookie must be HttpOnly and SameSite=Lax: %+v", c)
			}
			return []*http.Cookie{c}
		}
	}
	return nil
}

func TestSSOSignInWorksAcrossReplicas(t *testing.T) {
	idp := newFakeIdP(t)
	rs := ssoReplicas(t, idp)
	done := signIn(t, rs[0], rs[1], "admin", "?next=/ui/vms")
	if done.Code != 302 || done.Header().Get("Location") != "/ui/vms" {
		t.Fatalf("callback: %d %s %s", done.Code, done.Header().Get("Location"), done.Body)
	}
	me := do(rs[0], "GET", "/auth/me", session(t, done), nil, "")
	if !strings.Contains(me.Body.String(), `"identity":"acme:admin@acme.test"`) || !strings.Contains(me.Body.String(), `"kind":"admin"`) {
		t.Fatalf("me: %s", me.Body)
	}
	if w := do(rs[1], "GET", "/v1/hosts", session(t, done), nil, ""); w.Code != 200 {
		t.Fatalf("an admin session can't list hosts: %d %s", w.Code, w.Body)
	}
}

func TestSSOGroupRuleGivesOneTenant(t *testing.T) {
	idp := newFakeIdP(t)
	rs := ssoReplicas(t, idp)
	s := session(t, signIn(t, rs[0], rs[0], "support", ""))
	if w := do(rs[0], "GET", "/auth/me", s, nil, ""); !strings.Contains(w.Body.String(), `"tenants":["acme"]`) {
		t.Fatalf("me: %s", w.Body)
	}
	if w := do(rs[0], "GET", "/v1/hosts", s, nil, ""); w.Code != 403 {
		t.Fatalf("a service session listed hosts: %d", w.Code)
	}
}

func TestSSONoRuleNoAccess(t *testing.T) {
	idp := newFakeIdP(t)
	rs := ssoReplicas(t, idp)
	w := signIn(t, rs[0], rs[0], "unverified", "") // an unverified email proves nothing
	if w.Code != 403 || session(t, w) != nil {
		t.Fatalf("unverified email got in: %d", w.Code)
	}
}

func TestSSOSessionRefusesOtherOrigins(t *testing.T) {
	idp := newFakeIdP(t)
	rs := ssoReplicas(t, idp)
	s := session(t, signIn(t, rs[0], rs[0], "admin", ""))
	body := `{"kind":"service","name":"x"}`
	if w := do(rs[0], "POST", "/v1/tokens", s, map[string]string{"Origin": "https://evil.example"}, body); w.Code != 403 {
		t.Fatalf("cross-origin POST with a session: %d", w.Code)
	}
	if w := do(rs[0], "POST", "/v1/tokens", s, map[string]string{"Origin": "http://flint.test"}, body); w.Code != 200 {
		t.Fatalf("same-origin POST: %d %s", w.Code, w.Body)
	}
	if w := do(rs[0], "POST", "/auth/logout", s, map[string]string{"Origin": "http://flint.test"}, ""); w.Code != 200 {
		t.Fatalf("logout: %d", w.Code)
	}
	tokenCache.Range(func(k, _ any) bool { tokenCache.Delete(k); return true }) // another replica's cache expires
	if w := do(rs[1], "GET", "/auth/me", s, nil, ""); w.Code != 401 {
		t.Fatalf("session still works after logout: %d", w.Code)
	}
}

func TestSSOCLICodeIsOneTimeAndNeedsTheVerifier(t *testing.T) {
	idp := newFakeIdP(t)
	rs := ssoReplicas(t, idp)
	verifier := oauth2.GenerateVerifier()
	q := "?" + url.Values{"cli_port": {"54321"}, "cli_challenge": {oauth2.S256ChallengeFromVerifier(verifier)}}.Encode()
	w := signIn(t, rs[0], rs[0], "admin", q)
	to, _ := url.Parse(w.Header().Get("Location"))
	if to.Host != "127.0.0.1:54321" || to.Path != "/callback" {
		t.Fatalf("callback should send the browser to the CLI: %s", w.Header().Get("Location"))
	}
	code := to.Query().Get("code")
	exchange := func(s *Server, v string) *httptest.ResponseRecorder {
		b, _ := json.Marshal(map[string]string{"code": code, "verifier": v})
		return do(s, "POST", "/auth/cli/exchange", nil, nil, string(b))
	}
	if w := exchange(rs[1], "not-the-verifier-not-the-verifier-xx"); w.Code != 400 {
		t.Fatalf("wrong verifier accepted: %d", w.Code)
	}
	// A failed try spends the code: sign in again for a fresh one.
	w = signIn(t, rs[0], rs[0], "admin", q)
	to, _ = url.Parse(w.Header().Get("Location"))
	code = to.Query().Get("code")
	ok := exchange(rs[1], verifier) // redeemed on another replica
	if ok.Code != 200 {
		t.Fatalf("exchange: %d %s", ok.Code, ok.Body)
	}
	var got struct{ Token string }
	json.Unmarshal(ok.Body.Bytes(), &got)
	if w := do(rs[0], "GET", "/v1/hosts", nil, map[string]string{"Authorization": "Bearer " + got.Token}, ""); w.Code != 200 {
		t.Fatalf("CLI token doesn't work: %d", w.Code)
	}
	if w := exchange(rs[0], verifier); w.Code != 400 {
		t.Fatalf("code redeemed twice: %d", w.Code)
	}
}

func TestSSOForgedOrStaleLoginIsRefused(t *testing.T) {
	idp := newFakeIdP(t)
	rs := ssoReplicas(t, idp)
	start := do(rs[0], "GET", "/auth/login/acme", nil, nil, "")
	c := start.Result().Cookies()[0]
	c.Value = strings.Replace(c.Value, ".", "x.", 1) // tampered
	if w := do(rs[0], "GET", "/auth/callback/acme?code=x&state=y", []*http.Cookie{c}, nil, ""); w.Code != 400 {
		t.Fatalf("tampered login cookie: %d", w.Code)
	}
	if w := do(rs[0], "GET", "/auth/callback/acme?code=x&state=wrong", start.Result().Cookies(), nil, ""); w.Code != 400 {
		t.Fatalf("wrong state: %d", w.Code)
	}
	if w := signIn(t, rs[0], rs[0], "admin", "?next=//evil.example/x"); w.Header().Get("Location") != "/ui/" {
		t.Fatalf("redirected off-site: %s", w.Header().Get("Location"))
	}
}

func TestSSOConfigChecks(t *testing.T) {
	base := `"providers":[{"id":"g","type":"google","client_id":"c","client_secret":"s"}]`
	for bad, why := range map[string]string{
		`{"public_url":"https://f",` + base + `,"rules":[{"kind":"admin"}]}`:                                                                              "matches everyone",
		`{"public_url":"https://f",` + base + `,"rules":[{"email":"a@b","kind":"admin","tenants":["x"]}]}`:                                                "only service rules",
		`{"public_url":"https://f",` + base + `,"rules":[]}`:                                                                                              "nobody could sign in",
		`{` + base + `,"rules":[{"email":"a@b","kind":"admin"}]}`:                                                                                         "public_url",
		`{"public_url":"https://f","providers":[{"id":"a","type":"auth0","client_id":"c","client_secret":"s"}],"rules":[{"email":"a@b","kind":"admin"}]}`: "needs \"domain\"",
		`{"public_url":"https://f",` + base + `,"rules":[{"emial":"a@b","kind":"admin"}]}`:                                                                "unknown field",
	} {
		if _, err := parseSSOConfig([]byte(bad)); err == nil || !strings.Contains(err.Error(), why) {
			t.Errorf("%s: got %v, want %q", bad, err, why)
		}
	}
	rules := []ssoRule{{Kind: "admin", GithubTeam: "acme/platform"}, {Kind: "service", EmailDomain: "acme.com", Tenants: []string{"acme"}}}
	if r := matchRule(rules, identity{Provider: "github", GithubTeams: []string{"Acme/Platform"}}); r == nil || r.Kind != "admin" {
		t.Error("team rule")
	}
	if r := matchRule(rules, identity{Email: "x@acme.com.evil.io", EmailVerified: true}); r != nil {
		t.Error("lookalike domain matched")
	}
}
