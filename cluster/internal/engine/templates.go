package engine

import (
	"context"
	"fmt"

	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/store"
)

// TemplateFor returns a template a tenant's VMs may boot: a shared one or the
// tenant's own. Another tenant's is "not found", even for an admin.
func (e *Engine) TemplateFor(ctx context.Context, id, tenant string) (*model.Template, error) {
	t, err := e.store.GetTemplate(ctx, id)
	if err == nil && t.Tenant != "" && t.Tenant != tenant {
		err = store.ErrNotFound
	}
	if store.IsNotFound(err) {
		return nil, httpx.NotFound("template not found: " + id)
	}
	return t, err
}

// CheckBootable checks a tenant may boot new VMs from a template now: it is
// theirs (or shared) and built. The default template always is.
func (e *Engine) CheckBootable(ctx context.Context, id, tenant string) error {
	if id == model.DefaultTemplate {
		return nil
	}
	t, err := e.TemplateFor(ctx, id, tenant)
	if err != nil {
		return err
	}
	if t.Status != model.TemplateReady {
		return httpx.Conflict("template " + id + " is " + string(t.Status))
	}
	return nil
}

// templateSnapshotFor is the newest uploaded build of a template for a host class.
func (e *Engine) templateSnapshotFor(ctx context.Context, templateID, hostClass string) (*model.Snapshot, error) {
	snaps, err := e.store.ListSnapshots(ctx, store.SnapshotFilter{Kind: model.SnapshotTemplate, TemplateID: templateID,
		HostClass: hostClass, UploadedOnly: true})
	if err != nil {
		return nil, err
	}
	if len(snaps) == 0 {
		return nil, fmt.Errorf("template %s has no snapshot for host class %s", templateID, hostClass)
	}
	return snaps[len(snaps)-1], nil
}

// CheckQuota refuses want more VMs for a tenant over its max_vms (0 = no
// limit). Every VM in the catalog counts, paused ones included: they hold a
// snapshot and can be resumed at any time.
func (e *Engine) CheckQuota(ctx context.Context, tenant string, want int) error {
	max, err := e.store.TenantQuota(ctx, tenant)
	if err != nil || max <= 0 {
		return err
	}
	used, err := e.store.CountTenantVMs(ctx, tenant)
	if err != nil {
		return err
	}
	if used+want > max {
		return &QuotaError{Tenant: tenant, Max: max, Used: used, Want: want}
	}
	return nil
}
