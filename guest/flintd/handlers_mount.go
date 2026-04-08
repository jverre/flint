package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"strings"
)

type mountNFSRequest struct {
	Source  string `json:"source"`  // e.g., "10.0.0.1:/sandboxes/abc123"
	Target  string `json:"target"`  // e.g., "/workspace"
	Options string `json:"options"` // e.g., "vers=3,soft,timeo=50,retrans=3,nolock"
}

type unmountRequest struct {
	Target string `json:"target"` // e.g., "/workspace"
}

func handleMountNFS(w http.ResponseWriter, r *http.Request) {
	var req mountNFSRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"invalid json: %s"}`, err.Error()), http.StatusBadRequest)
		return
	}
	if req.Source == "" || req.Target == "" {
		http.Error(w, `{"error":"source and target are required"}`, http.StatusBadRequest)
		return
	}

	// Validate target path to prevent path traversal.
	if !strings.HasPrefix(req.Target, "/") {
		http.Error(w, `{"error":"target must be an absolute path"}`, http.StatusBadRequest)
		return
	}

	// Create mount point directory.
	if err := os.MkdirAll(req.Target, 0755); err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"mkdir %s: %s"}`, req.Target, err.Error()), http.StatusInternalServerError)
		return
	}

	// Build mount command.
	args := []string{"-t", "nfs"}
	if req.Options != "" {
		args = append(args, "-o", req.Options)
	}
	args = append(args, req.Source, req.Target)

	cmd := exec.Command("mount", args...)
	output, err := cmd.CombinedOutput()
	if err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"mount failed: %s: %s"}`, err.Error(), string(output)), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(fmt.Sprintf(`{"ok":true,"target":"%s"}`, req.Target)))
}

func handleUnmountNFS(w http.ResponseWriter, r *http.Request) {
	var req unmountRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"invalid json: %s"}`, err.Error()), http.StatusBadRequest)
		return
	}
	if req.Target == "" {
		http.Error(w, `{"error":"target is required"}`, http.StatusBadRequest)
		return
	}

	cmd := exec.Command("umount", req.Target)
	output, err := cmd.CombinedOutput()
	if err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"umount failed: %s: %s"}`, err.Error(), string(output)), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"ok":true}`))
}
