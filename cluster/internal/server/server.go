// Package server is flint-server's HTTP side: the public API (SDK, CLI,
// Terraform, web UI) and the host API (host agents and snapd). Handlers
// parse and check requests, call the engine (VM operations), the store
// (catalog reads and simple writes) and the auth and sso packages, and turn
// what they return into answers. Replicas are stateless: all state is in the
// database and the object store.
package server

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jverre/flint/cluster/internal/auth"
	"github.com/jverre/flint/cluster/internal/db"
	"github.com/jverre/flint/cluster/internal/engine"
	"github.com/jverre/flint/cluster/internal/hostagent"
	"github.com/jverre/flint/cluster/internal/model"
	"github.com/jverre/flint/cluster/internal/pki"
	"github.com/jverre/flint/cluster/internal/snapstore"
	"github.com/jverre/flint/cluster/internal/sso"
	"github.com/jverre/flint/cluster/internal/store"
)

// Config for one flint-server replica.
type Config struct {
	DatabaseURL string // postgres://... or sqlite:///path
	StoreURL    string // file:///... or s3://bucket/prefix?endpoint=...
	// Listen serves the public API (SDK, CLI, Terraform). Plain HTTP unless
	// TLSCert/TLSKey are set; put it behind a TLS-terminating LB otherwise.
	Listen  string
	TLSCert string
	TLSKey  string
	// HostListen serves host agents (mTLS with host certificates; join uses a token).
	HostListen string
	// HostEndpoint is how hosts reach HostListen (put in join tokens),
	// e.g. https://10.0.0.5:9201 or https://flint-hosts.internal:9201.
	HostEndpoint string
	// AdminToken bootstraps an admin credential (stored hashed). Optional
	// after the first start.
	AdminToken string
	// CA files (PEM). Optional: without them the first replica generates the
	// CAs and stores them in the database. Terraform passes its own so it can
	// build join tokens before the control plane exists.
	CPCACert, CPCAKey, HostsCACert, HostsCAKey string
	// JoinSecret, if set, is a reusable host join credential (token ID
	// "static"), for autoscaling groups whose hosts can't be given a fresh
	// single-use token each. Rotate it by restarting with a new value.
	JoinSecret string
	// SSOConfig is the path of an sso.json (docs/sso.mdx): people sign in to
	// the web UI and CLI with an identity provider. Optional.
	SSOConfig string

	LeaseTTL           time.Duration // host lease; a silent host is fenced after this
	FenceMargin        time.Duration // extra wait after lease expiry before recovering its VMs
	CheckpointInterval time.Duration // durable VMs
	ReplicaID          string
}

func (c *Config) defaults() {
	if c.LeaseTTL == 0 {
		c.LeaseTTL = 15 * time.Second
	}
	if c.FenceMargin == 0 {
		c.FenceMargin = 5 * time.Second
	}
	if c.CheckpointInterval == 0 {
		c.CheckpointInterval = 5 * time.Minute
	}
	if c.ReplicaID == "" {
		hn, _ := os.Hostname()
		c.ReplicaID = fmt.Sprintf("%s-%s", hn, uuid.NewString()[:8])
	}
}

// Server is one control-plane replica.
type Server struct {
	cfg    Config
	store  *store.Store
	eng    *engine.Engine
	auth   *auth.Authenticator
	sso    *sso.Service
	agents *hostagent.Client

	hostsCA *pki.CA
	cpCA    *pki.CA

	leader   bool
	leaderMu sync.Mutex
}

// newServer wires a replica's parts around its store. agentHTTP talks to
// host agents (mTLS in production).
func newServer(cfg Config, st *store.Store, remote snapstore.Remote, agentHTTP *http.Client) *Server {
	agents := &hostagent.Client{HTTP: agentHTTP}
	a := auth.New(st)
	noSSO, _ := sso.New(context.Background(), "", st, a) // New replaces it when SSO is configured
	return &Server{
		cfg: cfg, store: st, auth: a, agents: agents, sso: noSSO,
		eng: engine.New(engine.Config{LeaseTTL: cfg.LeaseTTL, FenceMargin: cfg.FenceMargin, CheckpointInterval: cfg.CheckpointInterval,
			ReplicaID: cfg.ReplicaID}, st, agents, remote),
	}
}

