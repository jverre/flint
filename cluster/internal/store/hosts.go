package store

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/jverre/flint/cluster/internal/db"
	"github.com/jverre/flint/cluster/internal/model"
)

const hostCols = `id, name, agent_url, snapd_url, zone, labels, backend, host_class, state, cordoned, capacity, cached, templates, version, last_heartbeat, state_since, max_vms, created_at, patch_state, handover_token, handover_until`

func scanHost(r scanner) (*model.Host, error) {
	var h model.Host
	var state, labels, capacity, cached, templates, patchState string
	var cordoned int
	if err := r.Scan(&h.ID, &h.Name, &h.AgentURL, &h.SnapdURL, &h.Zone, &labels, &h.Backend, &h.HostClass, &state, &cordoned,
		&capacity, &cached, &templates, &h.Version, &h.LastHeartbeat, &h.StateSince, &h.MaxVMs, &h.CreatedAt, &patchState, &h.HandoverToken, &h.HandoverUntil); err != nil {
		return nil, err
	}
	h.State = model.HostState(state)
	h.Cordoned = cordoned != 0
	json.Unmarshal([]byte(patchState), &h.PatchState)
	json.Unmarshal([]byte(labels), &h.Labels)
	json.Unmarshal([]byte(capacity), &h.Capacity)
	json.Unmarshal([]byte(cached), &h.Cached)
	json.Unmarshal([]byte(templates), &h.Templates)
	return &h, nil
}

// InsertHost registers a host (a join).
func (s *Store) InsertHost(ctx context.Context, h *model.Host) error {
	if h.CreatedAt == 0 {
		h.CreatedAt = model.NowMs()
	}
	if h.Labels == nil {
		h.Labels = map[string]string{}
	}
	if h.Cached == nil {
		h.Cached = []string{}
	}
	if h.Templates == nil {
		h.Templates = []model.HostTemplate{}
	}
	_, err := s.db.Exec(ctx, `INSERT INTO hosts (id, name, agent_url, snapd_url, zone, labels, backend, host_class, state, cordoned,
		capacity, cached, templates, version, last_heartbeat, state_since, max_vms, created_at, patch_state, handover_token, handover_until)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		h.ID, h.Name, h.AgentURL, h.SnapdURL, h.Zone, mustJSON(h.Labels), h.Backend, h.HostClass, string(h.State), boolInt(h.Cordoned),
		mustJSON(h.Capacity), mustJSON(h.Cached), mustJSON(h.Templates), h.Version, h.LastHeartbeat, h.StateSince, h.MaxVMs, h.CreatedAt,
		mustJSON(h.PatchState), h.HandoverToken, h.HandoverUntil)
	return err
}

func (s *Store) GetHost(ctx context.Context, id string) (*model.Host, error) {
	return scanHost(s.db.QueryRow(ctx, `SELECT `+hostCols+` FROM hosts WHERE id = ?`, id))
}

func (s *Store) ListHosts(ctx context.Context) ([]*model.Host, error) {
	return queryAll(ctx, s, scanHost, `SELECT `+hostCols+` FROM hosts ORDER BY created_at`)
}

// SetHostState moves a host to state (a no-op if it's there already).
func (s *Store) SetHostState(ctx context.Context, id string, state model.HostState) error {
	_, err := s.db.Exec(ctx, `UPDATE hosts SET state = ?, state_since = ? WHERE id = ? AND state <> ?`, string(state), model.NowMs(), id, string(state))
	return err
}

// SetHostMaxVMs sets the operator's VM cap (0: what the host reports).
func (s *Store) SetHostMaxVMs(ctx context.Context, id string, n int) error {
	k, err := affected(s.db.Exec(ctx, `UPDATE hosts SET max_vms = ? WHERE id = ?`, n, id))
	if err == nil && k == 0 {
		err = ErrNotFound
	}
	return err
}

// SetCordoned stops (or resumes) placing VMs on a host. Uncordoning also
// drops the host's drain reservations for VMs not already moving, and
// returns a draining or drained host to service.
func (s *Store) SetCordoned(ctx context.Context, id string, on bool) error {
	return s.db.Tx(ctx, func(tx *db.Tx) error {
		k, err := affected(tx.Exec(ctx, `UPDATE hosts SET cordoned = ? WHERE id = ?`, boolInt(on), id))
		if err != nil {
			return err
		}
		if k == 0 {
			return ErrNotFound
		}
		if on {
			return nil
		}
		if _, err := tx.Exec(ctx, `DELETE FROM drain_reservations WHERE source_host = ? AND vm_id NOT IN (SELECT id FROM vms WHERE state = ?)`,
			id, string(model.VMMigrating)); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE hosts SET state = ?, state_since = ? WHERE id = ? AND state IN (?, ?)`,
			string(model.HostReady), model.NowMs(), id, string(model.HostDraining), string(model.HostDrained))
		return err
	})
}

func (s *Store) DeleteHost(ctx context.Context, id string) error {
	_, err := s.db.Exec(ctx, `DELETE FROM hosts WHERE id = ?`, id)
	return err
}

// Heartbeat is what a heartbeat records about its host.
type Heartbeat struct {
	PatchState map[string]any
	Capacity   model.Capacity
	Cached     []string
	Templates  []model.HostTemplate
	Version    string
	Backend    string
	HostClass  string
	At         int64
}

// ErrHandoverRaced: the host's handover token changed under a heartbeat.
var ErrHandoverRaced = errors.New("handover started during heartbeat")

// RecordHeartbeat renews a host's lease and inventory, and ends any agent
// handover, provided the handover token is still the one the caller saw.
func (s *Store) RecordHeartbeat(ctx context.Context, id, handoverToken string, hb Heartbeat) error {
	if hb.Cached == nil {
		hb.Cached = []string{}
	}
	if hb.Templates == nil {
		hb.Templates = []model.HostTemplate{}
	}
	k, err := affected(s.db.Exec(ctx, `UPDATE hosts SET handover_token = '', handover_until = 0, patch_state = ?, capacity = ?, cached = ?,
		templates = ?, version = ?, backend = ?, host_class = ?, last_heartbeat = ? WHERE id = ? AND handover_token = ?`,
		mustJSON(hb.PatchState), mustJSON(hb.Capacity), mustJSON(hb.Cached), mustJSON(hb.Templates), hb.Version, hb.Backend, hb.HostClass,
		hb.At, id, handoverToken))
	if err == nil && k != 1 {
		err = ErrHandoverRaced
	}
	return err
}

// ErrHandoverLost: another owner took the handover, or the lease expired.
var ErrHandoverLost = errors.New("handover raced with another owner")

// ClaimHandover gives snapd the host's lease until `until`, provided the
// handover token is still prevToken and the lease hasn't expired (its last
// heartbeat is after leaseFloor).
func (s *Store) ClaimHandover(ctx context.Context, id, prevToken, token string, until, now, leaseFloor int64) error {
	k, err := affected(s.db.Exec(ctx, `UPDATE hosts SET handover_token = ?, handover_until = ?, last_heartbeat = ?
		WHERE id = ? AND handover_token = ? AND last_heartbeat > ?`, token, until, now, id, prevToken, leaseFloor))
	if err == nil && k != 1 {
		err = ErrHandoverLost
	}
	return err
}
