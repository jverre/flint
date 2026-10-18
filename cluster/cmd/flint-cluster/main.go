// flint-cluster runs Flint's cluster components:
//
//	flint-cluster server   control plane replica (API, scheduler, reconcilers)
//	flint-cluster snapd    per-host snapshot store, peer server and fence
//	flint-cluster join     enrol this host with a join token
//
// Every flag can also be set with the FLINT_* environment variable shown in
// its help, which is how the systemd units and cloud-init configure them.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/jverre/flint/cluster/internal/join"
	"github.com/jverre/flint/cluster/internal/server"
	"github.com/jverre/flint/cluster/internal/snapd"
)

func env(name, def string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return def
}

func envDur(name string, def time.Duration) time.Duration {
	if v := os.Getenv(name); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: flint-cluster server|snapd|join [flags]")
	os.Exit(2)
}

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	var err error
	switch os.Args[1] {
	case "server":
		err = runServer(ctx, os.Args[2:])
	case "snapd":
		err = runSnapd(ctx, os.Args[2:])
	case "join":
		err = runJoin(ctx, os.Args[2:])
	default:
		usage()
	}
	if err != nil {
		log.Fatal(err)
	}
}

func runServer(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("server", flag.ExitOnError)
	var cfg server.Config
	fs.StringVar(&cfg.DatabaseURL, "database", env("FLINT_DATABASE_URL", "sqlite:///var/lib/flint/server/flint.db"), "postgres://... or sqlite:///path (FLINT_DATABASE_URL)")
	fs.StringVar(&cfg.StoreURL, "store", env("FLINT_STORE_URL", "file:///var/lib/flint/store"), "snapshot store: file:///path or s3://bucket/prefix?endpoint=... (FLINT_STORE_URL)")
	fs.StringVar(&cfg.Listen, "listen", env("FLINT_LISTEN", ":9300"), "public API address (FLINT_LISTEN)")
	fs.StringVar(&cfg.TLSCert, "tls-cert", env("FLINT_TLS_CERT", ""), "serve the API over TLS with this certificate (FLINT_TLS_CERT)")
	fs.StringVar(&cfg.TLSKey, "tls-key", env("FLINT_TLS_KEY", ""), "key for --tls-cert (FLINT_TLS_KEY)")
	fs.StringVar(&cfg.HostListen, "host-listen", env("FLINT_HOST_LISTEN", ":9301"), "address host agents connect to (FLINT_HOST_LISTEN)")
	fs.StringVar(&cfg.HostEndpoint, "host-endpoint", env("FLINT_HOST_ENDPOINT", ""), "URL hosts use to reach --host-listen, put in join tokens (FLINT_HOST_ENDPOINT)")
	fs.StringVar(&cfg.AdminToken, "admin-token", env("FLINT_ADMIN_TOKEN", ""), "bootstrap admin API token (FLINT_ADMIN_TOKEN)")
	fs.StringVar(&cfg.CPCACert, "cp-ca-cert", env("FLINT_CP_CA_CERT", ""), "control-plane CA certificate file (FLINT_CP_CA_CERT; default: generated, kept in the database)")
	fs.StringVar(&cfg.CPCAKey, "cp-ca-key", env("FLINT_CP_CA_KEY", ""), "control-plane CA key file (FLINT_CP_CA_KEY)")
	fs.StringVar(&cfg.HostsCACert, "hosts-ca-cert", env("FLINT_HOSTS_CA_CERT", ""), "hosts CA certificate file (FLINT_HOSTS_CA_CERT)")
	fs.StringVar(&cfg.HostsCAKey, "hosts-ca-key", env("FLINT_HOSTS_CA_KEY", ""), "hosts CA key file (FLINT_HOSTS_CA_KEY)")
	fs.StringVar(&cfg.JoinSecret, "join-secret", env("FLINT_JOIN_SECRET", ""), "reusable host join secret for autoscaling groups (FLINT_JOIN_SECRET)")
	fs.DurationVar(&cfg.LeaseTTL, "lease-ttl", envDur("FLINT_LEASE_TTL", 15*time.Second), "host lease: silent hosts' VMs are fenced after this (FLINT_LEASE_TTL)")
	fs.DurationVar(&cfg.FenceMargin, "fence-margin", envDur("FLINT_FENCE_MARGIN", 5*time.Second), "wait after lease expiry before recovering VMs (FLINT_FENCE_MARGIN)")
	fs.DurationVar(&cfg.CheckpointInterval, "checkpoint-interval", envDur("FLINT_CHECKPOINT_INTERVAL", 5*time.Minute), "how often durable VMs are checkpointed (FLINT_CHECKPOINT_INTERVAL)")
	fs.StringVar(&cfg.ReplicaID, "replica-id", env("FLINT_REPLICA_ID", ""), "name of this replica (default: hostname + random)")
	joinToken := fs.Bool("print-join-token", false, "print a new host join token and exit")
	zone := fs.String("zone", "", "zone for --print-join-token")
	fs.Parse(args)
	s, err := server.New(ctx, cfg)
	if err != nil {
		return err
	}
	if *joinToken {
		tok, err := s.CreateJoinToken(ctx, *zone, time.Hour)
		if err != nil {
			return err
		}
		fmt.Println(tok)
		return nil
	}
	return s.Run(ctx)
}

