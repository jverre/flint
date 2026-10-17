// Package snapd is the per-host companion of the Python host agent:
//
//   - the snapshot store's local API (unix socket, root only): take a
//     snapshot of files into the cache and object store, restore one;
//   - the peer API (mTLS, host certificates only): serve cached chunks to
//     other hosts restoring a snapshot taken here;
//   - the second liveness channel: a heartbeat object in the object store;
//   - fencing: kill VMs whose leases the agent stopped renewing, backed by
//     the host watchdog so a hung snapd can't keep them alive either;
//   - host certificate renewal.
package snapd

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/jverre/flint/cluster/internal/pki"
	"github.com/jverre/flint/cluster/internal/snapstore"
)

// Config for snapd.
type Config struct {
	IdentityDir string // written by `flint-cluster join`: host.json, host.key, host.crt, hosts-ca.pem, cp-ca.pem
	CacheDir    string
	CacheMax    int64
	StoreURL    string
	Socket      string // local API
	PeerListen  string // e.g. 0.0.0.0:9501
	// WatchdogDevice (e.g. /dev/watchdog): petted every second while snapd
	// runs. If snapd hangs or dies, the host resets, so VMs whose leases
	// lapsed can't outlive their fence.
	WatchdogDevice string
}

// Identity is the host's cluster identity on disk.
type Identity struct {
	HostID   string `json:"host_id"`
	Server   string `json:"server"` // control plane host endpoint
	AgentURL string `json:"agent_url"`
	SnapdURL string `json:"snapd_url"`
	Name     string `json:"name"`
}

// LoadIdentity reads host.json from dir.
func LoadIdentity(dir string) (*Identity, error) {
	b, err := os.ReadFile(filepath.Join(dir, "host.json"))
	if err != nil {
		return nil, err
	}
	var id Identity
	return &id, json.Unmarshal(b, &id)
}

type Daemon struct {
	cfg   Config
	id    *Identity
	store *snapstore.Store

	certMu sync.RWMutex
	cert   *tls.Certificate

	fence fence
}

// Run starts snapd and blocks until ctx is done.
func Run(ctx context.Context, cfg Config) error {
	id, err := LoadIdentity(cfg.IdentityDir)
	if err != nil {
		return fmt.Errorf("host identity (run `flint-cluster join` first): %w", err)
	}
	d := &Daemon{cfg: cfg, id: id}
	if err := d.loadCert(); err != nil {
		return err
	}
	hostsCA, err := os.ReadFile(filepath.Join(cfg.IdentityDir, "hosts-ca.pem"))
	if err != nil {
		return err
	}
	pool, err := pki.PoolFromPEM(hostsCA)
	if err != nil {
		return err
	}
	remote, err := snapstore.OpenRemote(cfg.StoreURL)
	if err != nil {
		return err
	}
	cache, err := snapstore.OpenCache(cfg.CacheDir, cfg.CacheMax)
	if err != nil {
		return err
	}
	peerClient := &http.Client{Transport: &http.Transport{TLSClientConfig: &tls.Config{
		MinVersion: tls.VersionTLS12, RootCAs: pool, GetClientCertificate: d.clientCert,
	}}}
	d.store = snapstore.NewStore(remote, cache, peerClient)

	errc := make(chan error, 3)
	// Local API on a root-only unix socket.
	os.Remove(cfg.Socket)
	if err := os.MkdirAll(filepath.Dir(cfg.Socket), 0o700); err != nil {
		return err
	}
	ln, err := net.Listen("unix", cfg.Socket)
	if err != nil {
		return err
	}
	os.Chmod(cfg.Socket, 0o600)
	local := &http.Server{Handler: d.localHandler()}
	go func() { errc <- local.Serve(ln) }()

	// Peer API: only clients with a hosts-CA certificate.
	peerTLS := &tls.Config{
		MinVersion:     tls.VersionTLS12,
		GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) { return d.currentCert(), nil },
		ClientCAs:      pool,
		ClientAuth:     tls.RequireAndVerifyClientCert,
	}
	pln, err := net.Listen("tcp", cfg.PeerListen)
	if err != nil {
		return err
	}
	peer := &http.Server{Handler: snapstore.PeerHandler(cache), ReadHeaderTimeout: 10 * time.Second}
	go func() { errc <- peer.Serve(tls.NewListener(pln, peerTLS)) }()

	go d.storageHeartbeat(ctx)
	go d.renewLoop(ctx)
	d.fence.path = filepath.Join(cfg.IdentityDir, "fence.json")
	d.fence.load()
	go d.fence.loop(ctx)
	if cfg.WatchdogDevice != "" {
		go d.watchdog(ctx)
	}
	log.Printf("snapd for host %s: local %s, peers %s, store %s", id.HostID, cfg.Socket, cfg.PeerListen, cfg.StoreURL)

	select {
	case <-ctx.Done():
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
	}
	local.Close()
	peer.Close()
	return nil
}

