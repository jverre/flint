package server

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
)

// An operation is the durable record of one change to a VM: what it is, how
// far it got (its step) and what it touched (its params: snapshot IDs, the
// source host). Each step is written before the action it guards, so if the
// replica running it dies, the leader can tell from the record and from the
// hosts whether to finish the operation or roll it back.
//
// An operation always holds its VM's lock (vmlock.go). The leader only takes
// over an operation once it can take that lock, i.e. once the replica that
// started it has stopped renewing it.

// Operation kinds.
const (
	OpCreate     = "create"
	OpDelete     = "delete"
	OpPause      = "pause"
	OpResume     = "resume"
	OpFork       = "fork"
	OpCheckpoint = "checkpoint"
	OpMigrate    = "migrate"
	OpRecover    = "recover"
)

// Operation states.
const (
	OpRunning   = "running"
	OpSucceeded = "succeeded"
	OpFailed    = "failed"
)

type Operation struct {
	ID        string            `json:"id"`
	VMID      string            `json:"vm_id"`
	Tenant    string            `json:"tenant"`
	Kind      string            `json:"kind"`
	State     string            `json:"state"`
	Step      string            `json:"step"`
	Params    map[string]string `json:"params"`
	Holder    string            `json:"-"`
	Error     string            `json:"error,omitempty"`
	CreatedAt int64             `json:"created_at_ms"`
	UpdatedAt int64             `json:"updated_at_ms"`
}

const opCols = `id, vm_id, tenant, kind, state, step, params, holder, error, created_at, updated_at`

func scanOp(r scanner) (*Operation, error) {
	var o Operation
	var params string
	if err := r.Scan(&o.ID, &o.VMID, &o.Tenant, &o.Kind, &o.State, &o.Step, &params, &o.Holder, &o.Error, &o.CreatedAt, &o.UpdatedAt); err != nil {
		return nil, err
	}
	o.Params = map[string]string{}
	json.Unmarshal([]byte(params), &o.Params)
	return &o, nil
}

func (s *Server) getOp(ctx context.Context, id string) (*Operation, error) {
	return scanOp(s.db.QueryRow(ctx, `SELECT `+opCols+` FROM operations WHERE id = ?`, id))
}

