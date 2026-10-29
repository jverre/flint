package server

import (
	"context"
	"testing"
)

// Template IDs are one namespace across tenants; ownership must still hold.

func addTemplate(t *testing.T, s *Server, id, tenant, image string) {
	t.Helper()
	if _, err := s.db.Exec(context.Background(), `INSERT INTO templates (id, name, tenant, image, rootfs_mb, status, build_host, error, created_at, updated_at)
		VALUES (?, ?, ?, ?, 500, 'ready', NULL, '', 1, 1)`, id, id, tenant, image); err != nil {
		t.Fatal(err)
	}
}

func bearer(token string) map[string]string {
	return map[string]string{"Authorization": "Bearer " + token}
}

func TestOnlyAnAdminImportsOrDeletesTemplates(t *testing.T) {
	s := replicas(t, 1)[0]
	ctx := context.Background()
	addTemplate(t, s, "base", "", "alpine:3.20")
	addTemplate(t, s, "app", "acme", "alpine:3.20")
	_, acme, _ := s.createToken(ctx, KindService, "acme", []string{"acme"})
	if w := do(s, "POST", "/templates", nil, bearer(acme), `{"name":"mine","image":"alpine:3.20"}`); w.Code != 403 {
		t.Fatalf("a service token imported a template: %d %s", w.Code, w.Body)
	}
	for _, id := range []string{"base", "app"} { // shared, and the token's own tenant's
		if w := do(s, "DELETE", "/templates/"+id, nil, bearer(acme), ""); w.Code != 403 {
			t.Fatalf("a service token deleted %s: %d %s", id, w.Code, w.Body)
		}
	}
	if w := do(s, "GET", "/templates/app", nil, bearer(acme), ""); w.Code != 200 {
		t.Fatalf("a tenant can't read its own template: %d %s", w.Code, w.Body)
	}
	_, admin, _ := s.createToken(ctx, KindAdmin, "ops", nil)
	if w := do(s, "DELETE", "/templates/base", nil, bearer(admin), ""); w.Code != 200 {
		t.Fatalf("admin delete: %d %s", w.Code, w.Body)
	}
}

func TestAnImportCannotReplaceAnotherOwnersTemplate(t *testing.T) {
	s := replicas(t, 1)[0]
	ctx := context.Background()
	addTemplate(t, s, "app", "acme", "alpine:3.20")
	addTemplate(t, s, "base", "", "alpine:3.20")
	_, admin, _ := s.createToken(ctx, KindAdmin, "ops", nil)
	for _, body := range []string{
		`{"name":"app","image":"evil:1"}`,                   // acme's, as a shared template
		`{"name":"app","image":"evil:1","tenant":"globex"}`, // acme's, for another tenant
		`{"name":"base","image":"evil:1","tenant":"acme"}`,  // a shared one, for one tenant
	} {
		if w := do(s, "POST", "/templates", nil, bearer(admin), body); w.Code != 409 {
			t.Fatalf("%s: want 409, got %d %s", body, w.Code, w.Body)
		}
	}
	for _, id := range []string{"app", "base"} {
		if tpl, _ := s.getTemplate(ctx, id); tpl.Image != "alpine:3.20" || tpl.Status != "ready" {
			t.Fatalf("%s was replaced: %+v", id, tpl)
		}
	}
}

func TestImportRejectsBadRequests(t *testing.T) {
	s := replicas(t, 1)[0]
	_, admin, _ := s.createToken(context.Background(), KindAdmin, "ops", nil)
	for _, body := range []string{
		`{"name":"x","image":"alpine; rm -rf /"}`,
		`{"name":"x","image":"alpine:3.20","rootfs_size_mb":32}`,
		`{"name":"x","image":"alpine:3.20","registry_auth":{"username":"u"}}`,
		`{"name":"default","image":"alpine:3.20"}`,
	} {
		if w := do(s, "POST", "/templates", nil, bearer(admin), body); w.Code != 400 {
			t.Fatalf("%s: want 400, got %d %s", body, w.Code, w.Body)
		}
	}
	if w := do(s, "POST", "/templates/build", nil, bearer(admin), `{"name":"x","dockerfile":"FROM alpine"}`); w.Code != 410 {
		t.Fatalf("Dockerfile builds: want 410, got %d %s", w.Code, w.Body)
	}
}

func TestVMsOnlyBootTheirTenantsTemplates(t *testing.T) {
	s := replicas(t, 1)[0]
	ctx := context.Background()
	addTemplate(t, s, "acme-app", "acme", "alpine:3.20")
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
