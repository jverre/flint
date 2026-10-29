package store

import (
	"context"
	"encoding/json"
	"strings"

	"github.com/jverre/flint/cluster/internal/db"
	"github.com/jverre/flint/cluster/internal/model"
)

const vmCols = `id, tenant, COALESCE(host_id, ''), state, template_id, availability, spec, labels, COALESCE(vmset_id, ''), role,
COALESCE(checkpoint_id, ''), checkpoint_at, COALESCE(pause_id, ''), error, info, created_at, updated_at`

func scanVM(r scanner) (*model.VM, error) {
	var v model.VM
	var state, avail, role, spec, labels, info string
	if err := r.Scan(&v.ID, &v.Tenant, &v.HostID, &state, &v.TemplateID, &avail, &spec, &labels, &v.VMSetID, &role,
		&v.CheckpointID, &v.CheckpointAt, &v.PauseID, &v.Error, &info, &v.CreatedAt, &v.UpdatedAt); err != nil {
		return nil, err
	}
	v.State, v.Availability, v.Role = model.VMState(state), model.Availability(avail), model.Role(role)
	json.Unmarshal([]byte(spec), &v.Spec)
	json.Unmarshal([]byte(labels), &v.Labels)
	json.Unmarshal([]byte(info), &v.Info)
	return &v, nil
}

func (s *Store) InsertVM(ctx context.Context, v *model.VM) error {
	return insertVM(ctx, s.db, v)
}

func insertVM(ctx context.Context, x execer, v *model.VM) error {
	now := model.NowMs()
	v.CreatedAt, v.UpdatedAt = now, now
	if v.Labels == nil {
		v.Labels = map[string]string{}
	}
	_, err := x.Exec(ctx, `INSERT INTO vms (id, tenant, host_id, state, template_id, availability, spec, labels, vmset_id, role,
		checkpoint_id, checkpoint_at, pause_id, error, info, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		v.ID, v.Tenant, nullable(v.HostID), string(v.State), v.TemplateID, string(v.Availability), mustJSON(v.Spec), mustJSON(v.Labels),
		nullable(v.VMSetID), string(v.Role), nullable(v.CheckpointID), v.CheckpointAt, nullable(v.PauseID), v.Error, mustJSON(v.Info), now, now)
	return err
}

func (s *Store) GetVM(ctx context.Context, id string) (*model.VM, error) {
	return scanVM(s.db.QueryRow(ctx, `SELECT `+vmCols+` FROM vms WHERE id = ?`, id))
}

// VMFilter selects VMs; zero fields don't filter.
type VMFilter struct {
	Tenant           string
	HostID           string
	VMSetID          string
	States           []model.VMState
	Availability     model.Availability
	UpdatedBefore    int64 // updated_at < this
	CheckpointBefore int64 // checkpoint_at < this
}

// ListVMs returns matching VMs, oldest first.
func (s *Store) ListVMs(ctx context.Context, f VMFilter) ([]*model.VM, error) {
	var w where
	if f.Tenant != "" {
		w.add("tenant = ?", f.Tenant)
	}
	if f.HostID != "" {
		w.add("host_id = ?", f.HostID)
	}
	if f.VMSetID != "" {
		w.add("vmset_id = ?", f.VMSetID)
	}
	states := make([]string, len(f.States))
	for i, st := range f.States {
		states[i] = string(st)
	}
	w.in("state", states)
	if f.Availability != "" {
		w.add("availability = ?", string(f.Availability))
	}
	if f.UpdatedBefore > 0 {
		w.add("updated_at < ?", f.UpdatedBefore)
	}
	if f.CheckpointBefore > 0 {
		w.add("checkpoint_at < ?", f.CheckpointBefore)
	}
	return queryAll(ctx, s, scanVM, `SELECT `+vmCols+` FROM vms`+w.String()+` ORDER BY created_at`, w.args...)
}

// VMUpdate is a partial update; nil fields are left alone.
type VMUpdate struct {
	HostID       *string
	State        *model.VMState
	CheckpointID *string
	CheckpointAt *int64
	PauseID      *string
	Error        *string
	Info         map[string]any
	Spec         *model.VMSpec
	Role         *model.Role
}

// Ptr is a pointer to v, for VMUpdate fields.
func Ptr[T any](v T) *T { return &v }

func (s *Store) UpdateVM(ctx context.Context, id string, u VMUpdate) error {
	sets := []string{"updated_at = ?"}
	args := []any{model.NowMs()}
	set := func(col string, v any) {
		sets = append(sets, col+" = ?")
		args = append(args, v)
	}
	if u.HostID != nil {
		set("host_id", nullable(*u.HostID))
	}
	if u.State != nil {
		set("state", string(*u.State))
	}
	if u.CheckpointID != nil {
		set("checkpoint_id", nullable(*u.CheckpointID))
	}
	if u.CheckpointAt != nil {
		set("checkpoint_at", *u.CheckpointAt)
	}
	if u.PauseID != nil {
		set("pause_id", nullable(*u.PauseID))
	}
	if u.Error != nil {
		set("error", *u.Error)
	}
	if u.Info != nil {
		set("info", mustJSON(u.Info))
	}
	if u.Spec != nil {
		set("spec", mustJSON(u.Spec))
	}
	if u.Role != nil {
		set("role", string(*u.Role))
	}
	args = append(args, id)
	_, err := s.db.Exec(ctx, `UPDATE vms SET `+strings.Join(sets, ", ")+` WHERE id = ?`, args...)
	return err
}

// DeleteVM removes a VM and any drain reservation it held.
func (s *Store) DeleteVM(ctx context.Context, id string) error {
	return s.db.Tx(ctx, func(tx *db.Tx) error {
		if _, err := tx.Exec(ctx, `DELETE FROM drain_reservations WHERE vm_id = ?`, id); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, `DELETE FROM vms WHERE id = ?`, id)
		return err
	})
}

// ActiveVMsByHost counts, per host, the VMs that use its CPU and memory.
func (s *Store) ActiveVMsByHost(ctx context.Context) (map[string]int, error) {
	args := []any{}
	marks := []string{}
	for _, st := range model.ActiveVMStates {
		args = append(args, string(st))
		marks = append(marks, "?")
	}
	rows, err := s.db.Query(ctx, `SELECT host_id, COUNT(*) FROM vms WHERE host_id IS NOT NULL AND state IN (`+strings.Join(marks, ", ")+`) GROUP BY host_id`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int{}
	for rows.Next() {
		var h string
		var n int
		if err := rows.Scan(&h, &n); err != nil {
			return nil, err
		}
		out[h] = n
	}
	return out, rows.Err()
}