func (s *Server) listOps(ctx context.Context, where string, args ...any) ([]*Operation, error) {
	rows, err := s.db.Query(ctx, `SELECT `+opCols+` FROM operations WHERE `+where+` ORDER BY created_at`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Operation
	for rows.Next() {
		o, err := scanOp(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, o)
	}
	return out, rows.Err()
}

// opRun is an operation in progress on this replica.
type opRun struct {
	s    *Server
	op   *Operation
	lock *vmLock
	once sync.Once
}

// beginOp locks the VM (waiting up to wait) and records the operation. Use
// run.ctx() for everything the operation does: it's cancelled if the lock is
// lost, so a replica that was cut off stops touching the VM.
func (s *Server) beginOp(ctx context.Context, vmID, tenant, kind string, wait time.Duration) (*opRun, error) {
	l, err := s.lockVM(ctx, vmID, wait)
	if err != nil {
		return nil, err
	}
	now := nowMs()
	op := &Operation{ID: "op-" + uuid.NewString(), VMID: vmID, Tenant: tenant, Kind: kind, State: OpRunning,
		Params: map[string]string{}, Holder: l.holder, CreatedAt: now, UpdatedAt: now}
	if _, err := s.db.Exec(l.ctx, `INSERT INTO operations (id, vm_id, tenant, kind, state, step, params, holder, error, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, '', '{}', ?, '', ?, ?)`, op.ID, vmID, tenant, kind, OpRunning, l.holder, now, now); err != nil {
		l.Release()
		return nil, err
	}
	if key := idemKeyFrom(ctx); key != "" {
		s.db.Exec(l.ctx, `UPDATE idempotency_keys SET op_id = ? WHERE key = ? AND op_id = ''`, op.ID, key)
	}
	return &opRun{s: s, op: op, lock: l}, nil
}

func (r *opRun) ctx() context.Context { return r.lock.Context() }

// step records progress (and any params) before the action it guards.
func (r *opRun) step(step string, kv ...string) error {
	for i := 0; i+1 < len(kv); i += 2 {
		r.op.Params[kv[i]] = kv[i+1]
	}
	r.op.Step = step
	_, err := r.s.db.Exec(r.ctx(), `UPDATE operations SET step = ?, params = ?, updated_at = ? WHERE id = ? AND holder = ?`,
		step, mustJSON(r.op.Params), nowMs(), r.op.ID, r.op.Holder)
	return err
}

// end records the outcome and releases the VM's lock. Safe to call twice.
func (r *opRun) end(err error) {
	r.once.Do(func() {
		state, msg := OpSucceeded, ""
		if err != nil {
			state, msg = OpFailed, err.Error()
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		// Only while we still hold the lock: a takeover has rewritten holder.
		if _, derr := r.s.db.Exec(ctx, `UPDATE operations SET state = ?, error = ?, updated_at = ? WHERE id = ? AND holder = ?`,
			state, msg, nowMs(), r.op.ID, r.op.Holder); derr != nil {
			log.Printf("operation %s: recording outcome: %v", r.op.ID, derr)
		}
		r.lock.Release()
	})
}

// ── Taking over interrupted operations ────────────────────────────────────

// resumeInterruptedOps (leader) finishes or rolls back operations whose
// replica died. An operation is interrupted when its VM's lock is free while
// the operation is still running.
func (s *Server) resumeInterruptedOps(ctx context.Context) {
	ops, err := s.listOps(ctx, `state = ? AND updated_at < ?`, OpRunning, nowMs()-vmLockRenew.Milliseconds())
	if err != nil {
		return
	}
	var wg sync.WaitGroup
	for _, op := range ops {
		wg.Add(1)
		go func(op *Operation) {
			defer wg.Done()
			s.takeOver(ctx, op)
		}(op)
	}
	wg.Wait()
}

func (s *Server) takeOver(ctx context.Context, stale *Operation) {
	l, err := s.lockVM(ctx, stale.VMID, 0)
	if err != nil {
		return // its replica is still at it
	}
	// It may have finished between the listing and the lock.
	op, err := s.getOp(l.ctx, stale.ID)
	if err != nil || op.State != OpRunning {
		l.Release()
		return
	}
	res, err := s.db.Exec(l.ctx, `UPDATE operations SET holder = ?, updated_at = ? WHERE id = ? AND state = ?`, l.holder, nowMs(), op.ID, OpRunning)
	if n, _ := res.RowsAffected(); err != nil || n != 1 {
		l.Release()
		return
	}
	op.Holder = l.holder
	run := &opRun{s: s, op: op, lock: l}
	log.Printf("taking over interrupted %s of %s (step %q, was %s)", op.Kind, short(op.VMID), op.Step, stale.Holder)
	outcome := s.finishInterrupted(run)
	if outcome != nil {
		outcome = fmt.Errorf("interrupted at %q: %w", op.Step, outcome)
	}
	s.emit(run.ctx(), "operation.recovered", op.Tenant, op.VMID, "", map[string]any{
		"operation_id": op.ID, "kind": op.Kind, "step": op.Step, "vm_id": op.VMID,
		"outcome": map[bool]string{true: "completed", false: "rolled_back"}[outcome == nil]})
	run.end(outcome)
}

// rolledBack is the outcome of an interrupted operation that was undone.
type rolledBack string

func (e rolledBack) Error() string { return "rolled back: " + string(e) }

// finishInterrupted decides, from the operation's last step and what the
// hosts actually have, whether to complete or undo it. nil = completed.
func (s *Server) finishInterrupted(r *opRun) error {
	ctx := r.ctx()
	op := r.op
	v, err := s.getVM(ctx, op.VMID)
	if isNotFound(err) {
		if op.Kind == OpFork {
			s.dropForkChildren(ctx, op)
		}
		return nil // nothing left to fix
	}
	if err != nil {
		return err
	}
	switch op.Kind {
	case OpCreate:
		if st, _ := s.probe(ctx, v.HostID, v.ID); isRunningState(st) {
			return s.commitPlaced(ctx, v)
		}
		s.dropPartial(ctx, v.HostID, v.ID)
		s.deleteVMRow(ctx, v.ID)
		return rolledBack("the VM never started")

	case OpDelete:
		if h, ok := s.hostIfUsable(ctx, v.HostID); ok {
			if err := s.deleteOnHost(ctx, h, v.ID); err != nil {
				return err
			}
		}
		return s.deleteVMRow(ctx, v.ID)

	case OpPause:
		snap := op.Params["snapshot"]
		st, _ := s.probe(ctx, v.HostID, v.ID)
		if op.Step == "paused_on_host" || st == "Paused" {
			if ok, _ := s.remote.Exists(ctx, "manifests/"+snap+".json"); ok {
				h, _ := s.getHost(ctx, v.HostID)
				sn := &Snapshot{ID: snap, Kind: "pause", VMID: v.ID, Tenant: v.Tenant, SourceHost: v.HostID, Uploaded: true}
				if h != nil {
					sn.Backend, sn.HostClass = h.Backend, h.HostClass
				}
				if err := s.insertSnapshot(ctx, sn); err != nil {
					return err
				}
				return s.updateVM(ctx, v.ID, vmUpdate{State: str(VMPaused), PauseID: str(snap)})
			}
			if st == "Paused" {
				s.resumeInPlace(ctx, v)
			}
		}
		return rolledBack("the VM was left running")

	case OpResume, OpMigrate:
		src := op.Params["source_host"]
		if op.Step == "restoring" {
			if v.HostID != src {
				if st, _ := s.probe(ctx, v.HostID, v.ID); isRunningState(st) {
					if h, ok := s.hostIfUsable(ctx, src); ok && src != "" {
						s.deleteOnHost(ctx, h, v.ID)
					}
					if err := s.commitPlaced(ctx, v); err != nil {
						return err
					}
					s.updateVM(ctx, v.ID, vmUpdate{PauseID: str("")})
					s.emit(ctx, "vm.moved", v.Tenant, v.ID, v.HostID, map[string]any{"vm_id": v.ID, "from_host": src,
						"to_host": v.HostID, "reason": op.Params["reason"]})
					return nil
				}
				s.dropPartial(ctx, v.HostID, v.ID)
			}
			// Back where it was: paused there, with its snapshot.
			if err := s.updateVM(ctx, v.ID, vmUpdate{HostID: str(src), State: str(VMPaused), PauseID: str(op.Params["snapshot"])}); err != nil {
				return err
			}
			v.HostID, v.State = src, VMPaused
		}
		if op.Kind == OpMigrate {
			// Interrupted while pausing or restoring: it was running before, so
			// it runs again on its source.
			if st, _ := s.probe(ctx, v.HostID, v.ID); st == "Paused" {
				s.resumeInPlace(ctx, v)
			}
			return rolledBack("the VM stayed on its host")
		}
		// A resume interrupted on the VM's own host may have gone through.
		if st, _ := s.probe(ctx, v.HostID, v.ID); isRunningState(st) {
			if err := s.commitPlaced(ctx, v); err != nil {
				return err
			}
			return s.updateVM(ctx, v.ID, vmUpdate{PauseID: str("")})
		}
		return rolledBack("the VM is still paused")

	case OpRecover:
		if st, _ := s.probe(ctx, v.HostID, v.ID); isRunningState(st) {
			return s.commitPlaced(ctx, v)
		}
		s.dropPartial(ctx, v.HostID, v.ID)
		s.updateVM(ctx, v.ID, vmUpdate{State: str(VMRecovering), HostID: str("")})
		return rolledBack("recovery will be retried")

	case OpFork:
		s.dropForkChildren(ctx, op)
		return rolledBack("forks are all-or-nothing")

	default: // checkpoint: the VM kept running; an unrecorded snapshot is garbage-collected
		return rolledBack("nothing to undo")
	}
}

func isRunningState(st string) bool { return st == "Started" || st == "Running" }

// probe asks a host for its view of a VM: its state, "" if the host doesn't
// have it; ok=false if the host couldn't be asked.
func (s *Server) probe(ctx context.Context, hostID, vmID string) (string, bool) {
	h, ok := s.hostIfUsable(ctx, hostID)
	if !ok {
		return "", false
	}
	info, err := s.getOnHost(ctx, h, vmID)
	if isAgentNotFound(err) {
		return "", true
	}
	if err != nil {
		return "", false
	}
	st, _ := info["state"].(string)
	return st, true
}

// commitPlaced records a VM as running on the host it was being placed on.
func (s *Server) commitPlaced(ctx context.Context, v *VM) error {
	upd := vmUpdate{State: str(VMRunning), Error: str("")}
	if h, ok := s.hostIfUsable(ctx, v.HostID); ok {
		if info, err := s.getOnHost(ctx, h, v.ID); err == nil {
			upd.Info = info
		}
	}
	return s.updateVM(ctx, v.ID, upd)
}

func (s *Server) dropPartial(ctx context.Context, hostID, vmID string) {
	if h, ok := s.hostIfUsable(ctx, hostID); ok {
		s.deleteOnHost(ctx, h, vmID)
	}
}

func (s *Server) resumeInPlace(ctx context.Context, v *VM) {
	h, ok := s.hostIfUsable(ctx, v.HostID)
	if !ok {
		return
	}
	if info, err := s.resumeOnHost(ctx, h, v.ID); err == nil {
		s.updateVM(ctx, v.ID, vmUpdate{State: str(VMRunning), Info: info})
	}
}

func (s *Server) dropForkChildren(ctx context.Context, op *Operation) {
	for _, id := range strings.Split(op.Params["children"], ",") {
		if id == "" {
			continue
		}
		if c, err := s.getVM(ctx, id); err == nil {
			s.dropPartial(ctx, c.HostID, c.ID)
			s.deleteVMRow(ctx, c.ID)
		}
	}
}

// pruneOps forgets finished operations, idempotency keys and VM locks after a day.
func (s *Server) pruneOps(ctx context.Context) {
	cutoff := nowMs() - (24 * time.Hour).Milliseconds()
	s.db.Exec(ctx, `DELETE FROM operations WHERE state <> ? AND updated_at < ?`, OpRunning, cutoff)
	s.db.Exec(ctx, `DELETE FROM idempotency_keys WHERE created_at < ?`, cutoff)
	s.db.Exec(ctx, `DELETE FROM leases WHERE name LIKE 'vm:%' AND expires_at < ?`, nowMs()-time.Hour.Milliseconds())
}
