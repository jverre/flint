// r2nfs is Flint's NFS server for sandboxes.
//
// It serves two kinds of export, either of which can be enabled:
//   - Workspaces on Cloudflare R2, with overlay semantics: a read-only
//     template layer and a read-write sandbox layer, giving the same
//     isolation as copy-on-write local filesystems.
//   - Volumes: host directories that the daemon mounts into running
//     sandboxes, read-write or read-only, one export per mount.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"unicode"

	"github.com/jverre/flint/services/r2nfs/blockstore"
	nfs "github.com/willscott/go-nfs"
	nfshelper "github.com/willscott/go-nfs/helpers"
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
	volumesDir := flag.String("volumes-dir", "", "Host directory holding volumes (enables volume mounts)")
	imagesDir := flag.String("images-dir", "", "Host directory holding block images (enables image-backed volumes)")
	nbdListen := flag.String("nbd-listen", "", "Address sandboxes attach read-write images on (NBD)")
	nbdROListen := flag.String("nbd-ro-listen", "", "Address sandboxes attach read-only images on (NBD)")
	flag.Parse()

	r2Enabled := *accountID != "" && *accessKey != "" && *secretKey != ""
	if !r2Enabled && *volumesDir == "" {
		log.Fatal("nothing to serve: set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY, or --volumes-dir")
	}

	exports := newExportManager(nil, nil)
	var r2 *R2Client
	if r2Enabled {
		endpoint := fmt.Sprintf("https://%s.r2.cloudflarestorage.com", *accountID)
		var err error
		r2, err = newR2Client(endpoint, *accessKey, *secretKey, *bucket)
		if err != nil {
			log.Fatalf("Failed to create R2 client: %v", err)
		}
		cache, err := newDiskCache(*cacheDir, int64(*cacheSizeMB)*1024*1024)
		if err != nil {
			log.Fatalf("Failed to create cache: %v", err)
		}
		defer cache.Close()
		exports = newExportManager(r2, cache)
	}
	volumes := newVolumeManager(*volumesDir)

	var images *ImageManager
	if *imagesDir != "" && *volumesDir != "" {
		var remote blockstore.Remote
		if r2 != nil {
			remote = r2 // images are stored in R2 too, and load lazily on any host
		}
		var err error
		if images, err = newImageManager(*imagesDir, *volumesDir, remote); err != nil {
			log.Printf("Image-backed volumes disabled: %v", err)
			images = nil
		}
	}

	if images != nil {
		for _, l := range []struct {
			addr     string
			readOnly bool
		}{{*nbdListen, false}, {*nbdROListen, true}} {
			if l.addr == "" {
				continue
			}
			ln, err := net.Listen("tcp", l.addr)
			if err != nil {
				log.Fatalf("Failed to listen on %s: %v", l.addr, err)
			}
			go func(readOnly bool) {
				if err := images.ServeNBD(ln, readOnly); err != nil {
					log.Fatalf("NBD listener: %v", err)
				}
			}(l.readOnly)
			images.servingNBD.Store(true)
			log.Printf("NBD listening on %s (readonly=%v)", l.addr, l.readOnly)
		}
	}

	// Start management API.
	go serveMgmtAPI(*mgmtAddr, exports, volumes, images)

	// Start NFS server.
	listener, err := net.Listen("tcp", *listenAddr)
	if err != nil {
		log.Fatalf("Failed to listen on %s: %v", *listenAddr, err)
	}
	log.Printf("r2nfs listening on %s (mgmt on %s)", *listenAddr, *mgmtAddr)

	handler := newNFSHandler(exports, volumes)
	cachedHandler := nfshelper.NewCachingHandler(handler, handleCacheSize)
	go func() {
		// Closing the listener at shutdown ends Serve; that mustn't exit
		// before the shutdown path has flushed the images.
		if err := nfs.Serve(listener, cachedHandler); err != nil && !errors.Is(err, net.ErrClosed) {
			log.Fatalf("NFS server error: %v", err)
		}
	}()

	// Wait for shutdown signal.
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Println("Shutting down r2nfs")
	listener.Close()
	if images != nil {
		images.Flush() // the devices go with this process; get their data into the images first
	}
}

// handleCacheSize bounds the NFS file handles the server remembers. A guest
// whose handle was evicted gets ESTALE, so this must cover every file
// clients are working with across all mounts.
const handleCacheSize = 1 << 17

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

func writeJSONError(w http.ResponseWriter, status int, err error) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
}