// New connects to the database and object store and loads (or creates) the CAs.
func New(ctx context.Context, cfg Config) (*Server, error) {
	cfg.defaults()
	d, err := db.Open(cfg.DatabaseURL)
	if err != nil {
		return nil, err
	}
	st := store.New(d)
	if err := waitDB(ctx, st); err != nil {
		return nil, err
	}
	if err := d.Migrate(ctx); err != nil {
		return nil, err
	}
	remote, err := snapstore.OpenRemote(cfg.StoreURL)
	if err != nil {
		return nil, err
	}
	hostsCA, err := loadCA(ctx, st, "hosts_ca", "flint hosts CA", cfg.HostsCACert, cfg.HostsCAKey)
	if err != nil {
		return nil, err
	}
	cpCA, err := loadCA(ctx, st, "cp_ca", "flint control-plane CA", cfg.CPCACert, cfg.CPCAKey)
	if err != nil {
		return nil, err
	}
	// This replica's identity towards host agents.
	cpClient, err := cpCA.Keypair("flint-server", nil, 30*24*time.Hour)
	if err != nil {
		return nil, err
	}
	agentHTTP := &http.Client{
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{
				MinVersion:   tls.VersionTLS12,
				RootCAs:      hostsCA.Pool(),
				Certificates: []tls.Certificate{cpClient},
			},
			MaxIdleConnsPerHost: 16,
			IdleConnTimeout:     60 * time.Second,
			DialContext:         (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
		},
	}
	s := newServer(cfg, st, remote, agentHTTP)
	s.hostsCA, s.cpCA = hostsCA, cpCA
	if cfg.AdminToken != "" {
		if err := s.auth.EnsureAdminToken(ctx, cfg.AdminToken); err != nil {
			return nil, err
		}
	}
	if s.sso, err = sso.New(ctx, cfg.SSOConfig, st, s.auth); err != nil {
		return nil, err
	}
	return s, nil
}

func waitDB(ctx context.Context, st *store.Store) error {
	deadline := time.Now().Add(60 * time.Second)
	for {
		err := st.Ping(ctx)
		if err == nil {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("database not reachable: %w", err)
		}
		log.Printf("waiting for database: %v", err)
		time.Sleep(time.Second)
	}
}

