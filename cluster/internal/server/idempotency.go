package server

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"strings"
	"time"
)

// Mutating VM requests accept an Idempotency-Key header. A retry with the
// same key (from the same API token) gets the first request's answer instead
// of doing the work twice, even if it lands on another replica, and even if
// the replica that handled the first one died: the key is tied to the
// operation it started, and the answer is rebuilt from that operation's
// outcome once the leader has finished it.

const idemHeader = "Idempotency-Key"

type idemCtxKey struct{}

func idemKeyFrom(ctx context.Context) string {
	k, _ := ctx.Value(idemCtxKey{}).(string)
	return k
}

// recorder captures a handler's answer so it can be replayed.
type recorder struct {
	http.ResponseWriter
	status int
	body   bytes.Buffer
}

func (r *recorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (r *recorder) Write(b []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	r.body.Write(b)
	return r.ResponseWriter.Write(b)
}

// idempotent wraps a mutating handler (after auth).
func (s *Server) idempotent(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		raw := strings.TrimSpace(r.Header.Get(idemHeader))
		if raw == "" {
			h(w, r)
			return
		}
		if len(raw) > 255 {
			writeErr(w, 400, idemHeader+" must be at most 255 characters")
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
		if err != nil {
			writeErr(w, 400, "request body too large")
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		sum := sha256.Sum256([]byte(r.Method + " " + r.URL.Path + "?" + r.URL.RawQuery + "\n" + string(body)))
		hash := hex.EncodeToString(sum[:])
		key := principal(r).TokenID + ":" + raw
		ctx := r.Context()

		res, err := s.db.Exec(ctx, `INSERT INTO idempotency_keys (key, request_hash, created_at) VALUES (?, ?, ?) ON CONFLICT (key) DO NOTHING`,
			key, hash, nowMs())
		if err != nil {
			writeErr(w, 503, "idempotency store unavailable: "+err.Error())
			return
		}
		if n, _ := res.RowsAffected(); n == 0 {
			if s.replay(w, r, key, hash) {
				return
			}
			// A first attempt died before it started any operation: run it again.
			res, err := s.db.Exec(ctx, `UPDATE idempotency_keys SET created_at = ? WHERE key = ? AND op_id = '' AND response_status = 0 AND created_at < ?`,
				nowMs(), key, nowMs()-(30*time.Second).Milliseconds())
			if n, _ := res.RowsAffected(); err != nil || n != 1 {
				writeErr(w, 409, "a request with this "+idemHeader+" is still in progress")
				return
			}
		}
		rec := &recorder{ResponseWriter: w}
		h(rec, r.WithContext(context.WithValue(ctx, idemCtxKey{}, key)))
		status := rec.status
		if status == 0 {
			status = http.StatusOK
		}
		// 409 "busy" and 5xx are worth retrying with the same key; don't pin them.
		if status == http.StatusConflict && strings.Contains(rec.body.String(), errVMBusy.Detail) || status >= 500 {
			s.db.Exec(context.WithoutCancel(ctx), `DELETE FROM idempotency_keys WHERE key = ?`, key)
			return
		}
		s.db.Exec(context.WithoutCancel(ctx), `UPDATE idempotency_keys SET response_status = ?, response_body = ? WHERE key = ?`,
			status, rec.body.String(), key)
	}
}

// replay answers a repeated key. False if the first attempt left nothing to
// answer with (it died before starting an operation).
func (s *Server) replay(w http.ResponseWriter, r *http.Request, key, hash string) bool {
	ctx := r.Context()
	var storedHash, opID, body string
	var status int
	if err := s.db.QueryRow(ctx, `SELECT request_hash, op_id, response_status, response_body FROM idempotency_keys WHERE key = ?`, key).
		Scan(&storedHash, &opID, &status, &body); err != nil {
		writeErr(w, 409, "a request with this "+idemHeader+" is still in progress")
		return true
	}
	if storedHash != hash {
		writeErr(w, http.StatusUnprocessableEntity, idemHeader+" was already used for a different request")
		return true
	}
	if status != 0 {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Idempotent-Replayed", "true")
		w.WriteHeader(status)
		w.Write([]byte(body))
		return true
	}
	if opID == "" {
		return false
	}
	op, err := s.getOp(ctx, opID)
	if err != nil || op.State == OpRunning {
		writeErr(w, 409, "a request with this "+idemHeader+" is still in progress")
		return true
	}
	// The replica that ran it died; answer from the operation's outcome.
	w.Header().Set("Idempotent-Replayed", "true")
	if op.State == OpFailed {
		writeErr(w, 409, "the operation was interrupted: "+op.Error)
		return true
	}
	writeJSON(w, 200, s.opResult(ctx, op))
	return true
}

// opResult rebuilds the answer of a completed operation.
func (s *Server) opResult(ctx context.Context, op *Operation) map[string]any {
	switch op.Kind {
	case OpDelete, OpPause:
		return map[string]any{"ok": true}
	case OpFork:
		out := []map[string]any{}
		for _, id := range strings.Split(op.Params["children"], ",") {
			if c, err := s.getVM(ctx, id); err == nil {
				out = append(out, s.vmDict(c, nil))
			}
		}
		return map[string]any{"vms": out}
	}
	v, err := s.getVM(ctx, op.VMID)
	if err != nil {
		return map[string]any{"ok": true}
	}
	d := map[string]any{"vm": s.vmDict(v, nil)}
	if op.Kind == OpCheckpoint {
		d["snapshot_id"] = v.CheckpointID
	}
	return d
}
