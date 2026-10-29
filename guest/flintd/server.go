package main

import (
	"encoding/json"
	"errors"
	"io/fs"
	"net/http"
	"strconv"
)

func newServer() *http.ServeMux {
	mux := http.NewServeMux()

	// Health
	mux.HandleFunc("GET /health", handleHealth)

	// Synchronous exec
	mux.HandleFunc("POST /exec", handleExec)

	// Process management
	mux.HandleFunc("POST /processes", handleProcessCreate)
	mux.HandleFunc("GET /processes", handleProcessList)
	mux.HandleFunc("GET /processes/{pid}", handleProcessGet)
	mux.HandleFunc("POST /processes/{pid}/input", handleProcessInput)
	mux.HandleFunc("POST /processes/{pid}/signal", handleProcessSignal)
	mux.HandleFunc("POST /processes/{pid}/resize", handleProcessResize)

	// WebSocket output streaming
	mux.HandleFunc("GET /processes/{pid}/output", handleProcessOutput)

	// Filesystem
	mux.HandleFunc("GET /files", handleFileRead)
	mux.HandleFunc("POST /files", handleFileWrite)
	mux.HandleFunc("GET /files/stat", handleFileStat)
	mux.HandleFunc("GET /files/list", handleFileList)
	mux.HandleFunc("POST /files/mkdir", handleFileMkdir)
	mux.HandleFunc("DELETE /files", handleFileDelete)

	// NFS mount management (for cloud storage backends)
	mux.HandleFunc("POST /mount/nfs", handleMountNFS)
	mux.HandleFunc("DELETE /mount/nfs", handleUnmountNFS)

	// Block devices served by the host over NBD (exclusive volumes)
	mux.HandleFunc("POST /disks/attach", handleDiskAttach)
	mux.HandleFunc("POST /disks/detach", handleDiskDetach)
	mux.HandleFunc("POST /fs/freeze", handleFreeze(true))
	mux.HandleFunc("POST /fs/thaw", handleFreeze(false))

	return mux
}

// Every reply is JSON, errors included: {"error": "..."} with an HTTP error
// status. The helpers below are the only way handlers write one, so the body
// is always valid JSON whatever the message holds (command output, paths
// with quotes or newlines).

// writeJSON replies 200 with v as JSON.
func writeJSON(w http.ResponseWriter, v any) {
	writeJSONStatus(w, http.StatusOK, v)
}

func writeJSONStatus(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

// writeOK replies {"ok":true}.
func writeOK(w http.ResponseWriter) {
	writeJSON(w, map[string]bool{"ok": true})
}

// jsonError replies {"error": msg} with status.
func jsonError(w http.ResponseWriter, status int, msg string) {
	writeJSONStatus(w, status, map[string]string{"error": msg})
}

// decodeJSON decodes the request body into v, replying 400 if it can't.
func decodeJSON(w http.ResponseWriter, r *http.Request, v any) bool {
	if err := json.NewDecoder(r.Body).Decode(v); err != nil {
		jsonError(w, http.StatusBadRequest, "invalid json: "+err.Error())
		return false
	}
	return true
}

// requirePath returns the request's ?path=, replying 400 if it is missing.
func requirePath(w http.ResponseWriter, r *http.Request) (string, bool) {
	path := r.URL.Query().Get("path")
	if path == "" {
		jsonError(w, http.StatusBadRequest, "path is required")
		return "", false
	}
	return path, true
}

// fsError replies with err from a filesystem call: 404 if the path doesn't
// exist, 500 otherwise.
func fsError(w http.ResponseWriter, err error) {
	status := http.StatusInternalServerError
	if errors.Is(err, fs.ErrNotExist) {
		status = http.StatusNotFound
	}
	jsonError(w, status, err.Error())
}

// requestProcess returns the process the request's {pid} names, replying
// 400 or 404 if there is none.
func requestProcess(w http.ResponseWriter, r *http.Request) (*processHandler, bool) {
	pid, err := strconv.Atoi(r.PathValue("pid"))
	if err != nil {
		jsonError(w, http.StatusBadRequest, "invalid pid")
		return nil, false
	}
	h, err := requireProcess(pid)
	if err != nil {
		jsonError(w, http.StatusNotFound, err.Error())
		return nil, false
	}
	return h, true
}
