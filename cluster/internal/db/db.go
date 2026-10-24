// Package db is the control plane's store: Postgres for a cluster, SQLite for
// a single node. Queries are written once with ? placeholders.
package db

import (
	"context"
	"database/sql"
	"fmt"
	"strconv"
	"strings"

	_ "github.com/jackc/pgx/v5/stdlib"
	_ "modernc.org/sqlite"
)

// DB wraps database/sql with placeholder rebinding.
type DB struct {
	sql     *sql.DB
	Dialect string // "postgres" | "sqlite"
}

// Open accepts postgres://... / postgresql://... or sqlite:///path/to/file.db.
func Open(url string) (*DB, error) {
	switch {
	case strings.HasPrefix(url, "postgres://"), strings.HasPrefix(url, "postgresql://"):
		s, err := sql.Open("pgx", url)
		if err != nil {
			return nil, err
		}
		s.SetMaxOpenConns(20)
		return &DB{sql: s, Dialect: "postgres"}, nil
	case strings.HasPrefix(url, "sqlite://"):
		path := strings.TrimPrefix(url, "sqlite://")
		s, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(10000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(1)")
		if err != nil {
			return nil, err
		}
		s.SetMaxOpenConns(1) // one writer; avoids SQLITE_BUSY between our own goroutines
		return &DB{sql: s, Dialect: "sqlite"}, nil
	default:
		return nil, fmt.Errorf("database url must start with postgres:// or sqlite://")
	}
}

func (d *DB) Close() error { return d.sql.Close() }

// rebind turns ? into $1, $2, ... for Postgres.
func (d *DB) rebind(q string) string {
	if d.Dialect != "postgres" {
		return q
	}
	var b strings.Builder
	n := 0
	for _, c := range q {
		if c == '?' {
			n++
			b.WriteString("$" + strconv.Itoa(n))
			continue
		}
		b.WriteRune(c)
	}
	return b.String()
}

func (d *DB) Exec(ctx context.Context, q string, args ...any) (sql.Result, error) {
	return d.sql.ExecContext(ctx, d.rebind(q), args...)
}

func (d *DB) Query(ctx context.Context, q string, args ...any) (*sql.Rows, error) {
	return d.sql.QueryContext(ctx, d.rebind(q), args...)
}

func (d *DB) QueryRow(ctx context.Context, q string, args ...any) *sql.Row {
	return d.sql.QueryRowContext(ctx, d.rebind(q), args...)
}

// Tx runs fn in a transaction.
func (d *DB) Tx(ctx context.Context, fn func(tx *Tx) error) error {
	t, err := d.sql.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	if err := fn(&Tx{t: t, d: d}); err != nil {
		t.Rollback()
		return err
	}
	return t.Commit()
}

// Tx is a transaction with the same placeholder rebinding.
type Tx struct {
	t *sql.Tx
	d *DB
}

func (t *Tx) Exec(ctx context.Context, q string, args ...any) (sql.Result, error) {
	return t.t.ExecContext(ctx, t.d.rebind(q), args...)
}

func (t *Tx) QueryRow(ctx context.Context, q string, args ...any) *sql.Row {
	return t.t.QueryRowContext(ctx, t.d.rebind(q), args...)
}

func (t *Tx) Query(ctx context.Context, q string, args ...any) (*sql.Rows, error) {
	return t.t.QueryContext(ctx, t.d.rebind(q), args...)
}

const schema = `
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    tenants     TEXT NOT NULL DEFAULT '[]',
    name        TEXT NOT NULL DEFAULT '',
    created_at  BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS sso_codes (
    code_hash  TEXT PRIMARY KEY,
    grant_json TEXT NOT NULL,
    challenge  TEXT NOT NULL,
    expires_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS join_tokens (
    id          TEXT PRIMARY KEY,
    secret_hash TEXT NOT NULL,
    zone        TEXT NOT NULL DEFAULT '',
    labels      TEXT NOT NULL DEFAULT '{}',
    expires_at  BIGINT NOT NULL,
    used_at     BIGINT,
    host_id     TEXT,
    created_at  BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS hosts (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    agent_url      TEXT NOT NULL,
    snapd_url      TEXT NOT NULL,
    zone           TEXT NOT NULL DEFAULT '',
    labels         TEXT NOT NULL DEFAULT '{}',
    backend        TEXT NOT NULL DEFAULT '',
    host_class     TEXT NOT NULL DEFAULT '',
    state          TEXT NOT NULL,
    cordoned       INTEGER NOT NULL DEFAULT 0,
    capacity       TEXT NOT NULL DEFAULT '{}',
    cached         TEXT NOT NULL DEFAULT '[]',
    templates      TEXT NOT NULL DEFAULT '[]',
    version        TEXT NOT NULL DEFAULT '',
    last_heartbeat BIGINT NOT NULL DEFAULT 0,
    state_since    BIGINT NOT NULL DEFAULT 0,
    max_vms        INTEGER NOT NULL DEFAULT 0,
    created_at     BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS vms (
    id            TEXT PRIMARY KEY,
    tenant        TEXT NOT NULL,
    host_id       TEXT,
    state         TEXT NOT NULL,
    template_id   TEXT NOT NULL,
    availability  TEXT NOT NULL,
    spec          TEXT NOT NULL DEFAULT '{}',
    labels        TEXT NOT NULL DEFAULT '{}',
    vmset_id      TEXT,
    role          TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT,
    checkpoint_at BIGINT NOT NULL DEFAULT 0,
    pause_id      TEXT,
    error         TEXT NOT NULL DEFAULT '',
    info          TEXT NOT NULL DEFAULT '{}',
    created_at    BIGINT NOT NULL,
    updated_at    BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS vms_host ON vms (host_id);
CREATE INDEX IF NOT EXISTS vms_tenant ON vms (tenant);
CREATE INDEX IF NOT EXISTS vms_vmset ON vms (vmset_id);
CREATE TABLE IF NOT EXISTS snapshots (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    vm_id       TEXT,
    template_id TEXT,
    tenant      TEXT NOT NULL DEFAULT '',
    backend     TEXT NOT NULL,
    host_class  TEXT NOT NULL,
    source_host TEXT,
    uploaded    INTEGER NOT NULL DEFAULT 0,
    bytes       BIGINT NOT NULL DEFAULT 0,
    created_at  BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_vm ON snapshots (vm_id);
CREATE TABLE IF NOT EXISTS templates (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    tenant     TEXT NOT NULL DEFAULT '',
    dockerfile TEXT NOT NULL DEFAULT '',
    rootfs_mb  INTEGER NOT NULL DEFAULT 500,
    status     TEXT NOT NULL,
    build_host TEXT,
    error      TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS vmsets (
    id         TEXT PRIMARY KEY,
    tenant     TEXT NOT NULL,
    name       TEXT NOT NULL,
    spec       TEXT NOT NULL,
    generation BIGINT NOT NULL DEFAULT 1,
    deleted    INTEGER NOT NULL DEFAULT 0,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenant_quotas (
    tenant     TEXT PRIMARY KEY,
    max_vms    INTEGER NOT NULL DEFAULT 0,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
    name       TEXT PRIMARY KEY,
    holder     TEXT NOT NULL,
    expires_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    id              TEXT PRIMARY KEY,
    vm_id           TEXT NOT NULL,
    tenant          TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL,
    state           TEXT NOT NULL,
    step            TEXT NOT NULL DEFAULT '',
    params          TEXT NOT NULL DEFAULT '{}',
    holder          TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    created_at      BIGINT NOT NULL,
    updated_at      BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS operations_state ON operations (state);
CREATE INDEX IF NOT EXISTS operations_vm ON operations (vm_id);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key             TEXT PRIMARY KEY,
    request_hash    TEXT NOT NULL,
    op_id           TEXT NOT NULL DEFAULT '',
    response_status INTEGER NOT NULL DEFAULT 0,
    response_body   TEXT NOT NULL DEFAULT '',
    created_at      BIGINT NOT NULL
);
`

func (d *DB) eventsTable() string {
	if d.Dialect == "postgres" {
		return `CREATE TABLE IF NOT EXISTS events (
    seq     BIGSERIAL PRIMARY KEY,
    ts      BIGINT NOT NULL,
    type    TEXT NOT NULL,
    tenant  TEXT NOT NULL DEFAULT '',
    vm_id   TEXT NOT NULL DEFAULT '',
    host_id TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL
);`
	}
	return `CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      BIGINT NOT NULL,
    type    TEXT NOT NULL,
    tenant  TEXT NOT NULL DEFAULT '',
    vm_id   TEXT NOT NULL DEFAULT '',
    host_id TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL
);`
}

// addedColumns are columns added after a table was first released; Migrate
// adds them to existing databases.
var addedColumns = []struct{ table, column, def string }{
	{"hosts", "max_vms", "INTEGER NOT NULL DEFAULT 0"},
	{"tokens", "expires_at", "BIGINT"},                 // NULL: until revoked; set for SSO sessions
	{"tokens", "identity", "TEXT NOT NULL DEFAULT ''"}, // who signed in, for SSO sessions
}

func (d *DB) addColumns(ctx context.Context, exec func(context.Context, string, ...any) (sql.Result, error)) error {
	for _, c := range addedColumns {
		if d.Dialect == "postgres" {
			if _, err := exec(ctx, fmt.Sprintf(`ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s`, c.table, c.column, c.def)); err != nil {
				return err
			}
			continue
		}
		if _, err := exec(ctx, fmt.Sprintf(`ALTER TABLE %s ADD COLUMN %s %s`, c.table, c.column, c.def)); err != nil &&
			!strings.Contains(err.Error(), "duplicate column") {
			return err
		}
	}
	return nil
}

// Migrate creates the schema. Safe to run from every replica at once.
func (d *DB) Migrate(ctx context.Context) error {
	if d.Dialect == "postgres" {
		// Serialize concurrent migrations from several replicas.
		conn, err := d.sql.Conn(ctx)
		if err != nil {
			return err
		}
		defer conn.Close()
		if _, err := conn.ExecContext(ctx, "SELECT pg_advisory_lock(74656)"); err != nil {
			return err
		}
		defer conn.ExecContext(context.Background(), "SELECT pg_advisory_unlock(74656)")
		for _, stmt := range splitStatements(schema + d.eventsTable()) {
			if _, err := conn.ExecContext(ctx, stmt); err != nil {
				return fmt.Errorf("migrate: %w\n%s", err, stmt)
			}
		}
		return d.addColumns(ctx, conn.ExecContext)
	}
	for _, stmt := range splitStatements(schema + d.eventsTable()) {
		if _, err := d.sql.ExecContext(ctx, stmt); err != nil {
			return fmt.Errorf("migrate: %w\n%s", err, stmt)
		}
	}
	return d.addColumns(ctx, d.sql.ExecContext)
}

func splitStatements(s string) []string {
	var out []string
	for _, stmt := range strings.Split(s, ";") {
		if strings.TrimSpace(stmt) != "" {
			out = append(out, stmt)
		}
	}
	return out
}

// TryLease takes or renews a named lease for holder until expiresAt (unix
// ms). Returns true if holder now has it. Used for reconciler leadership.
func (d *DB) TryLease(ctx context.Context, name, holder string, now, expiresAt int64) (bool, error) {
	res, err := d.Exec(ctx, `UPDATE leases SET holder = ?, expires_at = ? WHERE name = ? AND (holder = ? OR expires_at < ?)`,
		holder, expiresAt, name, holder, now)
	if err != nil {
		return false, err
	}
	if n, _ := res.RowsAffected(); n == 1 {
		return true, nil
	}
	_, err = d.Exec(ctx, `INSERT INTO leases (name, holder, expires_at) VALUES (?, ?, ?) ON CONFLICT (name) DO NOTHING`, name, holder, expiresAt)
	if err != nil {
		return false, err
	}
	var h string
	if err := d.QueryRow(ctx, `SELECT holder FROM leases WHERE name = ?`, name).Scan(&h); err != nil {
		return false, err
	}
	return h == holder, nil
}

// RenewLease extends a lease holder still has. Returns false if it lost it.
func (d *DB) RenewLease(ctx context.Context, name, holder string, expiresAt int64) (bool, error) {
	res, err := d.Exec(ctx, `UPDATE leases SET expires_at = ? WHERE name = ? AND holder = ?`, expiresAt, name, holder)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n == 1, nil
}

// ReleaseLease gives a lease up if holder still has it.
func (d *DB) ReleaseLease(ctx context.Context, name, holder string) error {
	_, err := d.Exec(ctx, `DELETE FROM leases WHERE name = ? AND holder = ?`, name, holder)
	return err
}

// SetMetaIfAbsent stores a value unless one exists; returns the stored value.
func (d *DB) SetMetaIfAbsent(ctx context.Context, key, value string) (string, error) {
	if _, err := d.Exec(ctx, `INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING`, key, value); err != nil {
		return "", err
	}
	var v string
	err := d.QueryRow(ctx, `SELECT value FROM meta WHERE key = ?`, key).Scan(&v)
	return v, err
}
