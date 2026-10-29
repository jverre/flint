package store

import (
	"context"
	"database/sql"
	"encoding/json"

	"github.com/jverre/flint/cluster/internal/db"
	"github.com/jverre/flint/cluster/internal/model"
)

const snapCols = `id, kind, COALESCE(vm_id, ''), COALESCE(template_id, ''), tenant, backend, host_class, COALESCE(source_host, ''), uploaded, bytes, created_at`

func scanSnapshot(r scanner) (*model.Snapshot, error) {
	var sn model.Snapshot
	var kind string
	var up int
	if err := r.Scan(&sn.ID, &kind, &sn.VMID, &sn.TemplateID, &sn.Tenant, &sn.Backend, &sn.HostClass, &sn.SourceHost, &up, &sn.Bytes, &sn.CreatedAt); err != nil {
		return nil, err
	}
	sn.Kind = model.SnapshotKind(kind)
	sn.Uploaded = up != 0
	return &sn, nil
}

// InsertSnapshot records a snapshot; recording it again updates whether it
// is uploaded and its size.
func (s *Store) InsertSnapshot(ctx context.Context, sn *model.Snapshot) error {
	if sn.CreatedAt == 0 {
		sn.CreatedAt = model.NowMs()
	}
	_, err := s.db.Exec(ctx, `INSERT INTO snapshots (id, kind, vm_id, template_id, tenant, backend, host_class, source_host, uploaded, bytes, created_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET uploaded = excluded.uploaded, bytes = excluded.bytes`,
		sn.ID, string(sn.Kind), nullable(sn.VMID), nullable(sn.TemplateID), sn.Tenant, sn.Backend, sn.HostClass, nullable(sn.SourceHost),
		boolInt(sn.Uploaded), sn.Bytes, sn.CreatedAt)
	return err
}

func (s *Store) GetSnapshot(ctx context.Context, id string) (*model.Snapshot, error) {
	return scanSnapshot(s.db.QueryRow(ctx, `SELECT `+snapCols+` FROM snapshots WHERE id = ?`, id))
}

// SnapshotFilter selects snapshots; zero fields don't filter.
type SnapshotFilter struct {
	Kind          model.SnapshotKind
	TemplateID    string
	HostClass     string
	UploadedOnly  bool
	PendingOnly   bool  // not yet uploaded
	CreatedBefore int64 // created_at < this
}

// ListSnapshots returns matching snapshots, oldest first.
func (s *Store) ListSnapshots(ctx context.Context, f SnapshotFilter) ([]*model.Snapshot, error) {
	var w where
	if f.Kind != "" {
		w.add("kind = ?", string(f.Kind))
	}
	if f.TemplateID != "" {
		w.add("template_id = ?", f.TemplateID)
	}
	if f.HostClass != "" {
		w.add("host_class = ?", f.HostClass)
	}
	if f.UploadedOnly {
		w.add("uploaded = 1")
	}
	if f.PendingOnly {
		w.add("uploaded = 0")
	}
	if f.CreatedBefore > 0 {
		w.add("created_at < ?", f.CreatedBefore)
	}
	return queryAll(ctx, s, scanSnapshot, `SELECT `+snapCols+` FROM snapshots`+w.String()+` ORDER BY created_at`, w.args...)
}

func (s *Store) MarkSnapshotUploaded(ctx context.Context, id string) error {
	_, err := s.db.Exec(ctx, `UPDATE snapshots SET uploaded = 1 WHERE id = ?`, id)
	return err
}

func (s *Store) DeleteSnapshot(ctx context.Context, id string) error {
	_, err := s.db.Exec(ctx, `DELETE FROM snapshots WHERE id = ?`, id)
	return err
}

// ── Templates ─────────────────────────────────────────────────────────────

const templateCols = `id, name, tenant, dockerfile, rootfs_mb, status, build_host, error, created_at, updated_at`

func scanTemplate(r scanner) (*model.Template, error) {
	var t model.Template
	var status string
	var bh sql.NullString
	if err := r.Scan(&t.ID, &t.Name, &t.Tenant, &t.Dockerfile, &t.RootfsMB, &status, &bh, &t.Error, &t.CreatedAt, &t.UpdatedAt); err != nil {
		return nil, err
	}
	t.Status, t.BuildHost = model.TemplateStatus(status), bh.String
	return &t, nil
}

func (s *Store) GetTemplate(ctx context.Context, id string) (*model.Template, error) {
	return scanTemplate(s.db.QueryRow(ctx, `SELECT `+templateCols+` FROM templates WHERE id = ?`, id))
}

func (s *Store) ListTemplates(ctx context.Context) ([]*model.Template, error) {
	return queryAll(ctx, s, scanTemplate, `SELECT `+templateCols+` FROM templates ORDER BY created_at`)
}

