package snapstore

import (
	"encoding/json"
	"net/http"
	"strings"
)

// PeerHandler serves this host's cached chunks and manifests to other hosts.
// Mount it behind mTLS that only admits cluster host certificates. A chunk is
// addressed by its SHA-256, so knowing the hash is what lets a host fetch it.
func PeerHandler(c *Cache) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/peer/chunks/{hash}", func(w http.ResponseWriter, r *http.Request) {
		h := r.PathValue("hash")
		if !validHash(h) {
			http.Error(w, "bad hash", http.StatusBadRequest)
			return
		}
		b, err := c.GetChunk(h)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Write(b)
	})
	mux.HandleFunc("GET /v1/peer/manifests/{id}", func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimSuffix(r.PathValue("id"), ".json")
		m, err := c.GetManifest(id)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(m)
	})
	return mux
}
