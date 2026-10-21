// Package webui serves the Flint admin UI (built from web/ by scripts/build-ui.sh) at /ui.
// The build is embedded, so the control plane binary carries its own UI. API calls from the
// UI go to the same origin and authenticate with the user's token like any client.
package webui

import (
	"embed"
	"io/fs"
	"net/http"
	"path"
	"strings"
)

//go:embed all:dist
var dist embed.FS

const missing = `<!doctype html><meta charset="utf-8"><title>Flint</title>
<body style="font:14px system-ui;background:#151922;color:#eef1f7;display:grid;place-items:center;height:100vh;margin:0">
<div><h1 style="font-size:18px">The Flint web UI isn't built into this binary</h1>
<p>Run <code>scripts/build-ui.sh</code>, then rebuild flint-cluster.</p></div>`

// Handler serves files under /ui/ and falls back to index.html for client-side routes.
func Handler() http.Handler {
	root, err := fs.Sub(dist, "dist")
	if err != nil {
		panic(err)
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		index, err := fs.ReadFile(root, "index.html")
		if err != nil {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(missing))
			return
		}
		name := strings.TrimPrefix(path.Clean("/"+strings.TrimPrefix(r.URL.Path, "/ui")), "/")
		if name != "" && name != "index.html" {
			if st, err := fs.Stat(root, name); err == nil && !st.IsDir() {
				if strings.HasPrefix(name, "assets/") {
					// Vite fingerprints assets, so they never change under the same name.
					w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
				} else {
					w.Header().Set("Cache-Control", "no-cache")
				}
				http.ServeFileFS(w, r, root, name)
				return
			}
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-cache")
		_, _ = w.Write(index)
	})
}
