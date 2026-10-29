package server

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"regexp"
	"strings"

	"github.com/jverre/flint/cluster/internal/auth"
	"github.com/jverre/flint/cluster/internal/engine"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// slugify matches flint.core._template_build._slugify, so IDs agree with hosts.
var slugRe = regexp.MustCompile(`[^a-z0-9]+`)

func slugify(name string) string {
	s := strings.Trim(slugRe.ReplaceAllString(strings.ToLower(name), "-"), "-")
	if s == "" {
		s = "template"
	}
	return s
}

// templateTaken refuses a build of id for tenant when the ID belongs to
// someone else. IDs are one namespace: a rebuild must not replace another
// tenant's template (or a shared one), whose VMs would then run this Dockerfile.
func (s *Server) templateTaken(ctx context.Context, p *auth.Principal, id, tenant string) error {
	t, err := s.store.GetTemplate(ctx, id)
	if store.IsNotFound(err) {
		return nil
	}
	if err != nil {
		return err
	}
	switch {
	case t.Tenant == tenant:
		return nil
	case t.Tenant == "" || p.CanTenant(t.Tenant):
		return httpx.Conflict("template " + id + " already exists")
	default:
		return httpx.Forbidden("template " + id + " already exists for another tenant")
	}
}

// handleBuildTemplate builds on one host. When done, the host publishes the
// result as a snapshot; other hosts of the same class restore it on demand.
func (s *Server) handleBuildTemplate(w http.ResponseWriter, r *http.Request) error {
	ctx := r.Context()
	p := auth.FromContext(ctx)
	var body struct {
		Name       string `json:"name"`
		Dockerfile string `json:"dockerfile"`
		RootfsMB   int    `json:"rootfs_size_mb"`
		Tenant     string `json:"tenant"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20)).Decode(&body); err != nil || body.Name == "" || body.Dockerfile == "" {
		return httpx.BadRequest("name and dockerfile are required")
	}
	tenant, err := p.ResolveTenant(body.Tenant)
	if err != nil {
		return err
	}
	if body.RootfsMB == 0 {
		body.RootfsMB = 500
	}
	id := slugify(body.Name)
	if id == model.DefaultTemplate {
		return httpx.BadRequest("cannot rebuild the default template")
	}
	if err := s.templateTaken(ctx, p, id, tenant); err != nil {
		return err
	}
	cands, err := s.eng.Candidates(ctx, engine.PlaceRequest{TemplateID: model.DefaultTemplate})
	if err != nil {
		return err
	}
	h := cands[0]
	claimed, err := s.store.ClaimTemplateBuild(ctx, &model.Template{ID: id, Name: body.Name, Tenant: tenant, Dockerfile: body.Dockerfile,
		RootfsMB: body.RootfsMB, BuildHost: h.ID})
	if err != nil {
		return err
	}
	if !claimed { // another tenant claimed it meanwhile
		if err := s.templateTaken(ctx, p, id, tenant); err != nil {
			return err
		}
		return httpx.Conflict("template " + id + " already exists")
	}
	if err := s.agents.BuildTemplate(ctx, h, body.Name, body.Dockerfile, body.RootfsMB); err != nil {
		if serr := s.store.SetTemplateStatus(ctx, id, model.TemplateError, err.Error()); serr != nil {
			log.Printf("template %s: %v", id, serr)
		}
		return err
	}
	return httpx.JSON(w, map[string]any{"template_id": id, "status": model.TemplateBuilding, "build_host": h.ID})
}

func templateDict(t *model.Template) map[string]any {
	return map[string]any{"template_id": t.ID, "name": t.Name, "status": t.Status, "tenant": t.Tenant,
		"build_host": t.BuildHost, "error": t.Error, "created_at": float64(t.CreatedAt) / 1000}
}

func defaultTemplate() map[string]any {
	return map[string]any{"template_id": model.DefaultTemplate, "name": "Default (Alpine)", "status": model.TemplateReady}
}

func (s *Server) handleListTemplates(w http.ResponseWriter, r *http.Request) error {
	ts, err := s.store.ListTemplates(r.Context())
	if err != nil {
		return err
	}
	p := auth.FromContext(r.Context())
	out := []map[string]any{defaultTemplate()}
	for _, t := range ts {
		if t.Tenant == "" || p.CanTenant(t.Tenant) {
			out = append(out, templateDict(t))
		}
	}
	return httpx.JSON(w, map[string]any{"templates": out})
}

var errTemplateNotFound = httpx.NotFound("Template not found")

// templateForRequest loads the path's template if the caller may see it.
func (s *Server) templateForRequest(r *http.Request) (*model.Template, error) {
	t, err := s.store.GetTemplate(r.Context(), r.PathValue("id"))
	if store.IsNotFound(err) {
		return nil, errTemplateNotFound
	}
	if err != nil {
		return nil, err
	}
	if t.Tenant != "" && !auth.FromContext(r.Context()).CanTenant(t.Tenant) {
		return nil, errTemplateNotFound
	}
	return t, nil
}

func (s *Server) handleGetTemplate(w http.ResponseWriter, r *http.Request) error {
	if r.PathValue("id") == model.DefaultTemplate {
		return httpx.JSON(w, map[string]any{"template": defaultTemplate()})
	}
	t, err := s.templateForRequest(r)
	if err != nil {
		return err
	}
	d := templateDict(t)
	snaps, err := s.store.ListSnapshots(r.Context(), store.SnapshotFilter{Kind: model.SnapshotTemplate, TemplateID: t.ID})
	if err != nil {
		return err
	}
	classes := []string{}
	for _, sn := range snaps {
		classes = append(classes, sn.HostClass)
	}
	d["host_classes"] = classes
	return httpx.JSON(w, map[string]any{"template": d})
}

func (s *Server) handleDeleteTemplate(w http.ResponseWriter, r *http.Request) error {
	if r.PathValue("id") == model.DefaultTemplate {
		return httpx.BadRequest("Cannot delete default template")
	}
	t, err := s.templateForRequest(r)
	if err != nil {
		return err
	}
	if p := auth.FromContext(r.Context()); t.Tenant == "" && !p.SeesAllTenants() {
		return httpx.Forbidden("shared templates can only be deleted by an admin")
	}
	hosts, err := s.store.ListHosts(r.Context())
	if err != nil {
		return err
	}
	for _, h := range hosts {
		if (h.HasTemplate(t.ID) || h.ID == t.BuildHost) && h.State.Reachable() {
			if err := s.agents.DeleteTemplate(r.Context(), h, t.ID); err != nil {
				log.Printf("deleting template %s on %s: %v", t.ID, h.Name, err)
			}
		}
	}
	if err := s.store.DeleteTemplate(r.Context(), t.ID); err != nil {
		return err
	}
	return httpx.OK(w)
}