func runSnapd(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("snapd", flag.ExitOnError)
	var cfg snapd.Config
	fs.StringVar(&cfg.IdentityDir, "identity", env("FLINT_IDENTITY_DIR", "/var/lib/flint/state/cluster"), "host identity directory written by join (FLINT_IDENTITY_DIR)")
	fs.StringVar(&cfg.CacheDir, "cache", env("FLINT_SNAPSHOT_CACHE", "/var/lib/flint/snapshot-cache"), "local snapshot cache (FLINT_SNAPSHOT_CACHE)")
	cacheGB := fs.Int("cache-gb", 20, "cache size cap in GiB")
	fs.StringVar(&cfg.StoreURL, "store", env("FLINT_STORE_URL", ""), "snapshot store URL, same as the server's (FLINT_STORE_URL)")
	fs.StringVar(&cfg.Socket, "socket", env("FLINT_SNAPD_SOCKET", "/run/flint/snapd.sock"), "local API socket (FLINT_SNAPD_SOCKET)")
	fs.StringVar(&cfg.PeerListen, "peer-listen", env("FLINT_SNAPD_PEER_LISTEN", ":9501"), "peer chunk server address (FLINT_SNAPD_PEER_LISTEN)")
	fs.StringVar(&cfg.WatchdogDevice, "watchdog", env("FLINT_WATCHDOG_DEVICE", ""), "host watchdog device, e.g. /dev/watchdog (FLINT_WATCHDOG_DEVICE)")
	fs.Parse(args)
	cfg.CacheMax = int64(*cacheGB) << 30
	if cfg.StoreURL == "" {
		return fmt.Errorf("--store (FLINT_STORE_URL) is required")
	}
	return snapd.Run(ctx, cfg)
}

type labelFlags map[string]string

func (l labelFlags) String() string { return "" }
func (l labelFlags) Set(v string) error {
	k, val, ok := strings.Cut(v, "=")
	if !ok {
		return fmt.Errorf("label must be key=value")
	}
	l[k] = val
	return nil
}

func runJoin(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("join", flag.ExitOnError)
	var req join.Request
	req.Labels = map[string]string{}
	fs.StringVar(&req.Token, "token", env("FLINT_JOIN_TOKEN", ""), "join token (FLINT_JOIN_TOKEN)")
	fs.StringVar(&req.Dir, "identity", env("FLINT_IDENTITY_DIR", "/var/lib/flint/state/cluster"), "identity directory to write (FLINT_IDENTITY_DIR)")
	host, _ := os.Hostname()
	fs.StringVar(&req.Name, "name", env("FLINT_HOST_NAME", host), "host name (FLINT_HOST_NAME)")
	fs.StringVar(&req.AgentURL, "agent-url", env("FLINT_AGENT_URL", ""), "URL the control plane uses to reach this host's agent (FLINT_AGENT_URL)")
	fs.StringVar(&req.SnapdURL, "snapd-url", env("FLINT_SNAPD_URL", ""), "URL other hosts use to reach this host's snapd (FLINT_SNAPD_URL)")
	fs.StringVar(&req.Zone, "zone", env("FLINT_ZONE", ""), "failure domain (FLINT_ZONE)")
	fs.StringVar(&req.Backend, "backend", env("FLINT_BACKEND_KIND", ""), "backend kind")
	fs.StringVar(&req.HostClass, "host-class", env("FLINT_HOST_CLASS", ""), "host class")
	fs.Var(labelFlags(req.Labels), "label", "key=value label (repeatable)")
	fs.Parse(args)
	if req.Token == "" || req.AgentURL == "" || req.SnapdURL == "" {
		return fmt.Errorf("--token, --agent-url and --snapd-url are required")
	}
	id, err := join.Join(ctx, req)
	if err != nil {
		return err
	}
	fmt.Println(id.HostID)
	return nil
}
