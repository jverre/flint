// Package join enrols a host into a cluster with a single-use token.
package join

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/jverre/flint/cluster/internal/pki"
	"github.com/jverre/flint/cluster/internal/snapd"
)

// Request describes the joining host.
type Request struct {
	Token     string
	Dir       string // identity directory to write
	Name      string
	AgentURL  string
	SnapdURL  string
	Zone      string
	Labels    map[string]string
	Backend   string
	HostClass string
}

// Join exchanges the token for a host certificate. The control plane's
// certificate is checked against the CA fingerprint pinned in the token.
// Idempotent: an already joined directory is left alone.
func Join(ctx context.Context, req Request) (*snapd.Identity, error) {
	if id, err := snapd.LoadIdentity(req.Dir); err == nil {
		return id, nil
	}
	tok, err := pki.DecodeJoinToken(req.Token)
	if err != nil {
		return nil, err
	}
	keyPEM, csrPEM, err := pki.NewKeyAndCSR(req.Name)
	if err != nil {
		return nil, err
	}
	body, _ := json.Marshal(map[string]any{
		"token": req.Token, "name": req.Name, "csr_pem": string(csrPEM), "agent_url": req.AgentURL,
		"snapd_url": req.SnapdURL, "zone": req.Zone, "labels": req.Labels, "backend": req.Backend, "host_class": req.HostClass,
	})
	client := &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{TLSClientConfig: pki.PinnedTLS(tok.Fingerprint)}}
	var resp *http.Response
	for attempt := 0; ; attempt++ {
		hreq, _ := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(tok.Server, "/")+"/v1/join", bytes.NewReader(body))
		hreq.Header.Set("Content-Type", "application/json")
		resp, err = client.Do(hreq)
		if err == nil || attempt >= 30 {
			break
		}
		// The control plane may still be starting (e.g. both come up from Terraform at once).
		time.Sleep(2 * time.Second)
	}
	if err != nil {
		return nil, fmt.Errorf("join %s: %w", tok.Server, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("join refused: %s: %s", resp.Status, strings.TrimSpace(string(data)))
	}
	var out struct {
		HostID  string `json:"host_id"`
		Cert    string `json:"cert_pem"`
		HostsCA string `json:"hosts_ca_pem"`
		CPCA    string `json:"cp_ca_pem"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, err
	}
	if cp, err := pki.ParseCertPEM([]byte(out.CPCA)); err != nil || pki.Fingerprint(cp.Raw) != tok.Fingerprint {
		return nil, fmt.Errorf("control-plane CA in the join answer doesn't match the token")
	}
	if err := os.MkdirAll(req.Dir, 0o700); err != nil {
		return nil, err
	}
	id := &snapd.Identity{HostID: out.HostID, Server: tok.Server, AgentURL: req.AgentURL, SnapdURL: req.SnapdURL, Name: req.Name}
	idJSON, _ := json.MarshalIndent(id, "", "  ")
	files := []struct {
		name string
		data []byte
		mode os.FileMode
	}{
		{"host.key", keyPEM, 0o600},
		{"host.crt", []byte(out.Cert), 0o644},
		{"hosts-ca.pem", []byte(out.HostsCA), 0o644},
		{"cp-ca.pem", []byte(out.CPCA), 0o644},
		{"host.json", idJSON, 0o644}, // last: its presence means "joined"
	}
	for _, f := range files {
		if err := os.WriteFile(filepath.Join(req.Dir, f.name), f.data, f.mode); err != nil {
			return nil, err
		}
	}
	return id, nil
}
