package snapd

import (
	"context"
	"crypto/tls"
	"github.com/jverre/flint/cluster/internal/pki"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestExpiredFenceKillsOnlyTheRecordedProcess(t *testing.T) {
	cmd := exec.Command("sleep", "30")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer cmd.Process.Kill()
	f := &fence{path: filepath.Join(t.TempDir(), "fence.json")}
	f.arm(fenceReq{TTLMs: 20, VMs: []fencedVM{{VMID: "vm", PID: cmd.Process.Pid}}})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go f.loop(ctx)
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("process survived fence")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("fence did not fire")
	}
}

func TestReusedPIDIsNeverKilled(t *testing.T) {
	cmd := exec.Command("sleep", "30")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { cmd.Process.Kill(); cmd.Wait() }()
	f := &fence{}
	f.arm(fenceReq{TTLMs: 1, VMs: []fencedVM{{VMID: "vm", PID: cmd.Process.Pid, StartTime: "not-this-process"}}})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go f.loop(ctx)
	time.Sleep(150 * time.Millisecond)
	if err := cmd.Process.Signal(syscall.Signal(0)); err != nil {
		t.Fatalf("unrelated process killed: %v", err)
	}
}

func TestHandoverClaimSurvivesSnapdRestartAndNeedsMatchingToken(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "handover.json")
	if err := os.WriteFile(path, []byte(`{"token":"saved-token"}`), 0600); err != nil {
		t.Fatal(err)
	}
	d := &Daemon{cfg: Config{IdentityDir: dir}}
	claimed, err := d.claimHandover()
	if err != nil {
		t.Fatal(err)
	}
	if claimed["token"] != "saved-token" {
		t.Fatal(claimed)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal("claim removed token before a confirmed heartbeat")
	}
	w := httptest.NewRecorder()
	r := httptest.NewRequest("DELETE", "/v1/handover", nil)
	d.localHandler().ServeHTTP(w, r)
	if w.Code != 400 {
		t.Fatalf("missing token: %d", w.Code)
	}
}

func handoverDaemon(t *testing.T, delay time.Duration) *Daemon {
	t.Helper()
	cp, err := pki.NewCA("control-plane")
	if err != nil {
		t.Fatal(err)
	}
	hosts, err := pki.NewCA("hosts")
	if err != nil {
		t.Fatal(err)
	}
	serverCert, err := cp.Keypair("server", []string{"127.0.0.1"}, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	hostCert, err := hosts.Keypair("host", nil, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	hostPool, _ := pki.PoolFromPEM(hosts.CertPEM)
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if len(r.TLS.PeerCertificates) == 0 {
			t.Error("missing host mTLS identity")
		}
		time.Sleep(delay)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"lease_ttl_ms":15000}`))
	}))
	server.TLS = &tls.Config{Certificates: []tls.Certificate{serverCert}, ClientCAs: hostPool, ClientAuth: tls.RequireAndVerifyClientCert}
	server.StartTLS()
	t.Cleanup(server.Close)
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "cp-ca.pem"), cp.CertPEM, 0600); err != nil {
		t.Fatal(err)
	}
	return &Daemon{cfg: Config{IdentityDir: dir}, id: &Identity{HostID: "host", Server: server.URL}, cert: &hostCert}
}

func TestHandoverRenewsOverMTLSAndPersistsLease(t *testing.T) {
	d := handoverDaemon(t, 0)
	d.fence.arm(fenceReq{TTLMs: 5000, VMs: []fencedVM{{VMID: "vm", PID: 9999999}}})
	if err := d.beginHandover(context.Background()); err != nil {
		t.Fatal(err)
	}
	until := d.handover.until
	deadline := d.fence.deadline
	if !deadline.After(time.Now()) || deadline.After(until) {
		t.Fatal("invalid handover fence")
	}
	if _, err := os.Stat(filepath.Join(d.cfg.IdentityDir, "lease.json")); err != nil {
		t.Fatal("lease not persisted")
	}
	if err := d.beginHandover(context.Background()); err != nil {
		t.Fatal("idempotent stop failed", err)
	}
	if !d.handover.until.Equal(until) {
		t.Fatal("repeated stop extended handover")
	}
	if _, err := d.claimHandover(); err != nil {
		t.Fatal(err)
	}
	if d.handover.active {
		t.Fatal("returning agent did not stop keeper")
	}
}

func TestSlowRenewalCannotResurrectExpiredFence(t *testing.T) {
	d := handoverDaemon(t, 80*time.Millisecond)
	d.fence.arm(fenceReq{TTLMs: 20, VMs: []fencedVM{{VMID: "vm", PID: 9999999}}})
	before := d.fence.deadline
	if err := d.beginHandover(context.Background()); err == nil {
		t.Fatal("resurrected expired fence")
	}
	if !d.fence.deadline.Equal(before) {
		t.Fatal("late renewal moved an expired deadline")
	}
	if d.handover.active {
		t.Fatal("failed renewal kept extending leases")
	}
}