func writeOK(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"ok":true}`))
}

// registerImageRoutes adds the image API; without images every call is 503.
func registerImageRoutes(mux *http.ServeMux, images *ImageManager) {
	withImage := func(h func(w http.ResponseWriter, r *http.Request, id string)) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if images == nil {
				writeJSONError(w, http.StatusServiceUnavailable, fmt.Errorf("image-backed volumes are not enabled"))
				return
			}
			id := r.PathValue("id")
			if !isValidID(id) {
				writeJSONError(w, http.StatusBadRequest, fmt.Errorf("invalid image id"))
				return
			}
			h(w, r, id)
		}
	}
	mux.HandleFunc("PUT /images/{id}", withImage(func(w http.ResponseWriter, r *http.Request, id string) {
		var req struct {
			Size     int64  `json:"size"`
			From     string `json:"from"`
			ReadOnly bool   `json:"readonly"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSONError(w, http.StatusBadRequest, fmt.Errorf("invalid json"))
			return
		}
		if req.From != "" && !isValidID(req.From) {
			writeJSONError(w, http.StatusBadRequest, fmt.Errorf("invalid source image"))
			return
		}
		if err := images.Create(id, req.Size, req.From, req.ReadOnly); err != nil {
			writeJSONError(w, http.StatusBadRequest, err)
			return
		}
		writeOK(w)
	}))
	mux.HandleFunc("DELETE /images/{id}", withImage(func(w http.ResponseWriter, r *http.Request, id string) {
		if err := images.Delete(id); err != nil {
			writeJSONError(w, http.StatusInternalServerError, err)
			return
		}
		writeOK(w)
	}))
	mux.HandleFunc("POST /images/{id}/mount", withImage(func(w http.ResponseWriter, r *http.Request, id string) {
		if err := images.Mount(id); err != nil {
			writeJSONError(w, http.StatusInternalServerError, err)
			return
		}
		writeOK(w)
	}))
	mux.HandleFunc("DELETE /images/{id}/mount", withImage(func(w http.ResponseWriter, r *http.Request, id string) {
		images.Unmount(id)
		writeOK(w)
	}))
	// Exports: {id} names the export; the body names the image.
	mux.HandleFunc("PUT /nbd/{id}", withImage(func(w http.ResponseWriter, r *http.Request, name string) {
		var req struct {
			Image    string `json:"image"`
			ClientIP string `json:"client_ip"`
			ReadOnly bool   `json:"readonly"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSONError(w, http.StatusBadRequest, fmt.Errorf("invalid json"))
			return
		}
		if !isValidID(req.Image) || net.ParseIP(req.ClientIP) == nil {
			writeJSONError(w, http.StatusBadRequest, fmt.Errorf("image and client_ip are required"))
			return
		}
		if err := images.Export(name, req.Image, req.ClientIP, req.ReadOnly); err != nil {
			writeJSONError(w, http.StatusConflict, err)
			return
		}
		writeOK(w)
	}))
	mux.HandleFunc("DELETE /nbd/{id}", withImage(func(w http.ResponseWriter, r *http.Request, name string) {
		images.Unexport(name)
		writeOK(w)
	}))
}

// newMgmtHandler builds the HTTP handler for the management API.
// Extracted so tests can call it without starting a real listener.
func newMgmtHandler(exports *ExportManager, volumes *VolumeManager, images *ImageManager) http.Handler {
	mux := http.NewServeMux()
	registerMgmtRoutes(mux, exports, volumes, images)
	return mux
}

// serveMgmtAPI runs a small HTTP server for managing NFS exports.
func serveMgmtAPI(addr string, exports *ExportManager, volumes *VolumeManager, images *ImageManager) {
	mux := http.NewServeMux()
	registerMgmtRoutes(mux, exports, volumes, images)
	log.Printf("Management API on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("Management API error: %v", err)
	}
}

// registerMgmtRoutes adds the management API routes to a ServeMux.
func registerMgmtRoutes(mux *http.ServeMux, exports *ExportManager, volumes *VolumeManager, images *ImageManager) {
	registerImageRoutes(mux, images)

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

	mux.HandleFunc("POST /mounts", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			ID       string `json:"id"`
			ClientIP string `json:"client_ip"`
			Volume   string `json:"volume"`
			Subpath  string `json:"subpath"`
			ReadOnly bool   `json:"readonly"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, `{"error":"invalid json"}`, http.StatusBadRequest)
			return
		}
		if !isValidID(req.ID) || net.ParseIP(req.ClientIP) == nil || req.Volume == "" {
			http.Error(w, `{"error":"id, client_ip and volume are required"}`, http.StatusBadRequest)
			return
		}
		if err := volumes.Register(req.ID, req.ClientIP, req.Volume, req.Subpath, req.ReadOnly); err != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true}`))
	})

	mux.HandleFunc("DELETE /mounts/{id}", func(w http.ResponseWriter, r *http.Request) {
		volumes.Deregister(r.PathValue("id"))
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true}`))
	})

	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{
			"status": "ok", "exports": exports.Count(), "mounts": volumes.Count(),
			"r2": exports.r2 != nil, "volumes_dir": volumes.root, "images": images != nil,
			"nbd": images != nil && images.servingNBD.Load(),
		})
	})
}
