// r2nfs serves Cloudflare R2 objects as an NFS filesystem.
//
// It provides per-sandbox NFS exports with overlay semantics:
// a read-only template layer and a read-write sandbox layer,
// giving the same isolation as copy-on-write local filesystems.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"unicode"

	nfs "github.com/willscott/go-nfs"
)

func main() {
	listenAddr := flag.String("listen", "10.0.0.1:2049", "NFS listen address")
	mgmtAddr := flag.String("mgmt", "127.0.0.1:9200", "Management API listen address")
	bucket := flag.String("bucket", envOr("R2_BUCKET", "flint-storage"), "R2 bucket name")
	accountID := flag.String("account-id", os.Getenv("R2_ACCOUNT_ID"), "Cloudflare account ID")
	accessKey := flag.String("access-key", os.Getenv("R2_ACCESS_KEY_ID"), "R2 access key ID")
	secretKey := flag.String("secret-key", os.Getenv("R2_SECRET_ACCESS_KEY"), "R2 secret access key")
	cacheDir := flag.String("cache-dir", envOr("R2_CACHE_DIR", "/tmp/r2nfs-cache"), "Local cache directory")
	cacheSizeMB := flag.Int("cache-size-mb", 1024, "Maximum cache size in MB")
	flag.Parse()

	if *accountID == "" || *accessKey == "" || *secretKey == "" {
		log.Fatal("R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY are required")
	}

	endpoint := fmt.Sprintf("https://%s.r2.cloudflarestorage.com", *accountID)

	r2, err := newR2Client(endpoint, *accessKey, *secretKey, *bucket)
	if err != nil {
		log.Fatalf("Failed to create R2 client: %v", err)
	}

	cache, err := newDiskCache(*cacheDir, int64(*cacheSizeMB)*1024*1024)
	if err != nil {
		log.Fatalf("Failed to create cache: %v", err)
	}

	exports := newExportManager(r2, cache)

	// Start management API.
	go serveMgmtAPI(*mgmtAddr, exports)

	// Start NFS server.
	listener, err := net.Listen("tcp", *listenAddr)
	if err != nil {
		log.Fatalf("Failed to listen on %s: %v", *listenAddr, err)
	}
	log.Printf("r2nfs listening on %s (mgmt on %s)", *listenAddr, *mgmtAddr)

	handler := newNFSHandler(exports)
	go func() {
		if err := nfs.Serve(listener, handler); err != nil {
			log.Fatalf("NFS server error: %v", err)
		}
	}()

	// Wait for shutdown signal.
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Println("Shutting down r2nfs")
	listener.Close()
	cache.Close()
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// isValidID checks that an ID contains only safe characters (letters, digits,
// hyphens, underscores) to prevent path traversal via crafted vm_id/template_id.
func isValidID(s string) bool {
	if len(s) == 0 || len(s) > 128 {
		return false
	}
	for _, c := range s {
		if !unicode.IsLetter(c) && !unicode.IsDigit(c) && c != '-' && c != '_' {
			return false
		}
	}
	return true
}

// newMgmtHandler builds the HTTP handler for the management API.
// Extracted so tests can call it without starting a real listener.
func newMgmtHandler(exports *ExportManager) http.Handler {
	mux := http.NewServeMux()
	registerMgmtRoutes(mux, exports)
	return mux
}

// serveMgmtAPI runs a small HTTP server for managing NFS exports.
func serveMgmtAPI(addr string, exports *ExportManager) {
	mux := http.NewServeMux()
	registerMgmtRoutes(mux, exports)
	log.Printf("Management API on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("Management API error: %v", err)
	}
}

// registerMgmtRoutes adds the management API routes to a ServeMux.
func registerMgmtRoutes(mux *http.ServeMux, exports *ExportManager) {
	mux.HandleFunc("POST /exports", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			ClientIP   string `json:"client_ip"`
			VMID       string `json:"vm_id"`
			TemplateID string `json:"template_id"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, `{"error":"invalid json"}`, http.StatusBadRequest)
			return
		}
		if req.ClientIP == "" || req.VMID == "" {
			http.Error(w, `{"error":"client_ip and vm_id are required"}`, http.StatusBadRequest)
			return
		}
		if net.ParseIP(req.ClientIP) == nil {
			http.Error(w, `{"error":"invalid client_ip"}`, http.StatusBadRequest)
			return
		}
		if !isValidID(req.VMID) {
			http.Error(w, `{"error":"invalid vm_id"}`, http.StatusBadRequest)
			return
		}
		if req.TemplateID == "" {
			req.TemplateID = "default"
		}
		if !isValidID(req.TemplateID) {
			http.Error(w, `{"error":"invalid template_id"}`, http.StatusBadRequest)
			return
		}
		exports.Register(req.ClientIP, req.VMID, req.TemplateID)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true}`))
	})

	mux.HandleFunc("DELETE /exports/{client_ip}", func(w http.ResponseWriter, r *http.Request) {
		clientIP := r.PathValue("client_ip")
		exports.Deregister(clientIP)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true}`))
	})

	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"status":"ok","exports":%d}`, exports.Count())
	})
}
