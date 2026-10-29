package store

import (
	"context"
	"encoding/json"

	"github.com/jverre/flint/cluster/internal/db"
	"github.com/jverre/flint/cluster/internal/model"
)

const opCols = `id, vm_id, tenant, kind, state, step, params, holder, error, created_at, updated_at`

func scanOp(r scanner) (*model.Operation, error) {
	var o model.Operation
	var kind, state, step, params string
	if err := r.Scan(&o.ID, &o.VMID, &o.Tenant, &kind, &state, &step, &params, &o.Holder, &o.Error, &o.CreatedAt, &o.UpdatedAt); err != nil {
		return nil, err
	}
	o.Kind, o.State, o.Step = model.OpKind(kind), model.OpState(state), model.OpStep(step)
	json.Unmarshal([]byte(params), &o.Params)
	return &o, nil
}

func (s *Store) InsertOp(ctx context.Context, op *model.Operation) error {
	_, err := s.db.Exec(ctx, `INSERT INTO operations (id, vm_id, tenant, kind, state, step, params, holder, error, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)`, op.ID, op.VMID, op.Tenant, string(op.Kind), string(op.State), string(op.Step),
		mustJSON(op.Params), op.Holder, op.CreatedAt, op.UpdatedAt)
	return err
}

func (s *Store) GetOp(ctx context.Context, id string) (*model.Operation, error) {
	return scanOp(s.db.QueryRow(ctx, `SELECT `+opCols+` FROM operations WHERE id = ?`, id))
}

// StaleOps are running operations not updated since before.
func (s *Store) StaleOps(ctx context.Context, before int64) ([]*model.Operation, error) {
	return queryAll(ctx, s, scanOp, `SELECT `+opCols+` FROM operations WHERE state = ? AND updated_at < ? ORDER BY created_at`,
		string(model.OpRunning), before)
}

// SaveOpStep records an operation's step and params, if op.Holder still holds it.
func (s *Store) SaveOpStep(ctx context.Context, op *model.Operation) error {
	_, err := s.db.Exec(ctx, `UPDATE operations SET step = ?, params = ?, updated_at = ? WHERE id = ? AND holder = ?`,
		string(op.Step), mustJSON(op.Params), model.NowMs(), op.ID, op.Holder)
	return err
}

// FinishOp records an operation's outcome, if holder still holds it.
func (s *Store) FinishOp(ctx context.Context, id, holder string, state model.OpState, errMsg string) error {
	_, err := s.db.Exec(ctx, `UPDATE operations SET state = ?, error = ?, updated_at = ? WHERE id = ? AND holder = ?`,
		string(state), errMsg, model.NowMs(), id, holder)
	return err
}

// TakeOverOp makes holder the holder of a still-running operation.
func (s *Store) TakeOverOp(ctx context.Context, id, holder string) (bool, error) {
	k, err := affected(s.db.Exec(ctx, `UPDATE operations SET holder = ?, updated_at = ? WHERE id = ? AND state = ?`,
		holder, model.NowMs(), id, string(model.OpRunning)))
	return k == 1, err
}

// PruneOps forgets finished operations and idempotency keys from before
// cutoff, and VM lock leases expired before leaseCutoff.
func (s *Store) PruneOps(ctx context.Context, cutoff, leaseCutoff int64) error {
	if _, err := s.db.Exec(ctx, `DELETE FROM operations WHERE state <> ? AND updated_at < ?`, string(model.OpRunning), cutoff); err != nil {
		return err
	}
	if _, err := s.db.Exec(ctx, `DELETE FROM idempotency_keys WHERE created_at < ?`, cutoff); err != nil {
		return err
	}
	_, err := s.db.Exec(ctx, `DELETE FROM leases WHERE name LIKE 'vm:%' AND expires_at < ?`, leaseCutoff)
	return err
}

// ── Idempotency keys ──────────────────────────────────────────────────────

// IdempotencyRecord is what is known about a request made with a key.
type IdempotencyRecord struct {
	RequestHash string
	OpID        string // the operation the request started, if any
	Status      int    // its answer, once there is one
	Body        string
}

// ClaimIdempotencyKey records the first use of a key; false if it was used before.
func (s *Store) ClaimIdempotencyKey(ctx context.Context, key, hash string) (bool, error) {
	k, err := affected(s.db.Exec(ctx, `INSERT INTO idempotency_keys (key, request_hash, created_at) VALUES (?, ?, ?) ON CONFLICT (key) DO NOTHING`,
		key, hash, model.NowMs()))
	return k == 1, err
}

