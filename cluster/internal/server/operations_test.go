package server

import (
	"context"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jverre/flint/cluster/internal/auth"
	"github.com/jverre/flint/cluster/internal/db"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/store"
)

// replicasDB returns n servers sharing one database, as replicas behind a
// load balancer, and the database for setting up states the API can't reach.
func replicasDB(t *testing.T, n int) ([]*Server, *db.DB) {
	t.Helper()
	url := "sqlite://" + filepath.Join(t.TempDir(), "flint.db")
	var out []*Server
	var raw *db.DB
	for i := 0; i < n; i++ {
		d, err := db.Open(url)
		if err != nil {
			t.Fatal(err)
		}
		if err := d.Migrate(context.Background()); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { d.Close() })
		raw = d
		out = append(out, newServer(Config{ReplicaID: string(rune('a' + i)), LeaseTTL: 15 * time.Second}, store.New(d), nil, &http.Client{}))
	}
	return out, raw
}

func replicas(t *testing.T, n int) []*Server {
	rs, _ := replicasDB(t, n)
	return rs
}

func TestIdempotencyKeyReplaysTheFirstAnswer(t *testing.T) {
	rs := replicas(t, 2)
	calls := 0
	handler := func(w http.ResponseWriter, r *http.Request) {
		calls++
		httpx.WriteJSON(w, 200, map[string]any{"call": calls})
	}
	do := func(s *Server, token, key, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("POST", "/vms", strings.NewReader(body))
		r.Header.Set(idemHeader, key)
		r = r.WithContext(auth.WithPrincipal(r.Context(), &auth.Principal{TokenID: token, Kind: auth.KindService}))
		w := httptest.NewRecorder()
		s.idempotent(handler)(w, r)
		return w
	}
	first := do(rs[0], "tok", "k1", `{"tenant":"t"}`)
	again := do(rs[1], "tok", "k1", `{"tenant":"t"}`) // the retry lands on the other replica
	if calls != 1 || again.Code != 200 || again.Body.String() != first.Body.String() || again.Header().Get("Idempotent-Replayed") != "true" {
		t.Fatalf("retry wasn't replayed: calls=%d %d %q", calls, again.Code, again.Body.String())
	}
	if w := do(rs[0], "tok", "k1", `{"tenant":"other"}`); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("same key, different request: %d %s", w.Code, w.Body.String())
	}
	if do(rs[0], "other-token", "k1", `{"tenant":"t"}`); calls != 2 {
		t.Fatalf("keys aren't scoped to the token: calls=%d", calls)
	}
}
