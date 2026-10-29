package main

import (
	"bytes"
	"encoding/base64"
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
	Stdout string `json:"stdout"`
	Stderr string `json:"stderr"`
	// ExitCode must stay the last field: when the output is too large to
	// buffer, the host daemon streams the reply through and finds the exit
	// code by matching `"exit_code": N}` at its end
	// (src/flint/daemon/server.py, exec_command).
	ExitCode int `json:"exit_code"`
}

func handleExec(w http.ResponseWriter, r *http.Request) {
	var req execRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if len(req.Cmd) == 0 {
		jsonError(w, http.StatusBadRequest, "cmd is required")
		return
	}

	timeout := time.Duration(req.Timeout) * time.Second
	if timeout <= 0 {
		timeout = 60 * time.Second
	}

	// Subscribe before the process starts, so no output is missed.
	h, ch, err := startProcessSubscribed(startProcessRequest{Cmd: req.Cmd, Env: req.Env, Cwd: req.Cwd})
	if err != nil {
		// Command failed to start (e.g., binary not found) — return as exit_code 127
		writeJSON(w, execResponse{Stderr: err.Error() + "\n", ExitCode: 127})
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
			h.cancel() // kills the process; exit code stays -1
			break loop
		}
	}

	writeJSON(w, execResponse{Stdout: stdout.String(), Stderr: stderr.String(), ExitCode: exitCode})
}