// loadCA returns the cluster-wide CA stored in the database, creating it the
// first time. Replicas racing here all end up with the same one.
func loadCA(ctx context.Context, st *store.Store, key, cn, certFile, keyFile string) (*pki.CA, error) {
	var fresh *pki.CA
	var err error
	if certFile != "" {
		certPEM, err1 := os.ReadFile(certFile)
		keyPEM, err2 := os.ReadFile(keyFile)
		if err1 != nil || err2 != nil {
			return nil, fmt.Errorf("%s files: %v %v", key, err1, err2)
		}
		if fresh, err = pki.LoadCA(certPEM, keyPEM); err != nil {
			return nil, fmt.Errorf("%s: %w", key, err)
		}
	} else if fresh, err = pki.NewCA(cn); err != nil {
		return nil, err
	}
	certPEM, err := st.SetMetaIfAbsent(ctx, key+"_cert", string(fresh.CertPEM))
	if err != nil {
		return nil, err
	}
	if certPEM == string(fresh.CertPEM) {
		if err := st.InsertMetaIfAbsent(ctx, key+"_key", string(fresh.KeyPEM)); err != nil {
			return nil, err
		}
	}
	// Another replica may have inserted the cert but not yet the key.
	var keyPEM string
	for i := 0; i < 50; i++ {
		if keyPEM, err = st.GetMeta(ctx, key+"_key"); err == nil {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if err != nil {
		return nil, fmt.Errorf("%s key: %w", key, err)
	}
	if certFile != "" && certPEM != string(fresh.CertPEM) {
		return nil, fmt.Errorf("%s: the database already has a different CA than %s", key, certFile)
	}
	return pki.LoadCA([]byte(certPEM), []byte(keyPEM))
}

// Run serves both listeners and runs the reconcilers until ctx is done.
func (s *Server) Run(ctx context.Context) error {
	errc := make(chan error, 2)

	api := &http.Server{Addr: s.cfg.Listen, Handler: s.apiHandler(), ReadHeaderTimeout: 10 * time.Second}
	go func() {
		var err error
		if s.cfg.TLSCert != "" {
			err = api.ListenAndServeTLS(s.cfg.TLSCert, s.cfg.TLSKey)
		} else {
			err = api.ListenAndServe()
		}
		if !errors.Is(err, http.ErrServerClosed) {
			errc <- fmt.Errorf("api listener: %w", err)
		}
	}()

	hostTLS, err := s.hostListenerTLS()
	if err != nil {
		return err
	}
	hostSrv := &http.Server{Addr: s.cfg.HostListen, Handler: s.hostHandler(), TLSConfig: hostTLS, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		ln, err := net.Listen("tcp", s.cfg.HostListen)
		if err != nil {
			errc <- fmt.Errorf("host listener: %w", err)
			return
		}
		if err := hostSrv.Serve(tls.NewListener(ln, hostTLS)); !errors.Is(err, http.ErrServerClosed) {
			errc <- fmt.Errorf("host listener: %w", err)
		}
	}()

	go s.leaderLoop(ctx)
	log.Printf("flint-server %s: api on %s, hosts on %s (%s), db %s", s.cfg.ReplicaID, s.cfg.Listen, s.cfg.HostListen, s.cfg.HostEndpoint, s.store.Dialect())

	select {
	case <-ctx.Done():
	case err := <-errc:
		return err
	}
	shCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	api.Shutdown(shCtx)
	hostSrv.Shutdown(shCtx)
	return nil
}

// hostListenerTLS: the server presents a control-plane-CA certificate (with
// the CA in the chain, so joining hosts can check their pinned fingerprint)
// and verifies host client certificates against the hosts CA. Join requests
// carry no client certificate yet; their token authenticates them.
func (s *Server) hostListenerTLS() (*tls.Config, error) {
	var names []string
	if s.cfg.HostEndpoint != "" {
		if u := strings.TrimPrefix(strings.TrimPrefix(s.cfg.HostEndpoint, "https://"), "http://"); u != "" {
			host := u
			if h, _, err := net.SplitHostPort(u); err == nil {
				host = h
			}
			names = append(names, host)
		}
	}
	names = append(names, "localhost", "127.0.0.1")
	cert, err := s.cpCA.Keypair("flint-server", names, 365*24*time.Hour)
	if err != nil {
		return nil, err
	}
	cert.Certificate = append(cert.Certificate, s.cpCA.Cert.Raw)
	return &tls.Config{
		MinVersion:   tls.VersionTLS12,
		Certificates: []tls.Certificate{cert},
		ClientCAs:    s.hostsCA.Pool(),
		ClientAuth:   tls.VerifyClientCertIfGiven,
	}, nil
}

// hostFromRequest returns the authenticated host ID of a host-listener request.
func hostFromRequest(r *http.Request) (string, bool) {
	if r.TLS == nil || len(r.TLS.VerifiedChains) == 0 {
		return "", false
	}
	leaf := r.TLS.VerifiedChains[0][0]
	for _, eku := range leaf.ExtKeyUsage {
		if eku == x509.ExtKeyUsageClientAuth {
			return leaf.Subject.CommonName, leaf.Subject.CommonName != ""
		}
	}
	return "", false
}

// leaderLoop keeps trying to hold the reconciler lease; the holder runs them.
func (s *Server) leaderLoop(ctx context.Context) {
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	var cancel context.CancelFunc
	for {
		now := model.NowMs()
		ok, err := s.store.TryLease(ctx, "reconciler", s.cfg.ReplicaID, now, now+5000)
		if err != nil {
			log.Printf("leader lease: %v", err)
			ok = false
		}
		s.leaderMu.Lock()
		was := s.leader
		s.leader = ok
		s.leaderMu.Unlock()
		if ok && !was {
			log.Printf("replica %s is now the reconciler leader", s.cfg.ReplicaID)
			var rctx context.Context
			rctx, cancel = context.WithCancel(ctx)
			go s.eng.RunReconcilers(rctx)
		} else if !ok && was {
			log.Printf("replica %s lost the reconciler lease", s.cfg.ReplicaID)
			if cancel != nil {
				cancel()
			}
		}
		select {
		case <-ctx.Done():
			if cancel != nil {
				cancel()
			}
			return
		case <-tick.C:
		}
	}
}

func (s *Server) isLeader() bool {
	s.leaderMu.Lock()
	defer s.leaderMu.Unlock()
	return s.leader
}
