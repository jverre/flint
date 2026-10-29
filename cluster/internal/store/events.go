package store

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strconv"
	"strings"

	"github.com/jverre/flint/cluster/internal/db"
)

// Event is a cluster event as stored: indexed columns plus its payload.
type Event struct {
	TS        int64 // unix ms
	Type      string
	Tenant    string
	VMID      string
	HostID    string
	Actor     string
	ActorName string
	Payload   map[string]any
}

func (s *Store) InsertEvent(ctx context.Context, e Event) error {
	_, err := s.db.Exec(ctx, `INSERT INTO events (ts, type, tenant, vm_id, host_id, actor, actor_name, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		e.TS, e.Type, e.Tenant, e.VMID, e.HostID, e.Actor, e.ActorName, mustJSON(e.Payload))
	return err
}

// StoredEvent is an event read back, with its cluster-wide sequence number.
type StoredEvent struct {
	Seq     int64
	Tenant  string
	Payload json.RawMessage
}

func scanEvent(r scanner) (StoredEvent, error) {
	var e StoredEvent
	var p string
	err := r.Scan(&e.Seq, &e.Tenant, &p)
	e.Payload = json.RawMessage(p)
	return e, err
}

// EventQuery selects events after Since, oldest first; zero fields don't filter.
type EventQuery struct {
	Since   int64
	Limit   int
	Tenants []string // any of these tenants
	Tenant  string
	VMID    string
	Type    string
	Actor   string
}

func (s *Store) ListEvents(ctx context.Context, q EventQuery) ([]StoredEvent, error) {
	var w where
	w.add("seq > ?", q.Since)
	w.in("tenant", q.Tenants)
	for _, f := range []struct{ col, val string }{{"vm_id", q.VMID}, {"tenant", q.Tenant}, {"type", q.Type}, {"actor", q.Actor}} {
		if f.val != "" {
			w.add(f.col+" = ?", f.val)
		}
	}
	return queryAll(ctx, s, scanEvent, `SELECT seq, tenant, payload FROM events`+w.String()+` ORDER BY seq LIMIT ?`, append(w.args, q.Limit)...)
}

func (s *Store) LastEventSeq(ctx context.Context) (int64, error) {
	var seq int64
	err := s.db.QueryRow(ctx, `SELECT COALESCE(MAX(seq), 0) FROM events`).Scan(&seq)
	return seq, err
}

// ── Audit log ─────────────────────────────────────────────────────────────
//
// Every API call that changes something (and every sign-in) is appended to
// a hash-chained log, like the single daemon's (src/flint/daemon/eventlog.py):
//
//	hash = sha256(prev_hash, seq, ts_ms, actor, actor_name, method, path,
//	              status, tenant, request_id)   // joined by \x1f, UTF-8
//
// The first entry's prev_hash is 64 zeros. Request bodies are never recorded.
// Replicas append under a database lock, so the chain has no gaps or forks.

const GenesisHash = "0000000000000000000000000000000000000000000000000000000000000000"

type AuditEntry struct {
	Seq       int64  `json:"seq"`
	TsMs      int64  `json:"ts_ms"`
	Actor     string `json:"actor"`
	ActorName string `json:"actor_name"`
	Method    string `json:"method"`
	Path      string `json:"path"`
	Status    int    `json:"status"`
	Tenant    string `json:"tenant"`
	RequestID string `json:"request_id"`
	PrevHash  string `json:"prev_hash"`
	Hash      string `json:"hash"`
}

func (e AuditEntry) chainHash() string {
	parts := []string{e.PrevHash, strconv.FormatInt(e.Seq, 10), strconv.FormatInt(e.TsMs, 10), e.Actor, e.ActorName,
		e.Method, e.Path, strconv.Itoa(e.Status), e.Tenant, e.RequestID}
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x1f")))
	return hex.EncodeToString(sum[:])
}

// AppendAudit adds e to the chain, filling in seq, ts and the hashes.
func (s *Store) AppendAudit(ctx context.Context, e AuditEntry, now int64) error {
	return s.db.Tx(ctx, func(tx *db.Tx) error {
		// Serialize appends: take the write lock before reading the chain's end.
		lock := `SELECT pg_advisory_xact_lock(74657)`
		if s.db.Dialect != "postgres" {
			// SQLite can't upgrade a read lock while another writer waits; writing first waits instead.
			lock = `INSERT INTO meta (key, value) VALUES ('audit_lock', '') ON CONFLICT (key) DO UPDATE SET value = ''`
		}
		if _, err := tx.Exec(ctx, lock); err != nil {
			return err
		}
		var prevSeq int64
		prevHash := GenesisHash
		err := tx.QueryRow(ctx, `SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1`).Scan(&prevSeq, &prevHash)
		if err != nil && !IsNotFound(err) {
			return err
		}
		e.Seq, e.TsMs, e.PrevHash = prevSeq+1, now, prevHash
		e.Hash = e.chainHash()
		_, err = tx.Exec(ctx, `INSERT INTO audit (seq, ts_ms, actor, actor_name, method, path, status, tenant, request_id, prev_hash, hash)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			e.Seq, e.TsMs, e.Actor, e.ActorName, e.Method, e.Path, e.Status, e.Tenant, e.RequestID, e.PrevHash, e.Hash)
		return err
	})
}

// AuditEntries returns up to limit entries after seq since.
func (s *Store) AuditEntries(ctx context.Context, since int64, limit int) ([]AuditEntry, error) {
	out, err := queryAll(ctx, s, func(r scanner) (AuditEntry, error) {
		var e AuditEntry
		return e, r.Scan(&e.Seq, &e.TsMs, &e.Actor, &e.ActorName, &e.Method, &e.Path, &e.Status, &e.Tenant, &e.RequestID, &e.PrevHash, &e.Hash)
	}, `SELECT seq, ts_ms, actor, actor_name, method, path, status, tenant, request_id, prev_hash, hash
		FROM audit WHERE seq > ? ORDER BY seq LIMIT ?`, since, limit)
	if out == nil {
		out = []AuditEntry{}
	}
	return out, err
}

// AuditCheck is the outcome of verifying the chain.
type AuditCheck struct {
	OK       bool   `json:"ok"`
	Count    int    `json:"count"`
	BrokenAt int64  `json:"broken_at,omitempty"`
	Why      string `json:"why,omitempty"`
	LastHash string `json:"last_hash,omitempty"`
}

// VerifyAudit walks the whole chain checking every link and hash.
func (s *Store) VerifyAudit(ctx context.Context) (*AuditCheck, error) {
	const batch = 5000
	var prev *AuditEntry
	count := 0
	var since int64
	for {
		es, err := s.AuditEntries(ctx, since, batch)
		if err != nil {
			return nil, err
		}
		for i := range es {
			e := es[i]
			if prev != nil && (e.PrevHash != prev.Hash || e.Seq != prev.Seq+1) {
				return &AuditCheck{Count: count, BrokenAt: e.Seq, Why: "doesn't follow the entry before it"}, nil
			}
			if e.chainHash() != e.Hash {
				return &AuditCheck{Count: count, BrokenAt: e.Seq, Why: "hash doesn't match its fields"}, nil
			}
			prev, count = &e, count+1
		}
		if len(es) < batch {
			last := GenesisHash
			if prev != nil {
				last = prev.Hash
			}
			return &AuditCheck{OK: true, Count: count, LastHash: last}, nil
		}
		since = es[len(es)-1].Seq
	}
}
