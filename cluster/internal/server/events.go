package server

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/coder/websocket"
)

// emit records a cluster event. Payload keys follow the daemon's events
// (vm_id, vm, from, to, ...); type, ts and the actor (audit.go) are added.
func (s *Server) emit(ctx context.Context, typ, tenant, vmID, hostID string, payload map[string]any) {
	if payload == nil {
		payload = map[string]any{}
	}
	ts := time.Now()
	payload["type"] = typ
	payload["ts"] = float64(ts.UnixNano()) / 1e9
	if hostID != "" {
		payload["host_id"] = hostID
	}
	if tenant != "" {
		payload["tenant"] = tenant
	}
	actor, name := actorOf(ctx)
	payload["actor"] = actor
	if name != "" {
		payload["actor_name"] = name
	}
	s.insertEvent(ctx, ts.UnixMilli(), typ, tenant, vmID, hostID, actor, name, payload)
}

func (s *Server) insertEvent(ctx context.Context, ts int64, typ, tenant, vmID, hostID, actor, name string, payload map[string]any) {
	if _, err := s.db.Exec(ctx, `INSERT INTO events (ts, type, tenant, vm_id, host_id, actor, actor_name, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		ts, typ, tenant, vmID, hostID, actor, name, mustJSON(payload)); err != nil {
		log.Printf("emit %s: %v", typ, err)
	}
}

type eventRow struct {
	Seq     int64
	Tenant  string
	Payload json.RawMessage
}

func (s *Server) eventsSince(ctx context.Context, seq int64, limit int) ([]eventRow, error) {
	rows, err := s.db.Query(ctx, `SELECT seq, tenant, payload FROM events WHERE seq > ? ORDER BY seq LIMIT ?`, seq, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []eventRow
	for rows.Next() {
		var e eventRow
		var p string
		if err := rows.Scan(&e.Seq, &e.Tenant, &p); err != nil {
			return nil, err
		}
		e.Payload = json.RawMessage(p)
		out = append(out, e)
	}
	return out, rows.Err()
}

func (s *Server) lastEventSeq(ctx context.Context) int64 {
	var seq int64
	s.db.QueryRow(ctx, `SELECT COALESCE(MAX(seq), 0) FROM events`).Scan(&seq)
	return seq
}

// handleEventsWS streams new events. Any replica can serve it: events are
// polled from the shared table. ?tenant= filters; tenant-pinned tokens only
// see their tenants.
func (s *Server) handleEventsWS(w http.ResponseWriter, r *http.Request) {
	p := principal(r)
	tenant := r.URL.Query().Get("tenant")
	if tenant != "" && !p.CanTenant(tenant) {
		writeErr(w, http.StatusForbidden, "not allowed for this tenant")
		return
	}
	c, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	defer c.CloseNow()
	ctx := c.CloseRead(r.Context())
	seq := s.lastEventSeq(ctx)
	tick := time.NewTicker(200 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		evs, err := s.eventsSince(ctx, seq, 500)
		if err != nil {
			continue
		}
		for _, e := range evs {
			seq = e.Seq
			if tenant != "" && e.Tenant != tenant {
				continue
			}
			if e.Tenant == "" && !p.IsAdmin() && len(p.Tenants) > 0 || e.Tenant != "" && !p.CanTenant(e.Tenant) {
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
				return
			}
		}
	}
}

// handleEventsList returns stored events, oldest first:
// ?since=<seq>&vm_id=&tenant=&type=&actor=&limit=. Events of no tenant
// only go to callers who see every tenant. next is the since of the next page.
func (s *Server) handleEventsList(w http.ResponseWriter, r *http.Request) {
	s.writeEvents(w, r, r.URL.Query().Get("vm_id"))
}

// handleTimeline is everything that happened to one VM, with who did it.
func (s *Server) handleTimeline(w http.ResponseWriter, r *http.Request) {
	v, ok := s.vmForRequest(w, r)
	if !ok {
		return
	}
	s.writeEvents(w, r, v.ID)
}

func (s *Server) writeEvents(w http.ResponseWriter, r *http.Request, vmID string) {
	p := principal(r)
	q := r.URL.Query()
	since, _ := strconv.ParseInt(q.Get("since"), 10, 64)
	limit, _ := strconv.Atoi(q.Get("limit"))
	if limit <= 0 || limit > 5000 {
		limit = 500
	}
	where, args := []string{"seq > ?"}, []any{since}
	if !p.IsAdmin() && len(p.Tenants) > 0 {
		marks := make([]string, len(p.Tenants))
		for i, tenant := range p.Tenants {
			marks[i] = "?"
			args = append(args, tenant)
		}
		where = append(where, "tenant IN ("+strings.Join(marks, ",")+")")
	}
	for col, val := range map[string]string{"vm_id": vmID, "tenant": q.Get("tenant"), "type": q.Get("type"), "actor": q.Get("actor")} {
		if val != "" {
			where = append(where, col+" = ?")
			args = append(args, val)
		}
	}
	rows, err := s.db.Query(r.Context(), `SELECT seq, tenant, payload FROM events WHERE `+strings.Join(where, " AND ")+
		` ORDER BY seq LIMIT ?`, append(args, limit)...)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	defer rows.Close()
	out := []map[string]any{}
	next := since
	for rows.Next() {
		var seq int64
		var tenant, payload string
		if err := rows.Scan(&seq, &tenant, &payload); err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		next = seq
		var m map[string]any
		json.Unmarshal([]byte(payload), &m)
		m["seq"] = seq
		out = append(out, m)
	}
	if err := rows.Err(); err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"events": out, "next": next})
}
