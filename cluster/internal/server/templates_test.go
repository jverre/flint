package server

import (
	"context"
	"testing"
)

// Template IDs are one namespace across tenants; ownership must still hold.

func addTemplate(t *testing.T, s *Server, id, tenant, dockerfile string) {
	t.Helper()
	if _, err := s.db.Exec(context.Background(), `INSERT INTO templates (id, name, tenant, dockerfile, rootfs_mb, status, build_host, error, created_at, updated_at)
		VALUES (?, ?, ?, ?, 500, 'ready', NULL, '', 1, 1)`, id, id, tenant, dockerfile); err != nil {
		t.Fatal(err)
	}
}

func bearer(token string) map[string]string {
	return map[string]string{"Authorization": "Bearer " + token}
}

func TestAnotherTenantCannotRebuildATemplate(t *testing.T) {
	s := replicas(t, 1)[0]
	ctx := context.Background()
	addTemplate(t, s, "app", "acme", "FROM alpine")
	addTemplate(t, s, "base", "", "FROM alpine")
	_, globex, _ := s.createToken(ctx, KindService, "globex", []string{"globex"})
	_, acme, _ := s.createToken(ctx, KindService, "acme", []string{"acme"})
	for _, c := range []struct {
		name, token string
		want        int
	}{
		{"app", globex, 403},  // another tenant's: not even visible
		{"base", globex, 409}, // a shared one: visible, but not theirs to replace
		{"base", acme, 409},
	} {
		w := do(s, "POST", "/templates/build", nil, bearer(c.token), `{"name":"`+c.name+`","dockerfile":"FROM evil"}`)
		if w.Code != c.want {
			t.Fatalf("rebuilding %s: want %d, got %d %s", c.name, c.want, w.Code, w.Body)
		}
	}
	for _, id := range []string{"app", "base"} {
		if tpl, _ := s.getTemplate(ctx, id); tpl.Dockerfile != "FROM alpine" {
			t.Fatalf("%s was rewritten: %q", id, tpl.Dockerfile)
		}
	}
}

func TestATokensBuildBelongsToItsTenant(t *testing.T) {
	s := replicas(t, 1)[0]
	_, acme, _ := s.createToken(context.Background(), KindService, "acme", []string{"acme"})
	// No host to build on (503), but the template was claimed for acme, not shared.
	do(s, "POST", "/templates/build", nil, bearer(acme), `{"name":"mine","dockerfile":"FROM alpine"}`)
	if tpl, err := s.getTemplate(context.Background(), "mine"); err == nil && tpl.Tenant != "acme" {
		t.Fatalf("template tenant %q, want acme", tpl.Tenant)
	}
	_, both, _ := s.createToken(context.Background(), KindService, "both", []string{"acme", "globex"})
	if w := do(s, "POST", "/templates/build", nil, bearer(both), `{"name":"which","dockerfile":"FROM alpine"}`); w.Code != 400 {
		t.Fatalf("a two-tenant token must say which: %d %s", w.Code, w.Body)
	}
}

func TestVMsOnlyBootTheirTenantsTemplates(t *testing.T) {
	s := replicas(t, 1)[0]
	ctx := context.Background()
	addTemplate(t, s, "acme-app", "acme", "FROM alpine")
	_, globex, _ := s.createToken(ctx, KindService, "globex", []string{"globex"})
	_, admin, _ := s.createToken(ctx, KindAdmin, "ops", nil)
	for _, token := range []string{globex, admin} { // not even an admin can mix tenants
		if w := do(s, "POST", "/vms", nil, bearer(token), `{"tenant":"globex","template_id":"acme-app"}`); w.Code != 404 {
			t.Fatalf("VM from another tenant's template: %d %s", w.Code, w.Body)
		}
		if w := do(s, "POST", "/v1/vmsets", nil, bearer(token),
			`{"name":"web","tenant":"globex","spec":{"replicas":1,"template_id":"acme-app"}}`); w.Code != 404 {
			t.Fatalf("VM set from another tenant's template: %d %s", w.Code, w.Body)
		}
	}
	// A VM set saved with its own template keeps booting only that tenant's:
	// if the ID later belongs to someone else, its members are refused.
	v := &VM{ID: "vm-1", Tenant: "globex", State: VMPending, TemplateID: "acme-app"}
	if _, _, err := s.placeAndCreate(ctx, v, placeReq{}, nil); err == nil {
		t.Fatal("placed a globex VM on acme's template")
	} else if code, _ := statusFor(err); code != 404 {
		t.Fatalf("want 404, got %v", err)
	}
}

func TestOnlyAnAdminDeletesASharedTemplate(t *testing.T) {
	s := replicas(t, 1)[0]
	ctx := context.Background()
	addTemplate(t, s, "base", "", "FROM alpine")
	_, acme, _ := s.createToken(ctx, KindService, "acme", []string{"acme"})
	if w := do(s, "DELETE", "/templates/base", nil, bearer(acme), ""); w.Code != 403 {
		t.Fatalf("a tenant deleted a shared template: %d %s", w.Code, w.Body)
	}
	_, admin, _ := s.createToken(ctx, KindAdmin, "ops", nil)
	if w := do(s, "DELETE", "/templates/base", nil, bearer(admin), ""); w.Code != 200 {
		t.Fatalf("admin delete: %d %s", w.Code, w.Body)
	}
}
