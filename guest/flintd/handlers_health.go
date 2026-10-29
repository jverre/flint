package main

import (
	"net/http"
	"time"
)

func handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]any{
		"status":    "ok",
		"uptime_ms": time.Since(startTime).Milliseconds(),
		"processes": pm.count(),
	})
}