// ClaimTemplateBuild records a (re)build of t on its build host, unless the
// ID belongs to another tenant (ok=false). A rebuild drops the template's
// old snapshots.
func (s *Store) ClaimTemplateBuild(ctx context.Context, t *model.Template) (ok bool, err error) {
	now := model.NowMs()
	err = s.db.Tx(ctx, func(tx *db.Tx) error {
		k, err := affected(tx.Exec(ctx, `INSERT INTO templates (id, name, tenant, dockerfile, rootfs_mb, status, build_host, error, created_at, updated_at)
			VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?) ON CONFLICT (id) DO UPDATE SET name = excluded.name, dockerfile = excluded.dockerfile,
			rootfs_mb = excluded.rootfs_mb, status = excluded.status, build_host = excluded.build_host, error = '', updated_at = excluded.updated_at
			WHERE templates.tenant = excluded.tenant`,
			t.ID, t.Name, t.Tenant, t.Dockerfile, t.RootfsMB, string(model.TemplateBuilding), nullable(t.BuildHost), now, now))
		if err != nil || k == 0 {
			return err
		}
		ok = true
		_, err = tx.Exec(ctx, `DELETE FROM snapshots WHERE kind = ? AND template_id = ?`, string(model.SnapshotTemplate), t.ID)
		return err
	})
	return ok, err
}

func (s *Store) SetTemplateStatus(ctx context.Context, id string, status model.TemplateStatus, errMsg string) error {
	_, err := s.db.Exec(ctx, `UPDATE templates SET status = ?, error = ?, updated_at = ? WHERE id = ?`, string(status), errMsg, model.NowMs(), id)
	return err
}

// DeleteTemplate removes a template and its snapshots' catalog entries.
func (s *Store) DeleteTemplate(ctx context.Context, id string) error {
	return s.db.Tx(ctx, func(tx *db.Tx) error {
		if _, err := tx.Exec(ctx, `DELETE FROM snapshots WHERE kind = ? AND template_id = ?`, string(model.SnapshotTemplate), id); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, `DELETE FROM templates WHERE id = ?`, id)
		return err
	})
}

// ── VM sets ───────────────────────────────────────────────────────────────

const vmsetCols = `id, tenant, name, spec, generation, deleted, created_at, updated_at`

func scanVMSet(r scanner) (*model.VMSet, error) {
	var vs model.VMSet
	var spec string
	var del int
	if err := r.Scan(&vs.ID, &vs.Tenant, &vs.Name, &spec, &vs.Generation, &del, &vs.CreatedAt, &vs.UpdatedAt); err != nil {
		return nil, err
	}
	vs.Deleted = del != 0
	json.Unmarshal([]byte(spec), &vs.Spec)
	return &vs, nil
}

func (s *Store) InsertVMSet(ctx context.Context, vs *model.VMSet) error {
	now := model.NowMs()
	vs.Generation, vs.CreatedAt, vs.UpdatedAt = 1, now, now
	_, err := s.db.Exec(ctx, `INSERT INTO vmsets (id, tenant, name, spec, generation, deleted, created_at, updated_at) VALUES (?, ?, ?, ?, 1, 0, ?, ?)`,
		vs.ID, vs.Tenant, vs.Name, mustJSON(vs.Spec), now, now)
	return err
}

func (s *Store) GetVMSet(ctx context.Context, id string) (*model.VMSet, error) {
	return scanVMSet(s.db.QueryRow(ctx, `SELECT `+vmsetCols+` FROM vmsets WHERE id = ?`, id))
}

func (s *Store) ListVMSets(ctx context.Context, includeDeleted bool) ([]*model.VMSet, error) {
	q := `SELECT ` + vmsetCols + ` FROM vmsets`
	if !includeDeleted {
		q += ` WHERE deleted = 0`
	}
	return queryAll(ctx, s, scanVMSet, q+` ORDER BY created_at`)
}

// UpdateVMSetSpec replaces a set's spec, bumping its generation.
func (s *Store) UpdateVMSetSpec(ctx context.Context, id string, spec model.VMSetSpec) error {
	_, err := s.db.Exec(ctx, `UPDATE vmsets SET spec = ?, generation = generation + 1, updated_at = ? WHERE id = ?`, mustJSON(spec), model.NowMs(), id)
	return err
}

// MarkVMSetDeleted hides a set; the reconciler deletes its members, then it.
func (s *Store) MarkVMSetDeleted(ctx context.Context, id string) error {
	_, err := s.db.Exec(ctx, `UPDATE vmsets SET deleted = 1, updated_at = ? WHERE id = ?`, model.NowMs(), id)
	return err
}

func (s *Store) DeleteVMSet(ctx context.Context, id string) error {
	_, err := s.db.Exec(ctx, `DELETE FROM vmsets WHERE id = ?`, id)
	return err
}
