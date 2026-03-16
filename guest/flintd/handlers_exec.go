package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"time"
)

type execRequest struct {
	Cmd     []string          `json:"cmd"`
	Env     map[string]string `json:"env,omitempty"`
	Cwd     string            `json:"cwd,omitempty"`
	Timeout int               `json:"timeout,omitempty"`
}

type execResponse struct {
	Stdout   string `json:"stdout"`
	Stderr   string `json:"stderr"`
	ExitCode int    `json:"exit_code"`
}

func handleExec(w http.ResponseWriter, r *http.Request) {
	var req execRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, `{"error":"invalid json"}`, http.StatusBadRequest)
		return
	}
	if len(req.Cmd) == 0 {
		http.Error(w, `{"error":"cmd is required"}`, http.StatusBadRequest)
		return
	}

	timeout := time.Duration(req.Timeout) * time.Second
	if timeout <= 0 {
		timeout = 60 * time.Second
	}

	// Start a non-PTY process with early subscription to avoid missing output
	h, ch, err := startProcessSubscribed(startProcessRequest{
		Cmd: req.Cmd,
		Env: req.Env,
		Cwd: req.Cwd,
		Pty: false,
	})
	if err != nil {
		// Command failed to start (e.g., binary not found) — return as exit_code 127
		resp := execResponse{
			Stderr:   err.Error() + "\n",
			ExitCode: 127,
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
		return
	}

	// Collect output with timeout
	var stdout, stderr bytes.Buffer
	exitCode := -1
	timer := time.NewTimer(timeout)
	defer timer.Stop()

loop:
	for {
		select {
		case ev, ok := <-ch:
			if !ok {
				break loop
			}
			switch ev.Type {
			case "stdout":
				raw, _ := base64.StdEncoding.DecodeString(ev.Data)
				stdout.Write(raw)
			case "stderr":
				raw, _ := base64.StdEncoding.DecodeString(ev.Data)
				stderr.Write(raw)
			case "exit":
				if ev.Code != nil {
					exitCode = *ev.Code
				}
				break loop
			}
		case <-timer.C:
			// Timeout — kill the process
			h.cancel()
			exitCode = -1
			break loop
		}
	}

	resp := execResponse{
		Stdout:   stdout.String(),
		Stderr:   stderr.String(),
		ExitCode: exitCode,
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}
