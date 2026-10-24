package server

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestAuditChainHoldsAcrossReplicas(t *testing.T) {
	rs := replicas(t, 2)
	ctx := context.Background()
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			if err := rs[i%2].appendAudit(ctx, auditEntry{Actor: "token:x", Method: "POST", Path: fmt.Sprintf("/vms/%d", i), Status: 200}); err != nil {
				t.Error(err)
			}
		}(i)
	}
	wg.Wait()
	res, err := rs[0].verifyAudit(ctx)
	if err != nil || res["ok"] != true || res["count"] != 20 {
		t.Fatalf("verify: %v %v", res, err)
	}
	// Edit one entry, then remove another: each shows.
	rs[0].db.Exec(ctx, `UPDATE audit SET actor = 'token:someone-else' WHERE seq = 7`)
	if res, _ := rs[1].verifyAudit(ctx); res["ok"] != false || res["broken_at"] != int64(7) {
		t.Fatalf("an edited entry went unnoticed: %v", res)
	}
	rs[0].db.Exec(ctx, `UPDATE audit SET actor = 'token:x' WHERE seq = 7`)
	rs[0].db.Exec(ctx, `DELETE FROM audit WHERE seq = 12`)
	if res, _ := rs[1].verifyAudit(ctx); res["ok"] != false || res["broken_at"] != int64(13) {
		t.Fatalf("a removed entry went unnoticed: %v", res)
	}
}

func TestEventPagesFilterBeforeTheirLimit(t *testing.T) {
	s := replicas(t, 1)[0]
	ctx := context.Background()
	_, token, _ := s.createToken(ctx, KindService, "acme", []string{"acme"})
	// More than the old query's scan window, all invisible to this caller.
	_, err := s.db.Exec(ctx, `WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<5001)
		INSERT INTO events (ts, type, tenant, vm_id, host_id, payload)
		SELECT 1, 'vm.created', 'other', '', '', '{}' FROM n`)
	if err != nil {
		t.Fatal(err)
	}
	s.emit(ctx, "vm.created", "acme", "vm-1", "", map[string]any{"vm_id": "vm-1"})
	w := do(s, "GET", "/v1/events?limit=1", nil, map[string]string{"Authorization": "Bearer " + token}, "")
	var page struct {
		Events []map[string]any
		Next   int64
	}
	if err := json.Unmarshal(w.Body.Bytes(), &page); err != nil || len(page.Events) != 1 || page.Next != 5002 {
		t.Fatalf("page: %d %s (%v)", w.Code, w.Body, err)
	}
}

func TestLiveEventsHideUntaggedEventsFromPinnedTokens(t *testing.T) {
	s := replicas(t, 1)[0]
	_, token, _ := s.createToken(context.Background(), KindService, "acme", []string{"acme"})
	ts := httptest.NewServer(s.apiHandler())
	defer ts.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	c, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(ts.URL, "http")+"/events", &websocket.DialOptions{
		HTTPHeader: http.Header{"Authorization": {"Bearer " + token}},
	})
	if err != nil {
		t.Fatal(err)
	}
	defer c.CloseNow()
	// Keep publishing until the stream has established its initial cursor.
	done := make(chan struct{})
	defer close(done)
	go func() {
		tick := time.NewTicker(50 * time.Millisecond)
		defer tick.Stop()
		for {
			select {
			case <-done:
				return
			case <-tick.C:
				s.emit(ctx, "host.ready", "", "", "host-1", nil)
				s.emit(ctx, "vm.exec", "acme", "vm-1", "", map[string]any{"vm_id": "vm-1", "seq": -1})
			}
		}
	}()
	_, body, err := c.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var event map[string]any
	if err := json.Unmarshal(body, &event); err != nil || event["type"] != "vm.exec" || event["seq"].(float64) <= 0 {
		t.Fatalf("unexpected event: %s (%v)", body, err)
	}
}

func TestAPICallsAreAuditedWithTheirActor(t *testing.T) {
	s := replicas(t, 1)[0]
	ctx := context.Background()
	id, token, err := s.createToken(ctx, KindAdmin, "ops", nil)
	if err != nil {
		t.Fatal(err)
	}
	w := do(s, "POST", "/v1/tokens", nil, map[string]string{"Authorization": "Bearer " + token}, `{"kind":"service","name":"ci"}`)
	if w.Code != 200 {
		t.Fatalf("create token: %d %s", w.Code, w.Body)
	}
	do(s, "GET", "/v1/tokens", nil, map[string]string{"Authorization": "Bearer " + token}, "") // reads aren't audited
	es, _ := s.auditEntries(ctx, 0, 100)
	if len(es) != 1 {
		t.Fatalf("want one audit entry, got %+v", es)
	}
	e := es[0]
	if e.Actor != "token:"+id || e.ActorName != "ops" || e.Method != "POST" || e.Path != "/v1/tokens" || e.Status != 200 ||
		e.RequestID != w.Header().Get("X-Request-Id") {
		t.Fatalf("audit entry: %+v (request id %s)", e, w.Header().Get("X-Request-Id"))
	}
}

func TestEventsCarryTheirActorAndStayWithTheirTenant(t *testing.T) {
	s := replicas(t, 1)[0]
	ctx := context.Background()
	_, acme, _ := s.createToken(ctx, KindService, "acme-backend", []string{"acme"})
	_, other, _ := s.createToken(ctx, KindService, "other", []string{"other"})
	p, _ := s.authenticateToken(ctx, acme)
	s.emit(context.WithValue(ctx, principalKey, p), "vm.created", "acme", "vm-1", "", map[string]any{"vm_id": "vm-1"})
	s.emit(ctx, "host.ready", "", "", "h1", nil) // no tenant, no caller

	list := func(tok string) []map[string]any {
		w := do(s, "GET", "/v1/events", nil, map[string]string{"Authorization": "Bearer " + tok}, "")
		var out struct{ Events []map[string]any }
		json.Unmarshal(w.Body.Bytes(), &out)
		return out.Events
	}
	got := list(acme)
	if len(got) != 1 || got[0]["actor"] != "token:"+p.TokenID || got[0]["actor_name"] != "acme-backend" {
		t.Fatalf("acme's events: %v", got)
	}
	if got := list(other); len(got) != 0 {
		t.Fatalf("another tenant saw: %v", got)
	}
	_, admin, _ := s.createToken(ctx, KindAdmin, "ops", nil)
	all := list(admin)
	if len(all) != 2 || all[1]["actor"] != "system" || !strings.HasPrefix(fmt.Sprint(all[0]["actor"]), "token:") {
		t.Fatalf("admin's events: %v", all)
	}
}
