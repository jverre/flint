package main

import (
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
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Source == "" || req.Target == "" {
		jsonError(w, http.StatusBadRequest, "source and target are required")
		return
	}
	if !strings.HasPrefix(req.Target, "/") {
		jsonError(w, http.StatusBadRequest, "target must be an absolute path")
		return
	}
	if err := os.MkdirAll(req.Target, 0755); err != nil {
		jsonError(w, http.StatusInternalServerError, fmt.Sprintf("mkdir %s: %v", req.Target, err))
		return
	}

	args := []string{"-t", "nfs"}
	if req.Options != "" {
		args = append(args, "-o", req.Options)
	}
	args = append(args, req.Source, req.Target)
	if output, err := exec.Command("mount", args...).CombinedOutput(); err != nil {
		jsonError(w, http.StatusInternalServerError, fmt.Sprintf("mount failed: %v: %s", err, output))
		return
	}
	writeJSON(w, map[string]any{"ok": true, "target": req.Target})
}

func handleUnmountNFS(w http.ResponseWriter, r *http.Request) {
	var req unmountRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Target == "" {
		jsonError(w, http.StatusBadRequest, "target is required")
		return
	}
	if output, err := exec.Command("umount", req.Target).CombinedOutput(); err != nil {
		jsonError(w, http.StatusInternalServerError, fmt.Sprintf("umount failed: %v: %s", err, output))
		return
	}
	writeOK(w)
}
