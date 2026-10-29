package server

import (
	"context"
	"testing"

	"github.com/jverre/flint/cluster/internal/auth"
	"github.com/jverre/flint/cluster/internal/db"
	"github.com/jverre/flint/cluster/internal/model"
)

// Template IDs are one namespace across tenants; ownership must still hold.

func addTemplate(t *testing.T, d *db.DB, id, tenant, dockerfile string, status model.TemplateStatus) {
	t.Helper()
	if _, err := d.Exec(context.Background(), `INSERT INTO templates (id, name, tenant, dockerfile, rootfs_mb, status, build_host, error, created_at, updated_at)
		VALUES (?, ?, ?, ?, 500, ?, NULL, '', 1, 1)`, id, id, tenant, dockerfile, string(status)); err != nil {
		t.Fatal(err)
	}
}

func bearer(token string) map[string]string {
	return map[string]string{"Authorization": "Bearer " + token}
}

func TestAnotherTenantCannotRebuildATemplate(t *testing.T) {
	rs, d := replicasDB(t, 1)
	s := rs[0]
	ctx := context.Background()
	addTemplate(t, d, "app", "acme", "FROM alpine", model.TemplateReady)
	addTemplate(t, d, "base", "", "FROM alpine", model.TemplateReady)
	_, globex, _ := s.auth.CreateToken(ctx, auth.KindService, "globex", []string{"globex"})
	_, acme, _ := s.auth.CreateToken(ctx, auth.KindService, "acme", []string{"acme"})
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
		if tpl, _ := s.store.GetTemplate(ctx, id); tpl.Dockerfile != "FROM alpine" {
			t.Fatalf("%s was rewritten: %q", id, tpl.Dockerfile)
		}
	}
}

func TestATokensBuildBelongsToItsTenant(t *testing.T) {
	s := replicas(t, 1)[0]
	_, acme, _ := s.auth.CreateToken(context.Background(), auth.KindService, "acme", []string{"acme"})
	// No host to build on (503), but the template was claimed for acme, not shared.
	do(s, "POST", "/templates/build", nil, bearer(acme), `{"name":"mine","dockerfile":"FROM alpine"}`)
	if tpl, err := s.store.GetTemplate(context.Background(), "mine"); err == nil && tpl.Tenant != "acme" {
		t.Fatalf("template tenant %q, want acme", tpl.Tenant)
	}
	_, both, _ := s.auth.CreateToken(context.Background(), auth.KindService, "both", []string{"acme", "globex"})
	if w := do(s, "POST", "/templates/build", nil, bearer(both), `{"name":"which","dockerfile":"FROM alpine"}`); w.Code != 400 {
		t.Fatalf("a two-tenant token must say which: %d %s", w.Code, w.Body)
	}
}

// VMs and VM sets go through the same checks: the tenant is valid and the
// caller's, and the template is theirs (or shared) and built.
func TestVMsOnlyBootTheirTenantsReadyTemplates(t *testing.T) {
	rs, d := replicasDB(t, 1)
	s := rs[0]
	ctx := context.Background()
	addTemplate(t, d, "acme-app", "acme", "FROM alpine", model.TemplateReady)
	addTemplate(t, d, "half-built", "", "FROM alpine", model.TemplateBuilding)
	_, globex, _ := s.auth.CreateToken(ctx, auth.KindService, "globex", []string{"globex"})
	_, admin, _ := s.auth.CreateToken(ctx, auth.KindAdmin, "ops", nil)
	for _, c := range []struct {
		token, tenant, template string
		want                    int
	}{
		{globex, "globex", "acme-app", 404}, // another tenant's template
		{admin, "globex", "acme-app", 404},  // not even an admin can mix tenants
		{admin, "globex", "half-built", 409},
		{admin, "bad tenant!", "default", 400},
		{globex, "acme", "default", 403},
	} {
		if w := do(s, "POST", "/vms", nil, bearer(c.token), `{"tenant":"`+c.tenant+`","template_id":"`+c.template+`"}`); w.Code != c.want {
			t.Errorf("VM for %s from %s: want %d, got %d %s", c.tenant, c.template, c.want, w.Code, w.Body)
		}
		if w := do(s, "POST", "/v1/vmsets", nil, bearer(c.token),
			`{"name":"web","tenant":"`+c.tenant+`","spec":{"replicas":1,"template_id":"`+c.template+`"}}`); w.Code != c.want {
			t.Errorf("VM set for %s from %s: want %d, got %d %s", c.tenant, c.template, c.want, w.Code, w.Body)
		}
	}
}

func TestOnlyAnAdminDeletesASharedTemplate(t *testing.T) {
	rs, d := replicasDB(t, 1)
	s := rs[0]
	ctx := context.Background()
	addTemplate(t, d, "base", "", "FROM alpine", model.TemplateReady)
	_, acme, _ := s.auth.CreateToken(ctx, auth.KindService, "acme", []string{"acme"})
	if w := do(s, "DELETE", "/templates/base", nil, bearer(acme), ""); w.Code != 403 {
		t.Fatalf("a tenant deleted a shared template: %d %s", w.Code, w.Body)
	}
	_, admin, _ := s.auth.CreateToken(ctx, auth.KindAdmin, "ops", nil)
	if w := do(s, "DELETE", "/templates/base", nil, bearer(admin), ""); w.Code != 200 {
		t.Fatalf("admin delete: %d %s", w.Code, w.Body)
	}
	if _, err := s.store.GetTemplate(ctx, "base"); err == nil {
		t.Fatal("template still in the catalog after delete")
	}
}
