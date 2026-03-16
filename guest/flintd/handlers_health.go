package main

import (
	"encoding/json"
	"net/http"
	"time"
)

func handleHealth(w http.ResponseWriter, r *http.Request) {
	uptime := time.Since(startTime).Milliseconds()
	resp := map[string]any{
		"status":     "ok",
		"uptime_ms":  uptime,
		"processes":  pm.count(),
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}