func (d *Daemon) loadCert() error {
	cert, err := tls.LoadX509KeyPair(filepath.Join(d.cfg.IdentityDir, "host.crt"), filepath.Join(d.cfg.IdentityDir, "host.key"))
	if err != nil {
		return fmt.Errorf("host certificate: %w", err)
	}
	d.certMu.Lock()
	d.cert = &cert
	d.certMu.Unlock()
	return nil
}

func (d *Daemon) currentCert() *tls.Certificate {
	d.certMu.RLock()
	defer d.certMu.RUnlock()
	return d.cert
}

func (d *Daemon) clientCert(*tls.CertificateRequestInfo) (*tls.Certificate, error) { return d.currentCert(), nil }

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]string{"detail": err.Error()})
}

func (d *Daemon) localHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"status": "ok", "host_id": d.id.HostID, "cache_bytes": d.store.Cache.Size()})
	})
	mux.HandleFunc("POST /v1/snapshots", func(w http.ResponseWriter, r *http.Request) {
		var req snapstore.CreateRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeErr(w, 400, err)
			return
		}
		m, st, err := d.store.Create(r.Context(), req)
		if err != nil {
			writeErr(w, 500, err)
			return
		}
		writeJSON(w, 200, map[string]any{"snapshot": map[string]any{
			"id": m.ID, "kind": m.Kind, "bytes": m.Bytes(), "chunks": st.Chunks, "uploaded": req.Upload == "sync" || req.Upload == "",
		}, "stats": st})
	})
	mux.HandleFunc("POST /v1/restore", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			ID    string   `json:"id"`
			Dest  string   `json:"dest"`
			Peers []string `json:"peers"`
			Names []string `json:"names"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Dest == "" || !filepath.IsAbs(req.Dest) {
			writeErr(w, 400, errors.New("id and an absolute dest are required"))
			return
		}
		m, st, err := d.store.Restore(r.Context(), req.ID, req.Dest, req.Peers, req.Names)
		if err != nil {
			writeErr(w, 500, err)
			return
		}
		writeJSON(w, 200, map[string]any{"stats": st, "meta": m.Meta, "kind": m.Kind, "files": fileNames(m)})
	})
	mux.HandleFunc("GET /v1/snapshots/{id}/status", func(w http.ResponseWriter, r *http.Request) {
		done, err := d.store.UploadStatus(r.Context(), r.PathValue("id"))
		resp := map[string]any{"uploaded": done && err == nil}
		if err != nil {
			resp["error"] = err.Error()
		}
		writeJSON(w, 200, resp)
	})
	mux.HandleFunc("POST /v1/snapshots/{id}/wait", func(w http.ResponseWriter, r *http.Request) {
		if err := d.store.WaitUploaded(r.Context(), r.PathValue("id")); err != nil {
			writeErr(w, 500, err)
			return
		}
		writeJSON(w, 200, map[string]any{"uploaded": true})
	})
	mux.HandleFunc("DELETE /v1/snapshots/{id}", func(w http.ResponseWriter, r *http.Request) {
		d.store.Cache.DeleteManifest(r.PathValue("id"))
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	mux.HandleFunc("GET /v1/cache", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"complete": d.store.Cache.Complete(), "bytes": d.store.Cache.Size()})
	})
	mux.HandleFunc("POST /v1/fence", func(w http.ResponseWriter, r *http.Request) {
		var req fenceReq
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.TTLMs <= 0 {
			writeErr(w, 400, errors.New("ttl_ms and vms are required"))
			return
		}
		d.fence.arm(req)
		writeJSON(w, 200, d.fence.status())
	})
	mux.HandleFunc("DELETE /v1/fence", func(w http.ResponseWriter, r *http.Request) {
		d.fence.disarm()
		writeJSON(w, 200, d.fence.status())
	})
	mux.HandleFunc("GET /v1/fence", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, d.fence.status())
	})
	return mux
}

func fileNames(m *snapstore.Manifest) []string {
	var out []string
	for n := range m.Files {
		out = append(out, n)
	}
	return out
}

// storageHeartbeat writes heartbeats/<host_id> every 2s.
func (d *Daemon) storageHeartbeat(ctx context.Context) {
	t := time.NewTicker(2 * time.Second)
	defer t.Stop()
	for {
		b, _ := json.Marshal(map[string]any{"ts": time.Now().UnixMilli(), "host_id": d.id.HostID})
		hctx, cancel := context.WithTimeout(ctx, 5*time.Second)
		if err := d.store.Remote.Put(hctx, "heartbeats/"+d.id.HostID, b); err != nil {
			log.Printf("storage heartbeat: %v", err)
		}
		cancel()
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// renewLoop renews the host certificate at half its lifetime. The Python
// agent picks up the new files on its next TLS handshake.
func (d *Daemon) renewLoop(ctx context.Context) {
	t := time.NewTicker(10 * time.Minute)
	defer t.Stop()
	for {
		if err := d.renewIfDue(ctx); err != nil {
			log.Printf("certificate renewal: %v", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func (d *Daemon) renewIfDue(ctx context.Context) error {
	c := d.currentCert()
	leaf := c.Leaf
	if leaf == nil {
		return nil
	}
	life := leaf.NotAfter.Sub(leaf.NotBefore)
	if time.Until(leaf.NotAfter) > life/2 {
		return nil
	}
	keyPEM, err := os.ReadFile(filepath.Join(d.cfg.IdentityDir, "host.key"))
	if err != nil {
		return err
	}
	csr, err := pki.CSRFromKey(keyPEM, d.id.HostID)
	if err != nil {
		return err
	}
	cpCA, err := os.ReadFile(filepath.Join(d.cfg.IdentityDir, "cp-ca.pem"))
	if err != nil {
		return err
	}
	pool, err := pki.PoolFromPEM(cpCA)
	if err != nil {
		return err
	}
	client := &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{
		RootCAs: pool, Certificates: []tls.Certificate{*c},
	}}}
	body, _ := json.Marshal(map[string]string{"csr_pem": string(csr)})
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(d.id.Server, "/")+"/v1/hosts/self/renew", strings.NewReader(string(body)))
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	var out struct {
		Cert string `json:"cert_pem"`
	}
	if resp.StatusCode != 200 || json.NewDecoder(resp.Body).Decode(&out) != nil || out.Cert == "" {
		return fmt.Errorf("renew: %s", resp.Status)
	}
	p := filepath.Join(d.cfg.IdentityDir, "host.crt")
	if err := os.WriteFile(p+".tmp", []byte(out.Cert), 0o644); err != nil {
		return err
	}
	if err := os.Rename(p+".tmp", p); err != nil {
		return err
	}
	log.Printf("host certificate renewed")
	return d.loadCert()
}

// ── fencing ────────────────────────────────────────────────────────────────

type fencedVM struct {
	VMID string `json:"vm_id"`
	PID  int    `json:"pid"`
	// StartTime (from /proc/<pid>/stat) guards against killing a reused pid.
	StartTime string `json:"start_time,omitempty"`
}

type fenceReq struct {
	TTLMs int64      `json:"ttl_ms"`
	VMs   []fencedVM `json:"vms"`
}

// fence kills the listed VM processes if the agent doesn't renew the
// deadline in time: an agent that can't renew its leases with the control
// plane stops renewing here, and the control plane recovers those VMs
// elsewhere only after the lease (and so this deadline) has passed.
type fence struct {
	mu       sync.Mutex
	deadline time.Time
	vms      []fencedVM
	fenced   []string
	firedAt  time.Time
	// path persists the armed fence, so a restarted snapd still enforces a
	// deadline it accepted before (and fires at once if it passed meanwhile).
	path string
}

type fenceFile struct {
	Deadline int64      `json:"deadline_unix_ms"`
	VMs      []fencedVM `json:"vms"`
}

func (f *fence) save() {
	if f.path == "" {
		return
	}
	if f.deadline.IsZero() {
		os.Remove(f.path)
		return
	}
	b, _ := json.Marshal(fenceFile{Deadline: f.deadline.UnixMilli(), VMs: f.vms})
	tmp := f.path + ".tmp"
	if os.WriteFile(tmp, b, 0o600) == nil {
		os.Rename(tmp, f.path)
	}
}

func (f *fence) load() {
	b, err := os.ReadFile(f.path)
	if err != nil {
		return
	}
	var ff fenceFile
	if json.Unmarshal(b, &ff) != nil || ff.Deadline == 0 {
		return
	}
	f.mu.Lock()
	f.deadline, f.vms = time.UnixMilli(ff.Deadline), ff.VMs
	f.mu.Unlock()
	log.Printf("fence: restored %d leased VMs, deadline in %s", len(ff.VMs), time.Until(f.deadline).Round(time.Millisecond))
}

func (f *fence) arm(req fenceReq) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for i := range req.VMs {
		if req.VMs[i].StartTime == "" {
			req.VMs[i].StartTime = procStart(req.VMs[i].PID)
		}
	}
	f.vms = req.VMs
	f.deadline = time.Now().Add(time.Duration(req.TTLMs) * time.Millisecond)
	f.save()
}

func (f *fence) disarm() {
	f.mu.Lock()
	f.vms, f.deadline = nil, time.Time{}
	f.save()
	f.mu.Unlock()
}

func (f *fence) status() map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	st := map[string]any{"armed": !f.deadline.IsZero(), "vms": len(f.vms), "fenced": append([]string{}, f.fenced...)}
	if !f.deadline.IsZero() {
		st["deadline_in_ms"] = time.Until(f.deadline).Milliseconds()
	}
	if !f.firedAt.IsZero() {
		st["fired_at"] = f.firedAt.UnixMilli()
	}
	return st
}

func (f *fence) loop(ctx context.Context) {
	t := time.NewTicker(100 * time.Millisecond)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		f.mu.Lock()
		if f.deadline.IsZero() || time.Now().Before(f.deadline) {
			f.mu.Unlock()
			continue
		}
		vms := f.vms
		f.vms, f.deadline, f.firedAt = nil, time.Time{}, time.Now()
		f.save()
		f.mu.Unlock()
		for _, v := range vms {
			if v.PID <= 1 || (v.StartTime != "" && procStart(v.PID) != v.StartTime) {
				continue
			}
			if err := syscall.Kill(v.PID, syscall.SIGKILL); err == nil {
				log.Printf("fence: lease expired, killed VM %s (pid %d)", v.VMID, v.PID)
			}
			f.mu.Lock()
			f.fenced = append(f.fenced, v.VMID)
			if len(f.fenced) > 1000 {
				f.fenced = f.fenced[len(f.fenced)-1000:]
			}
			f.mu.Unlock()
		}
	}
}

// procStart returns field 22 (starttime) of /proc/<pid>/stat.
func procStart(pid int) string {
	b, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return ""
	}
	s := string(b)
	i := strings.LastIndexByte(s, ')')
	if i < 0 {
		return ""
	}
	fields := strings.Fields(s[i+1:])
	if len(fields) < 20 {
		return ""
	}
	return fields[19]
}

// watchdog pets the host watchdog every second. A clean exit disarms it with
// the magic close character; a hang or crash lets it fire.
func (d *Daemon) watchdog(ctx context.Context) {
	f, err := os.OpenFile(d.cfg.WatchdogDevice, os.O_WRONLY, 0)
	if err != nil {
		log.Printf("watchdog %s unavailable: %v (fencing falls back to process kills only)", d.cfg.WatchdogDevice, err)
		return
	}
	log.Printf("watchdog %s armed", d.cfg.WatchdogDevice)
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			f.Write([]byte("V"))
			f.Close()
			return
		case <-t.C:
			f.Write([]byte("\x00"))
		}
	}
}
