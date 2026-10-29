package main

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
)

// fileInfo describes a file in /files/stat and /files/list replies.
type fileInfo struct {
	Path  string `json:"path,omitempty"` // stat only
	Name  string `json:"name,omitempty"` // list only
	Size  int64  `json:"size"`
	IsDir bool   `json:"is_dir"`
	Mode  string `json:"mode"` // permission bits, octal
}

func newFileInfo(info os.FileInfo) fileInfo {
	return fileInfo{Size: info.Size(), IsDir: info.IsDir(), Mode: fmt.Sprintf("%04o", info.Mode().Perm())}
}

func handleFileRead(w http.ResponseWriter, r *http.Request) {
	path, ok := requirePath(w, r)
	if !ok {
		return
	}
	f, err := os.Open(path)
	if err != nil {
		fsError(w, err)
		return
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() {
		jsonError(w, http.StatusBadRequest, "not a regular file")
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	if st.Size() == 0 { // procfs reports zero size for files such as /proc/meminfo.
		io.Copy(w, f)
		return
	}
	http.ServeContent(w, r, st.Name(), st.ModTime(), f)
}

func handleFileWrite(w http.ResponseWriter, r *http.Request) {
	path, ok := requirePath(w, r)
	if !ok {
		return
	}
	mode := os.FileMode(0644)
	if m, err := strconv.ParseUint(r.URL.Query().Get("mode"), 8, 32); err == nil {
		mode = os.FileMode(m)
	}
	// Streamed to the file: no size limit, and no copy of it in memory.
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, mode)
	if err != nil {
		jsonError(w, http.StatusInternalServerError, err.Error())
		return
	}
	_, err = io.Copy(f, r.Body)
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		jsonError(w, http.StatusInternalServerError, "write failed: "+err.Error())
		return
	}
	writeOK(w)
}

func handleFileStat(w http.ResponseWriter, r *http.Request) {
	path, ok := requirePath(w, r)
	if !ok {
		return
	}
	info, err := os.Stat(path)
	if err != nil {
		fsError(w, err)
		return
	}
	resp := newFileInfo(info)
	resp.Path = path
	writeJSON(w, resp)
}

func handleFileList(w http.ResponseWriter, r *http.Request) {
	path, ok := requirePath(w, r)
	if !ok {
		return
	}
	entries, err := os.ReadDir(path)
	if err != nil {
		fsError(w, err)
		return
	}
	result := make([]fileInfo, 0, len(entries))
	for _, e := range entries {
		info, err := e.Info()
		if err != nil {
			continue
		}
		fi := newFileInfo(info)
		fi.Name = e.Name()
		result = append(result, fi)
	}
	writeJSON(w, map[string]any{"entries": result})
}

func handleFileMkdir(w http.ResponseWriter, r *http.Request) {
	path, ok := requirePath(w, r)
	if !ok {
		return
	}
	mkdir := os.Mkdir
	if r.URL.Query().Get("parents") == "true" {
		mkdir = os.MkdirAll
	}
	if err := mkdir(path, 0755); err != nil {
		jsonError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeOK(w)
}

func handleFileDelete(w http.ResponseWriter, r *http.Request) {
	path, ok := requirePath(w, r)
	if !ok {
		return
	}
	remove := os.Remove
	if r.URL.Query().Get("recursive") == "true" {
		remove = os.RemoveAll
	}
	if err := remove(path); err != nil {
		jsonError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeOK(w)
}
