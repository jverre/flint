package main

import "net/http"

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

	return mux
}
