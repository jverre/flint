package server

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"time"

	"github.com/coder/websocket"
)

// emit records a cluster event. Payload keys follow the daemon's events
// (vm_id, vm, from, to, ...); type and ts are added.
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
	if _, err := s.db.Exec(ctx, `INSERT INTO events (ts, type, tenant, vm_id, host_id, payload) VALUES (?, ?, ?, ?, ?, ?)`,
		ts.UnixMilli(), typ, tenant, vmID, hostID, mustJSON(payload)); err != nil {
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
			if e.Tenant != "" && !p.CanTenant(e.Tenant) {
				continue
			}
			wctx, cancel := context.WithTimeout(ctx, 10*time.Second)
			err := c.Write(wctx, websocket.MessageText, e.Payload)
			cancel()
			if err != nil {
				return
			}
		}
	}
}

// handleEventsList returns stored events (audit/timeline): ?since=<seq>&vm_id=&tenant=&limit=
func (s *Server) handleEventsList(w http.ResponseWriter, r *http.Request) {
	p := principal(r)
	q := r.URL.Query()
	var since int64
	json.Unmarshal([]byte(q.Get("since")), &since)
	limit := 500
	evs, err := s.eventsSince(r.Context(), since, 5000)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	var out []map[string]any
	for _, e := range evs {
		if e.Tenant != "" && !p.CanTenant(e.Tenant) {
			continue
		}
		if t := q.Get("tenant"); t != "" && e.Tenant != t {
			continue
		}
		var m map[string]any
		json.Unmarshal(e.Payload, &m)
		if v := q.Get("vm_id"); v != "" && m["vm_id"] != v {
			continue
		}
		if t := q.Get("type"); t != "" && m["type"] != t {
			continue
		}
		m["seq"] = e.Seq
		out = append(out, m)
		if len(out) >= limit {
			break
		}
	}
	if out == nil {
		out = []map[string]any{}
	}
	writeJSON(w, 200, map[string]any{"events": out})
}
