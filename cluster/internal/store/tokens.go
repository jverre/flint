package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"

	"github.com/jverre/flint/cluster/internal/db"
	"github.com/jverre/flint/cluster/internal/model"
)

// Token is an API credential (or SSO session); only its secret's hash is kept.
type Token struct {
	ID         string
	Kind       string
	SecretHash string
	Tenants    []string
	Name       string
	Identity   string // who signed in, for an SSO session
	CreatedAt  int64
	ExpiresAt  int64 // 0: until revoked
}

func (s *Store) InsertToken(ctx context.Context, t *Token) error {
	if t.Tenants == nil {
		t.Tenants = []string{}
	}
	if t.CreatedAt == 0 {
		t.CreatedAt = model.NowMs()
	}
	var expires any
	if t.ExpiresAt != 0 {
		expires = t.ExpiresAt
	}
	_, err := s.db.Exec(ctx, `INSERT INTO tokens (id, kind, secret_hash, tenants, name, created_at, expires_at, identity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		t.ID, t.Kind, t.SecretHash, mustJSON(t.Tenants), t.Name, t.CreatedAt, expires, t.Identity)
	return err
}

// PutBootstrapToken stores (or rotates) a credential given at startup.
func (s *Store) PutBootstrapToken(ctx context.Context, id, kind, secretHash string) error {
	_, err := s.db.Exec(ctx, `INSERT INTO tokens (id, kind, secret_hash, tenants, name, created_at) VALUES (?, ?, ?, '[]', 'bootstrap', ?)
		ON CONFLICT (id) DO UPDATE SET secret_hash = excluded.secret_hash, kind = excluded.kind`, id, kind, secretHash, model.NowMs())
	return err
}

const tokenCols = `id, kind, secret_hash, tenants, name, identity, created_at, expires_at`

func scanToken(r scanner) (*Token, error) {
	var t Token
	var tenants string
	var expires sql.NullInt64
	if err := r.Scan(&t.ID, &t.Kind, &t.SecretHash, &tenants, &t.Name, &t.Identity, &t.CreatedAt, &expires); err != nil {
		return nil, err
	}
	json.Unmarshal([]byte(tenants), &t.Tenants)
	t.ExpiresAt = expires.Int64
	return &t, nil
}

func (s *Store) GetToken(ctx context.Context, id string) (*Token, error) {
	return scanToken(s.db.QueryRow(ctx, `SELECT `+tokenCols+` FROM tokens WHERE id = ?`, id))
}

// ListTokens returns the tokens that haven't expired at now.
func (s *Store) ListTokens(ctx context.Context, now int64) ([]*Token, error) {
	return queryAll(ctx, s, scanToken, `SELECT `+tokenCols+` FROM tokens WHERE expires_at IS NULL OR expires_at > ? ORDER BY created_at`, now)
}

func (s *Store) DeleteToken(ctx context.Context, id string) error {
	_, err := s.db.Exec(ctx, `DELETE FROM tokens WHERE id = ?`, id)
	return err
}

func (s *Store) DeleteExpiredTokens(ctx context.Context, now int64) error {
	_, err := s.db.Exec(ctx, `DELETE FROM tokens WHERE expires_at IS NOT NULL AND expires_at < ?`, now)
	return err
}

// ── Host join tokens ──────────────────────────────────────────────────────

func (s *Store) InsertJoinToken(ctx context.Context, id, secretHash, zone string, labels map[string]string, expiresAt int64) error {
	if labels == nil {
		labels = map[string]string{}
	}
	_, err := s.db.Exec(ctx, `INSERT INTO join_tokens (id, secret_hash, zone, labels, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)`,
		id, secretHash, zone, mustJSON(labels), expiresAt, model.NowMs())
	return err
}

// ErrJoinTokenInvalid: the join token is unknown, wrong, expired or used.
var ErrJoinTokenInvalid = errors.New("join token is invalid, expired or already used")

// UseJoinToken spends a single-use join token for hostID and returns the
// zone and labels it gives the host. Only one joiner can spend it.
func (s *Store) UseJoinToken(ctx context.Context, id, secretHash, hostID string) (zone string, labels map[string]string, err error) {
	err = s.db.Tx(ctx, func(tx *db.Tx) error {
		var hash, lbl string
		var expires int64
		err := tx.QueryRow(ctx, `SELECT secret_hash, zone, labels, expires_at FROM join_tokens WHERE id = ? AND used_at IS NULL`, id).
			Scan(&hash, &zone, &lbl, &expires)
		if IsNotFound(err) || err == nil && (hash != secretHash || model.NowMs() > expires) {
			return ErrJoinTokenInvalid
		}
		if err != nil {
			return err
		}
		k, err := affected(tx.Exec(ctx, `UPDATE join_tokens SET used_at = ?, host_id = ? WHERE id = ? AND used_at IS NULL`, model.NowMs(), hostID, id))
		if err != nil {
			return err
		}
		if k != 1 {
			return ErrJoinTokenInvalid
		}
		labels = map[string]string{}
		json.Unmarshal([]byte(lbl), &labels)
		return nil
	})
	return zone, labels, err
}

// ── SSO one-time codes (the CLI's sign-in) ────────────────────────────────

// InsertSSOCode stores a one-time code (dropping expired ones first).
func (s *Store) InsertSSOCode(ctx context.Context, codeHash, grant, challenge string, expiresAt int64) error {
	if _, err := s.db.Exec(ctx, `DELETE FROM sso_codes WHERE expires_at < ?`, model.NowMs()); err != nil {
		return err
	}
	_, err := s.db.Exec(ctx, `INSERT INTO sso_codes (code_hash, grant_json, challenge, expires_at) VALUES (?, ?, ?, ?)`,
		codeHash, grant, challenge, expiresAt)
	return err
}

// SSOCode is a redeemed one-time code.
type SSOCode struct {
	Grant     string
	Challenge string
	ExpiresAt int64
}

// TakeSSOCode redeems a code: one try per code, whoever wins the race to
// delete it. ErrNotFound if it doesn't exist or was already taken.
func (s *Store) TakeSSOCode(ctx context.Context, codeHash string) (*SSOCode, error) {
	var c SSOCode
	if err := s.db.QueryRow(ctx, `SELECT grant_json, challenge, expires_at FROM sso_codes WHERE code_hash = ?`, codeHash).
		Scan(&c.Grant, &c.Challenge, &c.ExpiresAt); err != nil {
		return nil, err
	}
	k, err := affected(s.db.Exec(ctx, `DELETE FROM sso_codes WHERE code_hash = ?`, codeHash))
	if err != nil {
		return nil, err
	}
	if k != 1 {
		return nil, ErrNotFound
	}
	return &c, nil
}
