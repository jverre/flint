package main

import (
	"io"
	"net/http"
	"syscall"
)

func handleProcessCreate(w http.ResponseWriter, r *http.Request) {
	var req startProcessRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if len(req.Cmd) == 0 {
		jsonError(w, http.StatusBadRequest, "cmd is required")
		return
	}
	h, err := startProcess(req)
	if err != nil {
		jsonError(w, http.StatusInternalServerError, err.Error())
		return
	}
	// 201: the host's runtime checks for it.
	writeJSONStatus(w, http.StatusCreated, map[string]int{"pid": h.pid})
}

func handleProcessList(w http.ResponseWriter, r *http.Request) {
	procs := pm.list()
	result := make([]processInfo, 0, len(procs))
	for _, h := range procs {
		result = append(result, h.info())
	}
	writeJSON(w, map[string]any{"processes": result})
}

func handleProcessGet(w http.ResponseWriter, r *http.Request) {
	if h, ok := requestProcess(w, r); ok {
		writeJSON(w, h.info())
	}
}

func handleProcessInput(w http.ResponseWriter, r *http.Request) {
	h, ok := requestProcess(w, r)
	if !ok {
		return
	}
	data, err := io.ReadAll(io.LimitReader(r.Body, 1<<20)) // 1MB max
	if err != nil {
		jsonError(w, http.StatusBadRequest, "read body failed")
		return
	}
	if err := h.writeInput(data); err != nil {
		jsonError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeOK(w)
}

func handleProcessSignal(w http.ResponseWriter, r *http.Request) {
	h, ok := requestProcess(w, r)
	if !ok {
		return
	}
	var body struct {
		Signal int `json:"signal"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	if err := h.sendSignal(syscall.Signal(body.Signal)); err != nil {
		jsonError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeOK(w)
}

func handleProcessResize(w http.ResponseWriter, r *http.Request) {
	h, ok := requestProcess(w, r)
	if !ok {
		return
	}
	var body struct {
		Cols uint16 `json:"cols"`
		Rows uint16 `json:"rows"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	if err := h.resize(body.Cols, body.Rows); err != nil {
		jsonError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeOK(w)
}
