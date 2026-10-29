package sso

import (
	"strings"
	"testing"

	"github.com/jverre/flint/cluster/internal/auth"
)

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
	rules := []ssoRule{{Kind: auth.KindAdmin, GithubTeam: "acme/platform"}, {Kind: auth.KindService, EmailDomain: "acme.com", Tenants: []string{"acme"}}}
	if r := matchRule(rules, identity{Provider: "github", GithubTeams: []string{"Acme/Platform"}}); r == nil || r.Kind != auth.KindAdmin {
		t.Error("team rule")
	}
	if r := matchRule(rules, identity{Email: "x@acme.com.evil.io", EmailVerified: true}); r != nil {
		t.Error("lookalike domain matched")
	}
}
