// Package store is the catalog's persistence: every SQL statement the
// control plane runs is here, behind typed methods. Changes that span
// several statements run in one transaction. Records are model types.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"

	"github.com/jverre/flint/cluster/internal/db"
	"github.com/jverre/flint/cluster/internal/model"
)

// Store is the catalog in the shared database.
type Store struct {
	db *db.DB
}

func New(d *db.DB) *Store { return &Store{db: d} }

// Dialect is "postgres" or "sqlite".
func (s *Store) Dialect() string { return s.db.Dialect }

func (s *Store) Close() error { return s.db.Close() }

// ErrNotFound is what lookups of a missing record return.
var ErrNotFound = sql.ErrNoRows

func IsNotFound(err error) bool { return errors.Is(err, sql.ErrNoRows) }

// Ping checks the database answers.
func (s *Store) Ping(ctx context.Context) error {
	var one int
	return s.db.QueryRow(ctx, `SELECT 1`).Scan(&one)
}

type scanner interface{ Scan(dest ...any) error }

type execer interface {
	Exec(ctx context.Context, q string, args ...any) (sql.Result, error)
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func mustJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// affected is the number of rows a statement changed.
func affected(res sql.Result, err error) (int64, error) {
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

// where builds a WHERE clause from conditions that are set.
type where struct {
	conds []string
	args  []any
}

func (w *where) add(cond string, args ...any) {
	w.conds = append(w.conds, cond)
	w.args = append(w.args, args...)
}

func (w *where) in(col string, vals []string) {
	if len(vals) == 0 {
		return
	}
	marks := make([]string, len(vals))
	for i, v := range vals {
		marks[i] = "?"
		w.args = append(w.args, v)
	}
	w.conds = append(w.conds, col+" IN ("+strings.Join(marks, ", ")+")")
}

func (w *where) String() string {
	if len(w.conds) == 0 {
		return ""
	}
	return " WHERE " + strings.Join(w.conds, " AND ")
}

func queryAll[T any](ctx context.Context, s *Store, scan func(scanner) (T, error), q string, args ...any) ([]T, error) {
	rows, err := s.db.Query(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []T
	for rows.Next() {
		v, err := scan(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// ── Leases (reconciler leadership and VM locks) ───────────────────────────

func (s *Store) TryLease(ctx context.Context, name, holder string, now, expiresAt int64) (bool, error) {
	return s.db.TryLease(ctx, name, holder, now, expiresAt)
}

func (s *Store) RenewLease(ctx context.Context, name, holder string, expiresAt int64) (bool, error) {
	return s.db.RenewLease(ctx, name, holder, expiresAt)
}

func (s *Store) ReleaseLease(ctx context.Context, name, holder string) error {
	return s.db.ReleaseLease(ctx, name, holder)
}

// HeldLeases returns the names of unexpired leases starting with prefix.
func (s *Store) HeldLeases(ctx context.Context, prefix string, now int64) ([]string, error) {
	return queryAll(ctx, s, func(r scanner) (string, error) {
		var name string
		return name, r.Scan(&name)
	}, `SELECT name FROM leases WHERE name LIKE ? AND expires_at >= ?`, prefix+"%", now)
}

// ── Meta (cluster-wide settings: CAs, signing keys) ───────────────────────

// SetMetaIfAbsent stores a value unless one exists; returns the stored value.
func (s *Store) SetMetaIfAbsent(ctx context.Context, key, value string) (string, error) {
	return s.db.SetMetaIfAbsent(ctx, key, value)
}

// InsertMetaIfAbsent stores a value unless one exists.
func (s *Store) InsertMetaIfAbsent(ctx context.Context, key, value string) error {
	_, err := s.db.Exec(ctx, `INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING`, key, value)
	return err
}

func (s *Store) GetMeta(ctx context.Context, key string) (string, error) {
	var v string
	err := s.db.QueryRow(ctx, `SELECT value FROM meta WHERE key = ?`, key).Scan(&v)
	return v, err
}

// ── Quotas ────────────────────────────────────────────────────────────────

// TenantQuota is a tenant's max VMs (0: no limit).
func (s *Store) TenantQuota(ctx context.Context, tenant string) (int, error) {
	var max int
	err := s.db.QueryRow(ctx, `SELECT max_vms FROM tenant_quotas WHERE tenant = ?`, tenant).Scan(&max)
	if IsNotFound(err) {
		return 0, nil
	}
	return max, err
}

func (s *Store) PutTenantQuota(ctx context.Context, tenant string, maxVMs int) error {
	_, err := s.db.Exec(ctx, `INSERT INTO tenant_quotas (tenant, max_vms, updated_at) VALUES (?, ?, ?)
		ON CONFLICT (tenant) DO UPDATE SET max_vms = excluded.max_vms, updated_at = excluded.updated_at`, tenant, maxVMs, model.NowMs())
	return err
}

// CountTenantVMs counts the VMs a tenant holds: every VM in the catalog but
// lost ones (paused ones hold a snapshot and can be resumed at any time).
func (s *Store) CountTenantVMs(ctx context.Context, tenant string) (int, error) {
	var n int
	err := s.db.QueryRow(ctx, `SELECT COUNT(*) FROM vms WHERE tenant = ? AND state <> ?`, tenant, model.VMLost).Scan(&n)
	return n, err
}
