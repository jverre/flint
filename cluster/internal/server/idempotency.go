package server

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/jverre/flint/cluster/internal/auth"
	"github.com/jverre/flint/cluster/internal/engine"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
)

// Mutating VM requests accept an Idempotency-Key header. A retry with the
// same key (from the same API token) gets the first request's answer instead
// of doing the work twice, even if it lands on another replica, and even if
// the replica that handled the first one died: the key is tied to the
// operation it started, and the answer is rebuilt from that operation's
// outcome once the leader has finished it.

const idemHeader = "Idempotency-Key"

var errIdemInProgress = httpx.Conflict("a request with this " + idemHeader + " is still in progress")

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
			httpx.WriteErr(w, httpx.BadRequest(idemHeader+" must be at most 255 characters"))
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
		if err != nil {
			httpx.WriteErr(w, httpx.BadRequest("request body too large"))
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		sum := sha256.Sum256([]byte(r.Method + " " + r.URL.Path + "?" + r.URL.RawQuery + "\n" + string(body)))
		hash := hex.EncodeToString(sum[:])
		key := auth.FromContext(r.Context()).TokenID + ":" + raw
		ctx := r.Context()

		first, err := s.store.ClaimIdempotencyKey(ctx, key, hash)
		if err != nil {
			httpx.WriteErr(w, httpx.Unavailable("idempotency store unavailable: "+err.Error()))
			return
		}
		if !first {
			if s.replay(w, r, key, hash) {
				return
			}
			// A first attempt died before it started any operation: run it again.
			if ok, err := s.store.ReclaimIdempotencyKey(ctx, key, model.NowMs()-(30*time.Second).Milliseconds()); err != nil || !ok {
				httpx.WriteErr(w, errIdemInProgress)
				return
			}
		}
		rec := &recorder{ResponseWriter: w}
		h(rec, r.WithContext(engine.WithIdempotencyKey(ctx, key)))
		status := rec.status
		if status == 0 {
			status = http.StatusOK
		}
		ctx = context.WithoutCancel(ctx)
		// 409 "busy" and 5xx are worth retrying with the same key; don't pin them.
		if status == http.StatusConflict && strings.Contains(rec.body.String(), engine.ErrVMBusy.Detail) || status >= 500 {
			if err := s.store.DeleteIdempotencyKey(ctx, key); err != nil {
				log.Printf("idempotency: releasing key: %v", err)
			}
			return
		}
		if err := s.store.SaveIdempotentResponse(ctx, key, status, rec.body.String()); err != nil {
			log.Printf("idempotency: saving answer: %v", err)
		}
	}
}

// replay answers a repeated key. False if the first attempt left nothing to
// answer with (it died before starting an operation).
func (s *Server) replay(w http.ResponseWriter, r *http.Request, key, hash string) bool {
	ctx := r.Context()
	rec, err := s.store.GetIdempotencyKey(ctx, key)
	if err != nil {
		httpx.WriteErr(w, errIdemInProgress)
		return true
	}
	if rec.RequestHash != hash {
		httpx.WriteErr(w, httpx.NewError(http.StatusUnprocessableEntity, idemHeader+" was already used for a different request"))
		return true
	}
	if rec.Status != 0 {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Idempotent-Replayed", "true")
		w.WriteHeader(rec.Status)
		w.Write([]byte(rec.Body))
		return true
	}
	if rec.OpID == "" {
		return false
	}
	op, err := s.store.GetOp(ctx, rec.OpID)
	if err != nil || op.State == model.OpRunning {
		httpx.WriteErr(w, errIdemInProgress)
		return true
	}
	// The replica that ran it died; answer from the operation's outcome.
	w.Header().Set("Idempotent-Replayed", "true")
	if op.State == model.OpFailed {
		httpx.WriteErr(w, httpx.Conflict("the operation was interrupted: "+op.Error))
		return true
	}
	httpx.WriteJSON(w, http.StatusOK, s.opResult(ctx, op))
	return true
}

// opResult rebuilds the answer of a completed operation.
func (s *Server) opResult(ctx context.Context, op *model.Operation) map[string]any {
	switch op.Kind {
	case model.OpDelete, model.OpPause:
		return map[string]any{"ok": true}
	case model.OpFork:
		out := []map[string]any{}
		for _, id := range op.Params.ChildIDs() {
			if c, err := s.store.GetVM(ctx, id); err == nil {
				out = append(out, vmDict(c, nil))
			}
		}
		return map[string]any{"vms": out}
	}
	v, err := s.store.GetVM(ctx, op.VMID)
	if err != nil {
		return map[string]any{"ok": true}
	}
	d := map[string]any{"vm": vmDict(v, nil)}
	if op.Kind == model.OpCheckpoint {
		d["snapshot_id"] = v.CheckpointID
	}
	return d
}