func (s *Store) GetIdempotencyKey(ctx context.Context, key string) (*IdempotencyRecord, error) {
	var r IdempotencyRecord
	err := s.db.QueryRow(ctx, `SELECT request_hash, op_id, response_status, response_body FROM idempotency_keys WHERE key = ?`, key).
		Scan(&r.RequestHash, &r.OpID, &r.Status, &r.Body)
	if err != nil {
		return nil, err
	}
	return &r, nil
}

// ReclaimIdempotencyKey takes over a key whose first request died before
// starting an operation (no op, no answer, claimed before staleBefore).
func (s *Store) ReclaimIdempotencyKey(ctx context.Context, key string, staleBefore int64) (bool, error) {
	k, err := affected(s.db.Exec(ctx, `UPDATE idempotency_keys SET created_at = ? WHERE key = ? AND op_id = '' AND response_status = 0 AND created_at < ?`,
		model.NowMs(), key, staleBefore))
	return k == 1, err
}

// LinkIdempotencyKey ties a key to the operation its request started.
func (s *Store) LinkIdempotencyKey(ctx context.Context, key, opID string) error {
	_, err := s.db.Exec(ctx, `UPDATE idempotency_keys SET op_id = ? WHERE key = ? AND op_id = ''`, opID, key)
	return err
}

func (s *Store) SaveIdempotentResponse(ctx context.Context, key string, status int, body string) error {
	_, err := s.db.Exec(ctx, `UPDATE idempotency_keys SET response_status = ?, response_body = ? WHERE key = ?`, status, body, key)
	return err
}

func (s *Store) DeleteIdempotencyKey(ctx context.Context, key string) error {
	_, err := s.db.Exec(ctx, `DELETE FROM idempotency_keys WHERE key = ?`, key)
	return err
}

// ── Drain reservations ────────────────────────────────────────────────────

// ReserveDrain replaces a host's drain plan with moves, and marks it
// cordoned and draining, in one transaction.
func (s *Store) ReserveDrain(ctx context.Context, hostID string, moves []model.DrainMove) error {
	return s.db.Tx(ctx, func(tx *db.Tx) error {
		if _, err := tx.Exec(ctx, `DELETE FROM drain_reservations WHERE source_host = ?`, hostID); err != nil {
			return err
		}
		for _, m := range moves {
			if _, err := tx.Exec(ctx, `INSERT INTO drain_reservations (vm_id, source_host, target_host, vcpus, memory_mb, disk_mb) VALUES (?, ?, ?, ?, ?, ?)`,
				m.VMID, hostID, m.HostID, m.Resources.VCPUs, m.Resources.MemoryMB, m.Resources.DiskMB); err != nil {
				return err
			}
		}
		_, err := tx.Exec(ctx, `UPDATE hosts SET cordoned = 1, state = ?, state_since = ? WHERE id = ?`, string(model.HostDraining), model.NowMs(), hostID)
		return err
	})
}

// ReserveMove reserves room on target for one VM moving there.
func (s *Store) ReserveMove(ctx context.Context, vmID, source, target string, r model.Resources) error {
	_, err := s.db.Exec(ctx, `INSERT INTO drain_reservations (vm_id, source_host, target_host, vcpus, memory_mb, disk_mb) VALUES (?, ?, ?, ?, ?, ?)
		ON CONFLICT (vm_id) DO UPDATE SET target_host = excluded.target_host`, vmID, source, target, r.VCPUs, r.MemoryMB, r.DiskMB)
	return err
}

func (s *Store) DeleteReservation(ctx context.Context, vmID string) error {
	_, err := s.db.Exec(ctx, `DELETE FROM drain_reservations WHERE vm_id = ?`, vmID)
	return err
}

// ReservationTarget is where a drain plan sends a VM off source.
func (s *Store) ReservationTarget(ctx context.Context, vmID, source string) (string, error) {
	var target string
	err := s.db.QueryRow(ctx, `SELECT target_host FROM drain_reservations WHERE vm_id = ? AND source_host = ?`, vmID, source).Scan(&target)
	return target, err
}

// Reserved is the room reserved on one host for VMs moving there.
type Reserved struct {
	HostID   string
	Slots    int
	VCPUs    int
	MemoryMB int64
	DiskMB   int64
}

// ReservedByHost totals reservations per target host, leaving out exceptVM's own.
func (s *Store) ReservedByHost(ctx context.Context, exceptVM string) ([]Reserved, error) {
	return queryAll(ctx, s, func(r scanner) (Reserved, error) {
		var x Reserved
		return x, r.Scan(&x.HostID, &x.Slots, &x.VCPUs, &x.MemoryMB, &x.DiskMB)
	}, `SELECT target_host, COUNT(*), SUM(vcpus), SUM(memory_mb), SUM(disk_mb) FROM drain_reservations WHERE vm_id <> ? GROUP BY target_host`, exceptVM)
}
