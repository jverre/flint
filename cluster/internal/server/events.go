package server

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"github.com/coder/websocket"
	"github.com/jverre/flint/cluster/internal/auth"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/store"
)

// visible reports whether p may see an event of tenant: events of no tenant
// only go to callers who see every tenant.
func visible(p *auth.Principal, tenant string) bool {
	if tenant == "" {
		return p.SeesAllTenants()
	}
	return p.CanTenant(tenant)
}

// handleEventsWS streams new events. Any replica can serve it: events are
// polled from the shared table. ?tenant= filters; tenant-pinned tokens only
// see their tenants.
func (s *Server) handleEventsWS(w http.ResponseWriter, r *http.Request) error {
	p := auth.FromContext(r.Context())
	tenant := r.URL.Query().Get("tenant")
	if tenant != "" && !p.CanTenant(tenant) {
		return httpx.Forbidden("not allowed for this tenant")
	}
	c, err := websocket.Accept(w, r, nil)
	if err != nil {
		return nil // Accept has answered
	}
	defer c.CloseNow()
	ctx := c.CloseRead(r.Context())
	seq, err := s.store.LastEventSeq(ctx)
	if err != nil {
		c.Close(websocket.StatusInternalError, "events unavailable")
		return nil
	}
	tick := time.NewTicker(200 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-tick.C:
		}
		evs, err := s.store.ListEvents(ctx, store.EventQuery{Since: seq, Limit: 500})
		if err != nil {
			continue
		}
		for _, e := range evs {
			seq = e.Seq
			if tenant != "" && e.Tenant != tenant || !visible(p, e.Tenant) {
				continue
			}
			var payload map[string]any
			if json.Unmarshal(e.Payload, &payload) != nil {
				continue
			}
			payload["seq"] = e.Seq // host-local sequence numbers aren't cluster cursors
			data, _ := json.Marshal(payload)
			wctx, cancel := context.WithTimeout(ctx, 10*time.Second)
			err := c.Write(wctx, websocket.MessageText, data)
			cancel()
			if err != nil {
				return nil
			}
		}
	}
}

// handleEventsList returns stored events, oldest first:
// ?since=<seq>&vm_id=&tenant=&type=&actor=&limit=. Events of no tenant
// only go to callers who see every tenant. next is the since of the next page.
func (s *Server) handleEventsList(w http.ResponseWriter, r *http.Request) error {
	return s.writeEvents(w, r, r.URL.Query().Get("vm_id"))
}

// handleTimeline is everything that happened to one VM, with who did it.
func (s *Server) handleTimeline(w http.ResponseWriter, r *http.Request) error {
	v, err := s.vmForRequest(r)
	if err != nil {
		return err
	}
	return s.writeEvents(w, r, v.ID)
}

func (s *Server) writeEvents(w http.ResponseWriter, r *http.Request, vmID string) error {
	p := auth.FromContext(r.Context())
	q := r.URL.Query()
	since, _ := strconv.ParseInt(q.Get("since"), 10, 64)
	limit, _ := strconv.Atoi(q.Get("limit"))
	if limit <= 0 || limit > 5000 {
		limit = 500
	}
	eq := store.EventQuery{Since: since, Limit: limit, VMID: vmID, Tenant: q.Get("tenant"), Type: q.Get("type"), Actor: q.Get("actor")}
	if !p.SeesAllTenants() {
		eq.Tenants = p.Tenants
	}
	evs, err := s.store.ListEvents(r.Context(), eq)
	if err != nil {
		return err
	}
	out := []map[string]any{}
	next := since
	for _, e := range evs {
		next = e.Seq
		var m map[string]any
		json.Unmarshal(e.Payload, &m)
		if m == nil {
			m = map[string]any{}
		}
		m["seq"] = e.Seq
		out = append(out, m)
	}
	return httpx.JSON(w, map[string]any{"events": out, "next": next})
}
