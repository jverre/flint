package snapd

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jverre/flint/cluster/internal/pki"
)

type handover struct {
	mu     sync.Mutex
	token  string
	until  time.Time
	vms    []fencedVM
	active bool
}

func (d *Daemon) handoverPath() string { return filepath.Join(d.cfg.IdentityDir, "handover.json") }

func (d *Daemon) beginHandover(ctx context.Context) error {
	d.handover.mu.Lock()
	defer d.handover.mu.Unlock()
	if d.handover.active {
		if time.Now().Before(d.handover.until) {
			return nil
		}
		return fmt.Errorf("handover expired")
	}
	// A previous interrupted upgrade must be claimed by the new agent before
	// another upgrade can start. Repeated stops cannot extend the bound.
	if _, err := os.Stat(d.handoverPath()); err == nil {
		return fmt.Errorf("unclaimed previous handover")
	}
	d.fence.mu.Lock()
	if len(d.fence.vms) > 0 && !time.Now().Before(d.fence.deadline) {
		d.fence.mu.Unlock()
		return fmt.Errorf("VM lease already expired")
	}
	d.handover.vms = append([]fencedVM{}, d.fence.vms...)
	d.fence.mu.Unlock()
	d.handover.token = uuid.NewString()
	d.handover.until = time.Now().Add(120 * time.Second)
	// Persist the token before the control plane accepts it, so a crash or a
	// lost response cannot strand the returning agent without its claim.
	b, _ := json.Marshal(map[string]string{"token": d.handover.token})
	if err := os.WriteFile(d.handoverPath(), b, 0600); err != nil {
		return err
	}
	d.handover.active = true
	if err := d.renewHandoverLocked(ctx); err != nil {
		// Keep the token: a request might have committed despite a lost reply.
		d.handover.active = false
		return err
	}
	return nil
}

func (d *Daemon) renewHandoverLocked(ctx context.Context) error {
	if !d.handover.active || !time.Now().Before(d.handover.until) {
		return fmt.Errorf("handover ended")
	}
	ids := []string{}
	for _, v := range d.handover.vms {
		ids = append(ids, v.VMID)
	}
	body, _ := json.Marshal(map[string]any{"token": d.handover.token, "vms": ids})
	cpCA, err := os.ReadFile(filepath.Join(d.cfg.IdentityDir, "cp-ca.pem"))
	if err != nil {
		return err
	}
	pool, err := pki.PoolFromPEM(cpCA)
	if err != nil {
		return err
	}
	client := &http.Client{Timeout: 3 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{
		MinVersion: tls.VersionTLS12, RootCAs: pool, GetClientCertificate: d.clientCert,
	}}}
	defer client.CloseIdleConnections()
	req, err := http.NewRequestWithContext(ctx, "POST", d.id.Server+"/v1/hosts/self/handover", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	sent := time.Now()
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("control plane refused handover: %s", resp.Status)
	}
	var out struct {
		TTL int64 `json:"lease_ttl_ms"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return err
	}
	deadline := sent.Add(time.Duration(out.TTL) * time.Millisecond)
	if deadline.After(d.handover.until) {
		deadline = d.handover.until
	}
	if out.TTL <= 0 || !time.Now().Before(deadline) {
		return fmt.Errorf("handover renewal already expired")
	}
	d.fence.mu.Lock()
	defer d.fence.mu.Unlock()
	// Never resurrect a fence that fired while the network request was in flight.
	if len(d.handover.vms) > 0 && (!time.Now().Before(d.fence.deadline) || len(d.fence.vms) != len(d.handover.vms)) {
		return fmt.Errorf("VM fence expired during handover")
	}
	lease, _ := json.Marshal(map[string]any{"renewed_at": float64(sent.UnixMilli()) / 1000,
		"ttl_ms": deadline.Sub(sent).Milliseconds()})
	path := filepath.Join(d.cfg.IdentityDir, "lease.json")
	if err := os.WriteFile(path+".handover.tmp", lease, 0600); err != nil {
		return err
	}
	if err := os.Rename(path+".handover.tmp", path); err != nil {
		return err
	}
	if len(d.handover.vms) > 0 {
		d.fence.deadline = deadline
		d.fence.save()
	}
	return nil
}

func (d *Daemon) handoverLoop(ctx context.Context) {
	t := time.NewTicker(2 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		d.handover.mu.Lock()
		if d.handover.active {
			// Any failure is fail closed. The last acknowledged fence stands;
			// only a recovered agent may resume ordinary lease renewal.
			if err := d.renewHandoverLocked(ctx); err != nil {
				d.handover.active = false
			}
		}
		d.handover.mu.Unlock()
	}
}

func (d *Daemon) claimHandover() (map[string]any, error) {
	d.handover.mu.Lock()
	defer d.handover.mu.Unlock()
	d.handover.active = false
	b, err := os.ReadFile(d.handoverPath())
	if os.IsNotExist(err) {
		return map[string]any{}, nil
	}
	if err != nil {
		return nil, err
	}
	var result map[string]any
	if err := json.Unmarshal(b, &result); err != nil {
		return nil, err
	}
	// Leave token on disk until the agent confirms its first heartbeat. This
	// makes a crash between claiming and heartbeat retryable.
	return result, nil
}
