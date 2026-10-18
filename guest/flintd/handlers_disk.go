package main

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"github.com/jverre/flint/services/r2nfs/nbd"
)

// Block devices the host serves over NBD (exclusive volumes, and read-only
// snapshots). The daemon attaches one here, then mounts it with exec.

type attachedDisk struct {
	device *nbd.Device
	conn   net.Conn
}

var (
	disksMu sync.Mutex
	disks   = map[string]*attachedDisk{} // by device path
)

// nbdTimeout: requests the host hasn't answered by then fail with EIO
// instead of hanging the guest forever.
const nbdTimeout = 60 * time.Second

type diskAttachRequest struct {
	Address string `json:"address"` // host:port of the NBD server
	Export  string `json:"export"`
}

func handleDiskAttach(w http.ResponseWriter, r *http.Request) {
	var req diskAttachRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Address == "" || req.Export == "" {
		jsonError(w, http.StatusBadRequest, "address and export are required")
		return
	}
	disksMu.Lock()
	defer disksMu.Unlock()
	inUse := map[string]bool{}
	for dev := range disks {
		inUse[dev] = true
	}
	if _, err := nbd.FreeDevice(inUse); err != nil {
		jsonError(w, http.StatusConflict, err.Error()+" (is the guest kernel built with CONFIG_BLK_DEV_NBD?)")
		return
	}
	dial := func() (nbd.FileConn, error) {
		conn, err := net.DialTimeout("tcp", req.Address, 10*time.Second)
		if err != nil {
			return nil, err
		}
		return conn.(*net.TCPConn), nil
	}
	device, conn, err := nbd.AttachFree(dial, req.Export, nbdTimeout, inUse)
	if err != nil {
		jsonError(w, http.StatusBadGateway, err.Error())
		return
	}
	disks[device.Path] = &attachedDisk{device: device, conn: conn.(net.Conn)}
	writeJSON(w, map[string]string{"device": device.Path})
}

func handleDiskDetach(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Device string `json:"device"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Device == "" {
		jsonError(w, http.StatusBadRequest, "device is required")
		return
	}
	disksMu.Lock()
	d := disks[req.Device]
	delete(disks, req.Device)
	disksMu.Unlock()
	if d == nil {
		jsonError(w, http.StatusNotFound, "no disk attached at "+req.Device)
		return
	}
	err := d.device.Disconnect()
	d.conn.Close()
	if err != nil {
		jsonError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, map[string]bool{"ok": true})
}

const (
	ioctlFIFREEZE = 0xC0045877
	ioctlFITHAW   = 0xC0045878
)

// handleFreeze freezes (flushes and blocks writes to) or thaws the
// filesystem mounted at path, so the host can snapshot its disk.
func handleFreeze(freeze bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Path string `json:"path"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Path == "" {
			jsonError(w, http.StatusBadRequest, "path is required")
			return
		}
		f, err := os.Open(req.Path)
		if err != nil {
			jsonError(w, http.StatusNotFound, err.Error())
			return
		}
		defer f.Close()
		op := uintptr(ioctlFIFREEZE)
		if !freeze {
			op = ioctlFITHAW
		}
		var arg int
		if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), op, uintptr(unsafe.Pointer(&arg))); errno != 0 {
			jsonError(w, http.StatusInternalServerError, fmt.Sprintf("%s: %v", req.Path, errno))
			return
		}
		writeJSON(w, map[string]bool{"ok": true})
	}
}

func jsonError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"error": msg})
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}
